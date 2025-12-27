import json
import logging
import os
import random
import re
import string
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for

# ==========================
# CONFIG
# ==========================

REG_URL = "https://streamingstreaming.com/reg.php"
MAILTM_BASE = "https://api.mail.tm"

PASSWORD_CHARSET = string.ascii_letters + string.digits
CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", "config.json"))

_DEFAULT_ADMIN_PASSWORD = "ChangeMeNow!"
_DEFAULT_SECRET = "replace-this-secret"

# ==========================
# CONFIG LOADER
# ==========================

def load_file_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

_FILE_CONFIG = load_file_config()

ADMIN_PASSWORD = (
    os.environ.get("ADMIN_PASSWORD")
    or _FILE_CONFIG.get("admin_password")
    or _DEFAULT_ADMIN_PASSWORD
)

SECRET_KEY = (
    os.environ.get("FLASK_SECRET_KEY")
    or _FILE_CONFIG.get("secret_key")
    or _DEFAULT_SECRET
)

# ==========================
# USER AGENTS
# ==========================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/121.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) Chrome/120.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Firefox/122.0",
    "Mozilla/5.0 (Linux; Android 12) Chrome/120.0.0.0 Mobile",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2) Safari/604.1",
]

def random_user_agent():
    return random.choice(USER_AGENTS)

# ==========================
# LOGGING
# ==========================

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("emby_auto")

# ==========================
# FLASK
# ==========================

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

# ==========================
# HELPERS
# ==========================

def generate_password():
    return "".join(random.choices(string.digits, k=6))


def extract_activation_link(mail_json):
    parts = []
    if mail_json.get("from", {}).get("address", "").endswith("@mail.tm"):
        return None
    else:
        text = mail_json.get("text")
        if isinstance(text, str):
            parts.append(text)

        html = mail_json.get("html")
        if isinstance(html, list):
            parts.extend(html)
        elif isinstance(html, str):
            parts.append(html)

        combined = "\n".join(parts)

        urls = re.findall(r"https?://[^\s\"'<>]+", combined)
        return urls[0] if urls else None


# ==========================
# MAIL.TM API
# ==========================

def mailtm_get_domain():
    r = requests.get(f"{MAILTM_BASE}/domains", timeout=10)
    r.raise_for_status()
    return r.json()["hydra:member"][0]["domain"]

def mailtm_create_account():
    domain = mailtm_get_domain()
    username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email = f"{username}@{domain}"
    password = generate_password()

    r = requests.post(
        f"{MAILTM_BASE}/accounts",
        json={"address": email, "password": password},
        timeout=10,
    )

    if r.status_code not in (200, 201):
        logger.error("Mail.tm account creation failed: %s", r.text)
        return None

    return {"email": email, "password": password}

def mailtm_get_token(email, password):
    r = requests.post(
        f"{MAILTM_BASE}/token",
        json={"address": email, "password": password},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["token"]

def mailtm_wait_for_message(token, timeout=300, interval=8):
    headers = {"Authorization": f"Bearer {token}"}
    start = time.time()

    while time.time() - start < timeout:
        r = requests.get(f"{MAILTM_BASE}/messages", headers=headers, timeout=10)
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            logger.error("Inbox returned HTML:\n%s", r.text[:500])
            time.sleep(interval)
            continue

        messages = data.get("hydra:member", [])

        logger.info("Mail.tm inbox messages: %d", len(messages))

        if messages:
            return messages[0]["id"]

        time.sleep(interval)

    return None

def mailtm_fetch_message(token, message_id):
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(
        f"{MAILTM_BASE}/messages/{message_id}",
        headers=headers,
        timeout=10,
    )

    try:
        return r.json()
    except Exception:
        logger.error("Mail.tm returned non-JSON:\n%s", r.text[:500])
        return None


# ==========================
# MAIN FLOW
# ==========================

def register_and_activate():
    progress = []

    def step(stage, msg):
        progress.append({"stage": stage, "text": msg, "time": time.time()})
        logger.info(msg)

    step("start", "מתחיל תהליך רישום")

    mail = mailtm_create_account()
    if not mail:
        return {"success": False, "stage": "mailtm_create", "progress": progress}

    email = mail["email"]
    mail_password = mail["password"]

    step("temp_mail", f"תיבה נוצרה: {email}")

    token = mailtm_get_token(email, mail_password)

    emby_password = generate_password()

    headers = {
        "User-Agent": random_user_agent(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://streamingstreaming.com",
        "Referer": "https://streamingstreaming.com/",
    }

    r = requests.post(
        REG_URL,
        headers=headers,
        data={"email": email, "password": emby_password},
        timeout=15,
    )

    logger.info("reg.php status: %d", r.status_code)
    logger.info("reg.php body: %s", r.text[:200])

    if "error" in r.text.lower():
        return {"success": False, "stage": "registration", "progress": progress}

    step("registration", "נרשם, ממתין למייל הפעלה")

    msg_id = mailtm_wait_for_message(token)
    if not msg_id:
        return {"success": False, "stage": "inbox", "progress": progress}

    mail_data = mailtm_fetch_message(token, msg_id)

    link = extract_activation_link(mail_data)
    if not link:
        return {"success": False, "stage": "activation_link", "progress": progress}

    step("activation", "מפעיל חשבון")

    act = requests.get(link, headers={"User-Agent": random_user_agent()}, timeout=15)

    return {
        "success": act.ok,
        "username": email,
        "password": emby_password,
        "email": email,
        "activation_link": link,
        "progress": progress,
    }

# ==========================
# ROUTES
# ==========================

def is_authenticated():
    return session.get("auth", False)

@app.before_request
def auth_guard():
    if request.endpoint in {"login", "static"}:
        return
    if not is_authenticated():
        return redirect(url_for("login"))

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/register", methods=["POST"])
def api_register():
    result = register_and_activate()
    return jsonify(result), (200 if result.get("success") else 502)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["auth"] = True
            return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ==========================
# RUN
# ==========================

if __name__ == "__main__":
    app.run("0.0.0.0", int(os.environ.get("PORT", 5000)))
