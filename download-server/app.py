"""
AgentIQ Download Portal
=======================
Hosts the client-deploy package behind an email allow-list gate, with an
admin dashboard for managing access and license keys.

Setup:
    pip install -r requirements.txt
    export DOWNLOAD_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
    export SERVER_URL="https://your-public-domain.com"
    export ADMIN_USERNAME="admin"
    export ADMIN_PASSWORD="strong-password-here"
    uvicorn app:app --host 0.0.0.0 --port 8080
"""

import base64
import hashlib
import hmac
import io
import pathlib
import secrets
import tarfile
import time
from typing import Optional

import db
from config import (
    ADMIN_PASSWORD,
    ADMIN_SESSION_TTL_HOURS,
    ADMIN_USERNAME,
    PASSCODE_REVEAL_TTL_MINUTES,
    SECRET_KEY,
    SERVER_URL,
    TOKEN_TTL_HOURS,
)
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

SCRIPT_DIR   = pathlib.Path(__file__).parent
DEPLOY_DIR   = SCRIPT_DIR.parent / "client-deploy"
templates    = Jinja2Templates(directory=str(SCRIPT_DIR / "templates"))

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
def startup() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# Generic HMAC token helpers
# ---------------------------------------------------------------------------

def _make_token(payload: str, ttl_seconds: int) -> str:
    expiry  = int(time.time()) + ttl_seconds
    raw     = base64.urlsafe_b64encode(payload.encode()).decode() + "|" + str(expiry)
    sig     = hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode((raw + "|" + sig).encode()).decode()


def _verify_token(token: str) -> Optional[str]:
    """Return the original payload string if valid and not expired, else None."""
    try:
        decoded      = base64.urlsafe_b64decode(token.encode()).decode()
        payload_b64, expiry_str, sig = decoded.rsplit("|", 2)
        raw          = payload_b64 + "|" + expiry_str
        expected_sig = hmac.new(SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        if int(expiry_str) < time.time():
            return None
        return base64.urlsafe_b64decode(payload_b64.encode()).decode()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Download token  (payload = email)
# ---------------------------------------------------------------------------

def make_download_token(email: str) -> str:
    return _make_token(email, TOKEN_TTL_HOURS * 3600)


def verify_download_token(token: str) -> Optional[str]:
    return _verify_token(token)


# ---------------------------------------------------------------------------
# Admin session cookie  (payload = "admin")
# ---------------------------------------------------------------------------

ADMIN_COOKIE = "aiq_admin"


def make_admin_token() -> str:
    return _make_token("admin", ADMIN_SESSION_TTL_HOURS * 3600)


def is_admin(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE, "")
    return _verify_token(token) == "admin"


def require_admin(request: Request) -> Optional[Response]:
    if not is_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


# ---------------------------------------------------------------------------
# Passcode-reveal token  (payload = "email|passcode")
# ---------------------------------------------------------------------------

def make_reveal_token(email: str, passcode: str) -> str:
    return _make_token(f"{email}|{passcode}", PASSCODE_REVEAL_TTL_MINUTES * 60)


def verify_reveal_token(token: str) -> Optional[tuple]:
    """Returns (email, passcode) or None."""
    payload = _verify_token(token)
    if not payload or "|" not in payload:
        return None
    email, _, passcode = payload.partition("|")
    return email, passcode


# ---------------------------------------------------------------------------
# Archive builder
# ---------------------------------------------------------------------------

def build_archive() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(DEPLOY_DIR, arcname="agentiq-deploy")
    buf.seek(0)
    return buf.read()


# ===========================================================================
# Public routes — client download gate
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, error: str = ""):
    return templates.TemplateResponse("index.html", {
        "request": request, "error": error,
    })


@app.post("/verify", response_class=HTMLResponse)
async def verify(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    if not email or "@" not in email:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "error": "Please enter a valid email address.",
        })
    if not db.is_email_allowed(email):
        return templates.TemplateResponse("index.html", {
            "request": request,
            "error": "This email is not authorised for this release. "
                     "Contact your AgentIQ representative.",
        })
    token = make_download_token(email)
    return RedirectResponse(f"/portal?token={token}", status_code=303)


@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request, token: str = ""):
    email = verify_download_token(token)
    if not email:
        return RedirectResponse("/?error=Session+expired.+Please+verify+your+email+again.")
    download_url = f"{SERVER_URL}/download?token={token}"
    command = (
        f'curl -fsSL "{download_url}" -o agentiq-deploy.tar.gz '
        f'&& tar -xzf agentiq-deploy.tar.gz '
        f'&& python3 agentiq-deploy/scripts/deploy-ecr.py'
    )
    return templates.TemplateResponse("portal.html", {
        "request":      request,
        "email":        email,
        "token":        token,
        "download_url": download_url,
        "command":      command,
        "ttl_hours":    TOKEN_TTL_HOURS,
    })


