# MailDesk multi-account local build

This version stores each authorized Gmail account in a separate token file under `tokens/` and adds a frontend dropdown for choosing which connected account will send.

## Update existing project
1. Stop Uvicorn with Ctrl+C.
2. Back up your existing project folder.
3. Copy these files from this package into your existing `maildesk_app` folder:
   - `app.py`
   - `sender.py`
   - `templates/index.html`
   - `static/app.js`
   - `static/styles.css`
4. Keep `credentials.json`, `send_log.csv`, and `suppressed.txt`.
5. Your existing `token.json` is not used by this multi-account build. You can leave it in place as a backup; do not delete it until the new flow is tested.

## Run locally
```bash
MAILDESK_ENABLE_SEND=true uvicorn app:app --host 127.0.0.1 --port 8000
```

Refresh the dashboard. Click **+ Connect account** for each Google account you control that is listed under Google Cloud OAuth Audience > Test users. In the OAuth window choose the intended account and complete authorization. Each account is stored in `tokens/<hash>.json`. Then choose the sender from the dropdown.

## Notes
- Being listed as a Google OAuth test user only allows that account to authorize the app; each account still needs to complete OAuth.
- This implementation is for a single local operator. Do not expose the app publicly.
- Live sending is disabled unless `MAILDESK_ENABLE_SEND=true`.
- Keep OAuth credentials and token files private; do not commit them to source control.
- A hosted version needs web OAuth callback handling, authentication, secure per-user token storage, persistent job state, and deployment-specific secrets.
