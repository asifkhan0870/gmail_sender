
import base64
import csv
import hashlib
import os
import re
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from threading import Lock

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = Path(
    os.getenv("MAILDESK_CREDENTIALS_FILE", "/etc/secrets/credentials.json")
)
REDIRECT_URI = os.getenv(
    "MAILDESK_REDIRECT_URI",
    "https://gmail-sender-01.onrender.com/oauth/callback",
)
TOKENS_DIR = BASE_DIR / "tokens"
LOG_FILE = BASE_DIR / "send_log.csv"
SUPPRESSED_FILE = BASE_DIR / "suppressed.txt"

TOKENS_DIR.mkdir(exist_ok=True)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.metadata",
]

MAX_PER_DAY = 100
PAUSE_SECONDS = 10

LOG_FIELDS = ["timestamp", "email", "status", "detail"]
SEND_LOCK = Lock()

EMAIL_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def token_path_for_email(email):
    email = email.strip().lower()
    if not email:
        raise ValueError("A Gmail address is required.")

    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:24]
    return TOKENS_DIR / f"{digest}.json"


def load_suppressed():
    if not SUPPRESSED_FILE.exists():
        return set()

    return {
        line.strip().lower()
        for line in SUPPRESSED_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def read_log():
    if not LOG_FILE.exists():
        return []

    with LOG_FILE.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        return list(csv.DictReader(file))


def log_result(email, status, detail=""):
    exists = LOG_FILE.exists()

    with LOG_FILE.open(
        "a", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(file, fieldnames=LOG_FIELDS)

        if not exists:
            writer.writeheader()

        writer.writerow({
            "timestamp": timestamp(),
            "email": email,
            "status": status,
            "detail": detail[:300],
        })


def validate_recipients(rows):
    if not rows:
        raise ValueError("The CSV has no data rows.")

    email_column = next(
        (
            key for key in rows[0].keys()
            if key and key.strip().lower() == "email"
        ),
        None,
    )

    if not email_column:
        raise ValueError("CSV must contain an 'email' column.")

    seen = set()
    valid = []
    invalid = 0
    duplicates = 0
    suppressed = 0
    blocked = load_suppressed()

    for row in rows:
        email = (row.get(email_column) or "").strip().lower()

        if not EMAIL_PATTERN.fullmatch(email):
            invalid += 1
            continue

        if email in seen:
            duplicates += 1
            continue

        seen.add(email)

        if email in blocked:
            suppressed += 1
            continue

        valid.append(email)

    return {
        "eligible": valid,
        "invalid": invalid,
        "duplicates": duplicates,
        "suppressed": suppressed,
    }


def get_gmail_profile(creds):
    service = build(
        "gmail",
        "v1",
        credentials=creds,
        cache_discovery=False,
    )

    profile = service.users().getProfile(userId="me").execute()
    return service, profile.get("emailAddress", "")


def _oauth_flow(state=None):
    """Build a Google Web OAuth flow for the hosted Render app."""
    if not CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"OAuth credentials file not found at {CREDENTIALS_FILE}. "
            "Check Render Secret Files and MAILDESK_CREDENTIALS_FILE."
        )

    return Flow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
        state=state,
        redirect_uri=REDIRECT_URI,
    )


def begin_gmail_oauth():
    """Return an authorization URL and state for browser-based OAuth."""
    flow = _oauth_flow()
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
    )
    return authorization_url, state


def complete_gmail_oauth(code, state):
    """Exchange the callback code and save the verified Gmail token.

    The web route must validate state against the state returned by
    begin_gmail_oauth() before calling this function.
    """
    if not code or not state:
        raise ValueError("Google OAuth callback is missing code or state.")

    flow = _oauth_flow(state=state)
    flow.fetch_token(code=code)
    creds = flow.credentials

    _, actual_email = get_gmail_profile(creds)
    if not actual_email:
        raise RuntimeError("Google did not return a Gmail account address.")

    token_file = token_path_for_email(actual_email)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    return actual_email


def get_gmail_service(account_email):
    """Load the token belonging to the selected Gmail account."""
    if not account_email:
        raise ValueError("Select a Gmail account first.")

    token_file = token_path_for_email(account_email)

    if not token_file.exists():
        raise RuntimeError(
            f"{account_email} is not connected. "
            "Click Connect Gmail and authorize it first."
        )

    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError(
                f"Authorization for {account_email} has expired. "
                "Reconnect this account."
            )

    service, actual_email = get_gmail_profile(creds)

    if actual_email.lower() != account_email.strip().lower():
        raise RuntimeError(
            "The authorized Gmail account does not match the selected account."
        )

    return service, actual_email


