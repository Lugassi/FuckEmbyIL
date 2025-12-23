import json
import logging
import os
import random
import re
import string
import time
from pathlib import Path

import requests
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

# ==========================
#  CONFIG
# ==========================

# EmbyIL / streamingstreaming registration endpoint
REG_URL = "https://streamingstreaming.com/reg.php"

# Guerrilla Mail temp mail config
BASE_URL = "https://api.guerrillamail.com/ajax.php"

PASSWORD_CHARSET = string.ascii_letters + string.digits
CONFIG_PATH = Path(os.environ.get("CONFIG_FILE", "config.json"))
_DEFAULT_ADMIN_PASSWORD = "ChangeMeNow!"
_DEFAULT_SECRET = "replace-this-secret"


def load_file_config() -> dict:
    """Load admin/secret settings from config.json if it exists."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
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

# --- Random User-Agent Pool ---
USER_AGENTS = [
    # Chrome Desktop
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",

    # Firefox Desktop
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13.4; rv:121.0) Gecko/20100101 Firefox/121.0",

    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",

    # Safari Desktop
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",

    # Mobile Chrome
    "Mozilla/5.0 (Linux; Android 12; SM-A528B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",

    # Mobile Safari
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]


# ==========================
#  LOGGING
# ==========================

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
logger = logging.getLogger("emby_auto")


# ==========================
#  FLASK APP
# ==========================

app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY


def random_user_agent() -> str:
    ua = random.choice(USER_AGENTS)
    logger.debug("Using User-Agent: %s", ua)
    return ua


def generate_password(length: int = 10) -> str:
    pwd = "".join(random.choice(PASSWORD_CHARSET) for _ in range(length))
    logger.debug("Generated password: %s", pwd)
    return pwd


def generate_temp_email():
    """Generate a new temp email using the Guerrilla Mail API."""
    logger.info("1. מייצר תיבת דואר זמנית חדשה...")

    url = f"{BASE_URL}?f=get_email_address"

    try:
        resp = requests.get(url, timeout=10)
        logger.debug("Temp mail HTTP status: %s", resp.status_code)
        logger.debug("Temp mail raw body: %r", resp.text)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Temp mail creation failed: %s", exc)
        return None

    try:
        result = resp.json()
    except ValueError:
        logger.error("JSON decode failed for temp mail create: %r", resp.text)
        return None

    logger.debug("Temp mail JSON parsed: %r", result)

    if isinstance(result, dict) and "email_addr" in result:
        email_address = result["email_addr"]
        sid_token = result.get("sid_token")

        logger.info("   - Success! Email: %s", email_address)
        return {
            "email": email_address,
            "sid_token": sid_token,
        }

    logger.warning("   - API Error: Unexpected response format")
    return None


def check_inbox(email_address: str, sid_token: str, timeout_seconds=180, interval=10):
    """Poll the temporary mailbox until an email arrives or timeout is reached."""
    logger.info("3. בודק הודעות חדשות עבור %s ...", email_address)

    start_time = time.time()

    attempt = 1
    while time.time() - start_time < timeout_seconds:
        remaining = max(0, int(timeout_seconds - (time.time() - start_time)))
        logger.info(
            "   ניסיון #%d – ממתין למייל (נותרו ~%d שניות) עבור %s",
            attempt,
            remaining,
            email_address,
        )

        # Get messages list
        url = f"{BASE_URL}?f=check_email&sid_token={sid_token}"
        try:
            resp = requests.get(url, timeout=10)
            logger.debug("Inbox HTTP status: %s", resp.status_code)
            logger.debug("Inbox raw: %r", resp.text)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("   - שגיאת HTTP בבדיקת התיבה: %s", exc)
            time.sleep(interval)
            attempt += 1
            continue

        try:
            result = resp.json()
        except ValueError:
            logger.error("   - JSON לא תקין בבדיקת התיבה: %r", resp.text)
            time.sleep(interval)
            attempt += 1
            continue

        logger.debug("Inbox JSON parsed: %r", result)

        messages = result.get("list", [])
        if messages:
            # Fetch full content for each message
            full_messages = []
            for msg in messages:
                msg_id = msg.get("mail_id")
                if msg_id:
                    read_url = f"{BASE_URL}?f=fetch_email&email_id={msg_id}&sid_token={sid_token}"
                    try:
                        read_resp = requests.get(read_url, timeout=10)
                        if read_resp.ok:
                            full_msg = read_resp.json()
                            full_messages.append(full_msg)
                        else:
                            logger.warning("Failed to read message %s", msg_id)
                    except requests.RequestException as exc:
                        logger.error("Error reading message %s: %s", msg_id, exc)
            if full_messages:
                logger.info(
                    "   נמצאו %d הודעות חדשות עבור %s!",
                    len(full_messages),
                    email_address,
                )
                return full_messages

        logger.info("   - אין הודעות עדיין. חוזר לבדוק שוב בעוד %d שניות...", interval)
        time.sleep(interval)
        attempt += 1

    logger.warning("   הזמן לבדיקת התיבה (%d שניות) הסתיים ללא תוצאה.", timeout_seconds)
    return []


# ========= ACTIVATION LINK EXTRACTION ========= #

def extract_activation_link_from_message(msg: dict) -> str | None:
    """Try to extract an activation URL from a single message."""
    text_parts = []
    for key in ("mail_body", "textBody", "htmlBody", "text", "html", "content", "body"):
        if key in msg and isinstance(msg[key], str):
            text_parts.append(msg[key])

    combined = "\n".join(text_parts)
    if not combined:
        return None

    logger.debug("Searching activation link in message content: %r", combined[:500])

    urls = re.findall(r"https?://[^\s\"'>]+", combined)
    if not urls:
        return None

    return urls[0]


def find_activation_link(messages: list) -> str | None:
    """Look through messages for the activation email and return the first link."""
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            logger.warning("Message #%s is not a dict: %r", idx, msg)
            continue

        if idx == 0:
            logger.debug("First message structure: keys=%r, full=%r", list(msg.keys()), msg)

        subject = msg.get("subject", "")
        logger.debug("Examining message #%s subject: %r", idx, subject)

        link = extract_activation_link_from_message(msg)
        if link:
            logger.info("נמצא קישור הפעלה: %s", link)
            return link

    logger.warning("לא נמצא קישור הפעלה בהודעות שנמשכו.")
    return None


# ========= MAIN FLOW: REGISTER + ACTIVATE ========= #

def register_and_activate():
    """
    Flow:

    1. Create temp mail
    2. Register on streamingstreaming.com with that email
    3. Poll inbox for activation mail
    4. Extract activation link
    5. Call activation link
    6. Return details (email, password, activation status, etc.)
    """
    progress: list[dict] = []

    def mark(stage: str, text: str):
        entry = {
            "stage": stage,
            "text": text,
            "timestamp": time.time(),
        }
        progress.append(entry)
        logger.info(text)

    mark("start", "מתחיל תהליך יצירת החשבון והפעלתו...")

    email_info = generate_temp_email()
    if not email_info:
        mark("temp_mail", "יצירת תיבת הדואר הזמנית נכשלה.")
        return {
            "success": False,
            "stage": "temp_mail",
            "message": "יצירת תיבת הדואר הזמנית נכשלה. ודאו שה-API KEY נכון ונסו שוב.",
            "progress": progress,
        }

    email_address = email_info["email"]
    login = email_info.get("login")
    domain = email_info.get("domain")
    sid_token = email_info.get("sid_token")
    password = generate_password()
    mark("temp_mail", f"תיבת המייל נוצרה: {email_address}")

    headers = {
        "Cookie": "_fbp=fb.1.1765446890074.657799820443656124",
        "Origin": "https://streamingstreaming.com",
        "Referer": "https://streamingstreaming.com/",
        "User-Agent": random_user_agent(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "*/*",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    payload = {"email": email_address, "password": password}

    mark("registration", f"שולח בקשת רישום לשרת עבור {email_address}")
    logger.debug("Headers: %r", headers)
    logger.debug("Payload: %r", payload)

    try:
        reg_resp = requests.post(REG_URL, headers=headers, data=payload, timeout=15)
    except requests.RequestException as exc:
        logger.error("שגיאה בשליחת בקשת הרישום: %s", exc)
        mark("registration", "שגיאה בשליחת בקשת הרישום.")
        return {
            "success": False,
            "stage": "registration",
            "message": "הבקשה לרישום נכשלה מול השרת.",
            "details": str(exc),
            "progress": progress,
        }

    reg_body = reg_resp.text.strip()
    reg_snippet = f"{reg_body[:500]}..." if len(reg_body) > 500 else reg_body

    logger.info("reg.php החזיר %s עבור %s", reg_resp.status_code, email_address)
    logger.debug("תוכן תגובת reg.php: %s", reg_body)
    mark("registration", f"סטטוס תגובה: {reg_resp.status_code}. ממתין למייל הפעלה...")

    mark("inbox", "בודק את התיבה לקבלת מייל הפעלה...")
    messages = check_inbox(email_address, sid_token, timeout_seconds=120)

    if not messages:
        mark("inbox", "לא התקבל מייל הפעלה בזמן.")
        return {
            "success": False,
            "stage": "inbox",
            "message": "לא התקבל מייל הפעלה בתיבה הזמנית.",
            "email": email_address,
            "password": password,
            "server_reply": reg_snippet,
            "status_code": reg_resp.status_code,
            "login": login,
            "domain": domain,
            "progress": progress,
        }

    activation_link = find_activation_link(messages)
    if not activation_link:
        mark("activation_link", "לא נמצא קישור הפעלה בהודעות.")
        return {
            "success": False,
            "stage": "activation_link",
            "message": "נמצאו הודעות אך לא אותר קישור הפעלה.",
            "email": email_address,
            "password": password,
            "server_reply": reg_snippet,
            "status_code": reg_resp.status_code,
            "messages": messages,
            "sid_token": sid_token,
            "progress": progress,
        }

    mark("activation_link", "קישור הפעלה אותר. מפעיל את החשבון...")
    activation_status = None
    activation_http_status = None
    activation_error = None
    activation_body = None

    try:
        act_headers = {"User-Agent": random_user_agent(), "Accept": "*/*"}
        act_resp = requests.get(activation_link, headers=act_headers, timeout=15)
        activation_http_status = act_resp.status_code
        activation_status = act_resp.ok
        activation_body = act_resp.text[:500]
        logger.debug("תשובת קישור ההפעלה (%s): %s", activation_http_status, activation_body)
        mark("activation", f"שרת ההפעלה החזיר {activation_http_status}.")
    except requests.RequestException as exc:
        activation_status = False
        activation_error = str(exc)
        logger.error("שגיאה בעת הקלקה על קישור ההפעלה: %s", exc)
        mark("activation", "שגיאה בעת ניסיון ההפעלה.")

    success = reg_resp.ok and bool(activation_status)
    if success:
        mark("done", "החשבון הופעל בהצלחה!")
    else:
        mark("activation", "ההפעלה עדיין לא הצליחה. בדקו את הפרטים.")

    return {
        "success": success,
        "stage": "done" if success else "activation",
        "message": "החשבון נרשם והופעל בהצלחה." if success else "הופיעה בעיה במהלך ההפעלה. בדקו את הפרטים ונסו שוב.",
        "email": email_address,
        "password": password,
        "server_reply": reg_snippet,
        "status_code": reg_resp.status_code,
        "activation_link": activation_link,
        "activation_http_status": activation_http_status,
        "activation_status": activation_status,
        "activation_error": activation_error,
        "activation_body": activation_body,
        "login": login,
        "domain": domain,
        "progress": progress,
    }


# ==========================
#  ROUTES
# ==========================

def is_authenticated() -> bool:
    return session.get("is_authenticated", False)


@app.before_request
def enforce_admin_password():
    """Require the admin password for every route except login and static files."""
    exempt_endpoints = {"login", "logout", "static"}
    endpoint = request.endpoint or ""

    if endpoint in exempt_endpoints or endpoint.startswith("static"):
        return

    if is_authenticated():
        return

    return redirect(url_for("login", next=request.path))


@app.route("/")
def index():
    return render_template("index.html")


def api_register():
    """HTTP endpoint: POST /api/register to create + activate account."""
    result = register_and_activate()
    status_code = 200 if result.get("success") else 502
    return jsonify(result), status_code


app.add_url_rule("/api/register", "api_register", api_register, methods=["POST"])


@app.route("/login", methods=["GET", "POST"])
def login():
    if is_authenticated():
        return redirect(url_for("index"))

    error = None
    next_url = request.args.get("next") or request.form.get("next") or url_for("index")

    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["is_authenticated"] = True
            return redirect(next_url)
        error = "סיסמה שגויה. נסו שוב."

    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def create_app():
    """WSGI entry point."""
    return app


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
    )
