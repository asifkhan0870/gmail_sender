
import csv
import io
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone


from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

import sender

BASE_DIR = sender.BASE_DIR

load_dotenv()

app = FastAPI(title="MailDesk")
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)

SESSION_SECRET = os.getenv("MAILDESK_SESSION_SECRET", "")
API_KEY = os.getenv("MAILDESK_API_KEY", "")
WEB_SEND_ENABLED = (
    os.getenv("MAILDESK_ENABLE_SEND", "false").lower() == "true"
)

if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        "Configure MAILDESK_SESSION_SECRET with a random secret "
        "of at least 32 characters."
    )

if len(API_KEY) < 20:
    raise RuntimeError(
        "Configure MAILDESK_API_KEY with a strong access key."
    )

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=True,
    same_site="lax",
    max_age=3600,
)

JOBS = {}
JOBS_LOCK = threading.Lock()


def require_key(key: str | None):
    if not secrets.compare_digest(key or "", API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Invalid dashboard access key.",
        )


@app.get("/", response_class=HTMLResponse)
def index():
    return (
        BASE_DIR / "templates" / "index.html"
    ).read_text(encoding="utf-8")



@app.get("/api/status")
def status(
    access_key: str = "",
    request: Request = None,
):
    header_key = request.headers.get("X-Dashboard-Key", "") if request else ""
    require_key(access_key or header_key)

    return {
        "send_enabled": WEB_SEND_ENABLED,
        "sender_email": sender.current_account(),
        "connected_accounts": sender.connected_accounts(),
        "auth_mode": "web-oauth",
        "stats": sender.current_stats(),
    }

@app.post("/api/connect")
def connect_account(
    request: Request,
    access_key: str = Form(""),
):
    require_key(access_key)

    try:
        authorization_url, state = sender.begin_gmail_oauth()

        request.session["oauth_state"] = state

        return JSONResponse({
            "authorization_url": authorization_url,
        })

    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not start Google authorization: {exc}",
        ) from exc


@app.get("/oauth/callback")
def oauth_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    if error:
        request.session.clear()
        return HTMLResponse(
            "<h2>Google authorization was cancelled.</h2>",
            status_code=400,
        )

    expected_state = request.session.pop("oauth_state", None)

    if not expected_state or not state:
        raise HTTPException(
            status_code=400,
            detail="OAuth session expired. Return to MailDesk and try again.",
        )

    if not secrets.compare_digest(expected_state, state):
        raise HTTPException(
            status_code=400,
            detail="OAuth state verification failed.",
        )

    try:
        email = sender.complete_gmail_oauth(code, state)
        request.session.clear()

        return HTMLResponse(
            "<!doctype html><html><head><title>MailDesk</title></head>"
            "<body style='font-family:sans-serif;max-width:650px;margin:60px auto'>"
            "<h2>Gmail authorization successful</h2>"
            f"<p>Connected account: {email}</p>"
            "<p>Return to the MailDesk dashboard and refresh it.</p>"
            "</body></html>"
        )

    except Exception as exc:
        request.session.clear()
        raise HTTPException(
            status_code=400,
            detail=f"Google authorization failed: {exc}",
        ) from exc


@app.post("/api/validate")
async def validate_csv(
    file: UploadFile = File(...),
    access_key: str = Form(""),
):
    require_key(access_key)

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=400,
            detail="Please upload a .csv file.",
        )

    content = await file.read()

    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="CSV must be smaller than 5 MB.",
        )

    try:
        rows = list(
            csv.DictReader(
                io.StringIO(content.decode("utf-8-sig"))
            )
        )
        result = sender.validate_recipients(rows)

    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    processed = {
        row.get("email", "").strip().lower()
        for row in sender.read_log()
        if row.get("status") in {"SENT", "UNKNOWN"}
    }

    pending = [
        email
        for email in result["eligible"]
        if email not in processed
    ]

    return {
        "filename": file.filename,
        "eligible": result["eligible"],
        "eligible_count": len(result["eligible"]),
        "invalid": result["invalid"],
        "duplicates": result["duplicates"],
        "suppressed": result["suppressed"],
        "previously_processed": len(result["eligible"]) - len(pending),
        "pending": pending,
        "pending_count": len(pending),
        "sample": pending[:5],
    }


class SendRequest(BaseModel):
    recipients: list[str] = Field(min_length=1, max_length=100)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=20000)
    sender_email: str = Field(min_length=3, max_length=254)
    access_key: str = ""
    delay_seconds: int = Field(default=10, ge=5, le=120)
    confirm: bool = False
    consent_confirmed: bool = False


@app.post("/api/send")
def start_send(
    payload: SendRequest,
    background_tasks: BackgroundTasks,
):
    require_key(payload.access_key)

    if not WEB_SEND_ENABLED:
        raise HTTPException(
            status_code=403,
            detail="Live sending is disabled.",
        )

    if not payload.confirm or not payload.consent_confirmed:
        raise HTTPException(
            status_code=400,
            detail="Both confirmations are required.",
        )

    connected = sender.connected_accounts()

    if payload.sender_email.lower() not in {
        email.lower() for email in connected
    }:
        raise HTTPException(
            status_code=409,
            detail="Connect and authorize this Gmail account first.",
        )

    validated = sender.validate_recipients(
        [{"email": item} for item in payload.recipients]
    )

    recipients = validated["eligible"]

    processed = {
        row.get("email", "").strip().lower()
        for row in sender.read_log()
        if row.get("status") in {"SENT", "UNKNOWN"}
    }

    recipients = [
        email for email in recipients
        if email not in processed
    ]

    if not recipients:
        raise HTTPException(
            status_code=400,
            detail="No pending recipients.",
        )

    job_id = uuid.uuid4().hex[:12]

    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sent": 0,
            "errors": 0,
            "skipped": 0,
            "events": [],
            "error": None,
        }

    background_tasks.add_task(
        run_job,
        job_id,
        recipients,
        payload,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "recipient_count": len(recipients),
    }


def run_job(job_id, recipients, payload):
    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"

    try:
        result = sender.send_batch(
            recipients,
            payload.subject,
            payload.body,
            payload.sender_email,
            delay_seconds=payload.delay_seconds,
        )

        with JOBS_LOCK:
            JOBS[job_id].update(result)
            JOBS[job_id]["status"] = "finished"

    except Exception as exc:
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "failed"
            JOBS[job_id]["error"] = str(exc)


@app.get("/api/jobs/{job_id}")
def job_status(
    job_id: str,
    access_key: str = "",
):
    require_key(access_key)

    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if not job:
            raise HTTPException(
                status_code=404,
                detail="Job not found.",
            )

        return dict(job)


@app.get("/api/logs")
def logs(access_key: str = ""):
    require_key(access_key)
    return sender.current_stats()
