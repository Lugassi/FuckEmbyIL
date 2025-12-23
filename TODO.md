# TODO List for Switching to 1secmail API

- [x] Update constants: Remove Juhe API_KEY and BASE_URL, add new BASE_URL for 1secmail
- [x] Modify generate_temp_email() to use 1secmail genRandomMailbox action
- [x] Modify check_inbox() to use 1secmail getMessages and readMessage actions
- [x] Update extract_activation_link_from_message() to handle textBody and htmlBody keys
- [x] Update register_and_activate() to use login and domain instead of mailbox_id
- [x] Test the changes by running the app and verifying API calls