def connected_accounts():
    """Return Google accounts with saved token files."""
    accounts = []

    for token_file in TOKENS_DIR.glob("*.json"):
        try:
            creds = Credentials.from_authorized_user_file(
                str(token_file), SCOPES
            )

            if not creds.valid:
                if creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                    token_file.write_text(creds.to_json(), encoding="utf-8")
                else:
                    continue

            _, email = get_gmail_profile(creds)
            if email:
                accounts.append(email)

        except Exception:
            continue

    return sorted(set(accounts), key=str.lower)


def current_account():
    """Compatibility helper for the dashboard."""
    accounts = connected_accounts()
    return accounts[0] if len(accounts) == 1 else ""


def send_one(service, recipient, subject, body):
    message = EmailMessage()
    message["To"] = recipient
    message["Subject"] = subject.strip()
    message.set_content(body.strip())

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    result = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    return result.get("id")


def current_stats():
    rows = read_log()
    today = datetime.now().astimezone().date().isoformat()

    return {
        "sent_total": sum(row.get("status") == "SENT" for row in rows),
        "sent_today": sum(
            row.get("status") == "SENT"
            and row.get("timestamp", "").startswith(today)
            for row in rows
        ),
        "failed_total": sum(row.get("status") == "ERROR" for row in rows),
        "unknown_total": sum(row.get("status") == "UNKNOWN" for row in rows),
        "recent": list(reversed(rows[-30:])),
        "daily_cap": MAX_PER_DAY,
    }


def send_batch(
    recipients,
    subject,
    body,
    confirmed_sender,
    delay_seconds=PAUSE_SECONDS,
):
    if not subject.strip() or not body.strip():
        raise ValueError("Subject and message body are required.")

    if not 5 <= delay_seconds <= 120:
        raise ValueError("Pause must be between 5 and 120 seconds.")

    if not SEND_LOCK.acquire(blocking=False):
        raise RuntimeError("Another send job is already running.")

    try:
        service, actual_sender = get_gmail_service(confirmed_sender)

        if actual_sender.lower() != confirmed_sender.lower():
            raise RuntimeError(f"Selected account mismatch: {actual_sender}")

        log_rows = read_log()
        processed = {
            row.get("email", "").strip().lower()
            for row in log_rows
            if row.get("status") in {"SENT", "UNKNOWN"}
        }

        today = datetime.now().astimezone().date().isoformat()
        sent_today = sum(
            row.get("status") == "SENT"
            and row.get("timestamp", "").startswith(today)
            for row in log_rows
        )

        if sent_today >= MAX_PER_DAY:
            raise RuntimeError("The local daily safety cap has been reached.")

        blocked = load_suppressed()
        pending = [
            email.strip().lower()
            for email in recipients
            if email.strip().lower() not in processed
            and email.strip().lower() not in blocked
        ]
        pending = pending[:MAX_PER_DAY - sent_today]

        sent = 0
        errors = 0
        skipped = 0
        events = []

        for index, recipient in enumerate(pending, start=1):
            if recipient in load_suppressed():
                log_result(recipient, "SKIPPED", "Address is on suppression list")
                skipped += 1
                continue

            try:
                message_id = send_one(service, recipient, subject, body)

                if not message_id:
                    log_result(recipient, "UNKNOWN", "No message ID returned")
                    events.append({"email": recipient, "status": "UNKNOWN"})
                    break

                log_result(recipient, "SENT", f"gmail_message_id={message_id}")
                sent += 1
                events.append({"email": recipient, "status": "SENT"})

            except HttpError as exc:
                code = getattr(exc.resp, "status", None)
                log_result(recipient, "ERROR", f"HTTP {code}: {exc}")
                errors += 1
                events.append({
                    "email": recipient,
                    "status": "ERROR",
                    "detail": f"HTTP {code}",
                })

                if code in (401, 403, 429):
                    break

            except Exception as exc:
                log_result(recipient, "UNKNOWN", repr(exc))
                events.append({"email": recipient, "status": "UNKNOWN"})
                break

            if index < len(pending):
                time.sleep(delay_seconds)

        return {
            "sent": sent,
            "errors": errors,
            "skipped": skipped,
            "events": events,
        }

    finally:
        SEND_LOCK.release()
