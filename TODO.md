# TODO List for Switching to Guerrilla Mail API

- [x] Update constants: Remove Juhe API_KEY and BASE_URL, add new BASE_URL for Guerrilla Mail
- [x] Modify generate_temp_email() to use Guerrilla Mail get_email_address action
- [x] Modify check_inbox() to use Guerrilla Mail check_email and fetch_email actions
- [x] Update extract_activation_link_from_message() to handle mail_body key
- [x] Update register_and_activate() to use sid_token instead of mailbox_id
- [x] Test the changes by running the app and verifying API calls
