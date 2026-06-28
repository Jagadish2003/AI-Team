"""
AgentIQ Download Portal
=======================
Hosts the client-deploy package behind an email-based allow-list gate.

Setup:
    pip install -r requirements.txt

    # Required env vars before going public:
    export DOWNLOAD_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
    export SERVER_URL="https://your-public-domain.com"

    # Add authorised client emails to allowed_emails.json, then:
    uvicorn app:app --host 0.0.0.0 --port 8080

allowed_emails.json format:
    {
      "version": "1.0.0",
      "release_date": "2026-06-28",
      "emails": ["client@company.com"]
    }
"""

import base64
import hashlib
import hmac
import io
import json
import pathlib
import tarfile
import time
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from config import SECRET_KEY, SERVER_URL, TOKEN_TTL_HOURS

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR    = pathlib.Path(__file__).parent
DEPLOY_DIR    = SCRIPT_DIR.parent / "client-deploy"
EMAILS_FILE   = SCRIPT_DIR / "allowed_emails.json"
templates     = Jinja2Templates(directory=str(SCRIPT_DIR / "templates"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

# ---------------------------------------------------------------------------
# Allowed-email list helpers
# ---------------------------------------------------------------------------

def _load_email_config() -> dict:
    try:
        return json.loads(EMAILS_FILE.read_text())
    except Exception:
        return {"version": "unknown", "release_date": "-", "emails": []}


def is_allowed(email: str) -> bool:
    cfg = _load_email_config()
    return email.lower() in [e.lower() for e in cfg.get("emails", [])]


def get_version() -> str:
    return _load_email_config().get("version", "1.0.0")


def get_release_date() -> str:
    return _load_email_config().get("release_date", "-")

# ---------------------------------------------------------------------------
# Token helpers  (stateless HMAC — no DB required)
# ---------------------------------------------------------------------------

def make_token(email: str) -> str:
    expiry  = int(time.time()) + TOKEN_TTL_HOURS * 3600
    payload = base64.urlsafe_b64encode(email.encode()).decode() + "|" + str(expiry)
    sig     = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw     = payload + "|" + sig
    return base64.urlsafe_b64encode(raw.encode()).decode()


def verify_token(token: str) -> Optional[str]:
    """Returns the email if token is valid and not expired, else None."""
    try:
        raw             = base64.urlsafe_b64decode(token.encode()).decode()
        email_b64, expiry_str, sig = raw.rsplit("|", 2)
        payload         = email_b64 + "|" + expiry_str
        expected_sig    = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(expiry_str) < time.time():
            return None
        return base64.urlsafe_b64decode(email_b64.encode()).decode()
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Archive builder
# ---------------------------------------------------------------------------

def build_archive() -> bytes:
    """Create an in-memory tar.gz of the client-deploy folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(DEPLOY_DIR, arcname="agentiq-deploy")
    buf.seek(0)
    return buf.read()

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = ""):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "error": error,
        "version": get_version(),
    })


@app.post("/verify", response_class=HTMLResponse)
async def verify(request: Request, email: str = Form(...)):
    email = email.strip().lower()

    if not email or "@" not in email:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "error": "Please enter a valid email address.",
            "version": get_version(),
        })

    if not is_allowed(email):
        return templates.TemplateResponse("index.html", {
            "request": request,
            "error": "This email address is not authorised for this release. "
                     "Contact your AgentIQ representative.",
            "version": get_version(),
        })

    token = make_token(email)
    return RedirectResponse(f"/portal?token={token}", status_code=303)


@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request, token: str = ""):
    email = verify_token(token)
    if not email:
        return RedirectResponse("/?error=Session+expired+or+invalid.+Please+verify+your+email+again.")

    version      = get_version()
    release_date = get_release_date()
    download_url = f"{SERVER_URL}/download?token={token}"
    command      = (
        f'curl -fsSL "{download_url}" -o agentiq-deploy.tar.gz '
        f'&& tar -xzf agentiq-deploy.tar.gz '
        f'&& python3 agentiq-deploy/scripts/deploy-ecr.py'
    )

    return templates.TemplateResponse("portal.html", {
        "request":      request,
        "email":        email,
        "token":        token,
        "version":      version,
        "release_date": release_date,
        "download_url": download_url,
        "command":      command,
        "ttl_hours":    TOKEN_TTL_HOURS,
    })


@app.get("/download")
async def download(token: str = ""):
    email = verify_token(token)
    if not email:
        return RedirectResponse("/?error=Session+expired.+Please+verify+your+email+again.")

    version  = get_version()
    archive  = build_archive()
    filename = f"agentiq-deploy-{version}.tar.gz"

    return StreamingResponse(
        io.BytesIO(archive),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