@app.get("/download")
async def download(token: str = ""):
    email = verify_download_token(token)
    if not email:
        return RedirectResponse("/?error=Session+expired.+Please+verify+your+email+again.")
    archive  = build_archive()
    filename = "agentiq-deploy.tar.gz"
    return StreamingResponse(
        io.BytesIO(archive),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===========================================================================
# Public routes — client license retrieval
# ===========================================================================

@app.get("/license", response_class=HTMLResponse)
async def license_page(request: Request, error: str = ""):
    return templates.TemplateResponse("license.html", {
        "request": request, "error": error, "license_key": None,
    })


@app.post("/license", response_class=HTMLResponse)
async def license_lookup(
    request: Request,
    email: str = Form(...),
    passcode: str = Form(...),
):
    email   = email.strip().lower()
    passcode = passcode.strip()
    if not email or not passcode:
        return templates.TemplateResponse("license.html", {
            "request": request,
            "error": "Both email and passcode are required.",
            "license_key": None,
        })
    key = db.verify_license(email, passcode)
    if not key:
        return templates.TemplateResponse("license.html", {
            "request": request,
            "error": "Email or passcode is incorrect.",
            "license_key": None,
        })
    return templates.TemplateResponse("license.html", {
        "request":     request,
        "error":       "",
        "license_key": key,
        "email":       email,
    })


# ===========================================================================
# Admin routes
# ===========================================================================

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, error: str = ""):
    if is_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse("admin_login.html", {
        "request": request, "error": error,
    })


@app.post("/admin/login")
async def admin_login_post(
    response: Response,
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        token = make_admin_token()
        resp  = RedirectResponse("/admin", status_code=303)
        resp.set_cookie(
            ADMIN_COOKIE, token,
            httponly=True, samesite="strict", secure=False,
            max_age=ADMIN_SESSION_TTL_HOURS * 3600,
        )
        return resp
    return templates.TemplateResponse("admin_login.html", {
        "request": request,
        "error":   "Incorrect username or password.",
    })


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(ADMIN_COOKIE)
    return resp


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, msg: str = "", reveal: str = ""):
    guard = require_admin(request)
    if guard:
        return guard

    reveal_data = None
    if reveal:
        result = verify_reveal_token(reveal)
        if result:
            reveal_data = {"email": result[0], "passcode": result[1]}

    return templates.TemplateResponse("admin.html", {
        "request":     request,
        "clients":     db.list_clients(),
        "licenses":    db.list_licenses(),
        "msg":         msg,
        "reveal_data": reveal_data,
    })


# -- Client management -------------------------------------------------------

@app.post("/admin/clients/add")
async def admin_add_client(
    request: Request,
    email:       str = Form(...),
    expiry_date: str = Form(""),
):
    guard = require_admin(request)
    if guard:
        return guard
    try:
        db.add_client(email.strip(), expiry_date.strip() or None)
    except Exception as e:
        return RedirectResponse(f"/admin?msg=Error:+{e}", status_code=303)
    return RedirectResponse("/admin?msg=Client+added.", status_code=303)


@app.post("/admin/clients/{client_id}/update")
async def admin_update_client(
    request: Request,
    client_id:   int,
    email:       str = Form(...),
    expiry_date: str = Form(""),
    is_active:   int = Form(1),
):
    guard = require_admin(request)
    if guard:
        return guard
    db.update_client(client_id, email.strip(), expiry_date.strip() or None, is_active)
    return RedirectResponse("/admin?msg=Client+updated.", status_code=303)


@app.post("/admin/clients/{client_id}/delete")
async def admin_delete_client(request: Request, client_id: int):
    guard = require_admin(request)
    if guard:
        return guard
    db.delete_client(client_id)
    return RedirectResponse("/admin?msg=Client+removed.", status_code=303)


# -- License management ------------------------------------------------------

@app.post("/admin/licenses/add")
async def admin_add_license(
    request:     Request,
    email:       str = Form(...),
    license_key: str = Form(...),
    passcode:    str = Form(...),
):
    guard = require_admin(request)
    if guard:
        return guard
    if not email or not license_key or not passcode:
        return RedirectResponse("/admin?msg=All+fields+required.", status_code=303)
    db.add_license(email.strip(), license_key.strip(), passcode.strip())
    reveal_token = make_reveal_token(email.strip().lower(), passcode.strip())
    return RedirectResponse(f"/admin?reveal={reveal_token}", status_code=303)


@app.post("/admin/licenses/{license_id}/delete")
async def admin_delete_license(request: Request, license_id: int):
    guard = require_admin(request)
    if guard:
        return guard
    db.delete_license(license_id)
    return RedirectResponse("/admin?msg=License+removed.", status_code=303)
