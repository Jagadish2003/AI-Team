"""Transactional email service for CS-3.

This module centralizes outbound account emails for registration, invites, and
password reset. The current production transport is SMTP over Office 365.

Public surface:
    send_email(to, subject, html_body) -> bool
    send_invite_email(to, invite_token, org_name, role) -> bool
    send_welcome_email(to, org_name) -> bool
    send_password_reset_email(to, reset_token) -> bool
    send_org_approval_request_email(...) -> bool
    send_org_approved_email(...) -> bool
    send_org_rejected_email(...) -> bool

Contract: this module never raises into auth routes. It returns True on a
confirmed send and False on missing configuration, template errors, or SMTP
transport errors. Auth flows must continue even if mail delivery is down.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_FROM = "noreply@cloudfulcrum.com"
DEFAULT_FROM_NAME = "AgentIQ"
DEFAULT_BASE_URL = "http://localhost:3000"
DEFAULT_SMTP_PORT = 587

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_SMTP_TIMEOUT_SECONDS = 10


def _provider() -> str:
    return os.getenv("EMAIL_PROVIDER", "smtp").strip().lower()


def _from_email() -> str:
    return os.getenv("EMAIL_FROM", DEFAULT_FROM).strip()


def _from_name() -> str:
    return os.getenv("EMAIL_FROM_NAME", DEFAULT_FROM_NAME).strip()


def _base_url() -> str:
    """Public frontend URL used for invite and reset-password links."""
    return os.getenv("PUBLIC_HOSTNAME", DEFAULT_BASE_URL).rstrip("/")


def _backend_url() -> str:
    """Public backend URL used for org approval action links."""
    return os.getenv("AGENTIQ_BACKEND_URL", "http://localhost:8000").rstrip("/")


@lru_cache(maxsize=1)
def _jinja_env():
    """Build the Jinja2 environment over app/templates/."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )


def render_template(template_name: str, **context) -> str:
    """Render an HTML email template from app/templates/."""
    return _jinja_env().get_template(template_name).render(**context)


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send one HTML email through SMTP. Never raises to callers."""
    provider = _provider()
    try:
        # Test / local-dev transports that never contact a real mail server.
        if provider in {"noop", "none", "disabled"}:
            logger.debug(
                "email suppressed (provider=%s) to=%s subject=%s", provider, to, subject
            )
            return True
        if provider == "console":
            logger.info("EMAIL (console) to=%s subject=%s\n%s", to, subject, html_body)
            return True
        if provider not in {"smtp", "office365", "smtp.office365.com"}:
            logger.error("EMAIL_PROVIDER not configured or unknown: %r", provider)
            return False
        return _send_smtp(to, subject, html_body)
    except Exception:
        logger.exception("send_email failed for %s (provider=%s)", to, provider)
        return False


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _smtp_host(provider: str) -> str:
    configured = os.getenv("SMTP_HOST", "").strip()
    if configured:
        return configured
    # Support the exact IT handoff style where the Office 365 host is put in
    # EMAIL_PROVIDER instead of SMTP_HOST.
    if "." in provider:
        return provider
    return ""


def _smtp_port() -> int:
    raw = os.getenv("SMTP_PORT", str(DEFAULT_SMTP_PORT)).strip()
    try:
        return int(raw)
    except ValueError:
        logger.error("SMTP_PORT must be an integer; got %r", raw)
        return 0


def _send_smtp(to: str, subject: str, html_body: str) -> bool:
    provider = _provider()
    host = _smtp_host(provider)
    port = _smtp_port()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    from_email = _from_email()

    if not host:
        logger.error("SMTP_HOST is not set; cannot email %s", to)
        return False
    if port <= 0:
        return False
    if not username:
        logger.error("SMTP_USERNAME is not set; cannot email %s", to)
        return False
    if not password:
        logger.error("SMTP_PASSWORD is not set; cannot email %s", to)
        return False
    if not from_email:
        logger.error("EMAIL_FROM is not set; cannot email %s", to)
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((_from_name(), from_email))
    message["To"] = to
    message.set_content("This message contains HTML content.")
    message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS) as smtp:
        if _env_truthy("SMTP_USE_STARTTLS", default=True):
            smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(message)
    return True


def _render_and_send(to: str, subject: str, template_name: str, **context) -> bool:
    """Render a template and send it. Never raises."""
    try:
        html_body = render_template(template_name, **context)
    except Exception:
        logger.exception("Failed to render %s for %s", template_name, to)
        return False
    return send_email(to, subject, html_body)


def send_invite_email(to: str, invite_token: str, org_name: str, role: str) -> bool:
    """Invitation email with an accept-invite link."""
    link = f"{_base_url()}/accept-invite?token={invite_token}"
    return _render_and_send(
        to,
        f"You have been invited to {org_name} on AgentIQ",
        "invite.html",
        link=link,
        org=org_name,
        role=role,
    )


def send_welcome_email(to: str, org_name: str) -> bool:
    """Welcome email sent after successful registration."""
    return _render_and_send(
        to,
        f"Welcome to AgentIQ - {org_name}",
        "welcome.html",
        org=org_name,
    )


def send_password_reset_email(to: str, reset_token: str) -> bool:
    """Password-reset email with a reset link."""
    link = f"{_base_url()}/reset-password?token={reset_token}"
    return _render_and_send(
        to,
        "Reset your AgentIQ password",
        "reset_password.html",
        link=link,
    )


# ---------------------------------------------------------------------------
# AUTH-2 org approval emails
# ---------------------------------------------------------------------------


# Approval links live longer than invite tokens (72h) because the CloudFulcrum
# admin may not check the shared inbox daily. Mirrors
# user_auth.APPROVAL_TOKEN_EXPIRY_DAYS — kept here so the email copy is correct
# even though the token itself is minted in user_auth.
APPROVAL_TOKEN_EXPIRY_DAYS = 7


def send_org_approval_request_email(
    *,
    admin_email: str,
    org_name: str,
    registrant_email: str,
    approval_token: str,
    org_id: str,
) -> bool:
    """Send the pending-approval request to the CloudFulcrum admin inbox.

    Renders org_approval_request.html with the org name, registrant email, a
    UTC submission timestamp, and distinct approve/reject links. Each link
    carries the signed approval token and org_id as query parameters and is
    valid for APPROVAL_TOKEN_EXPIRY_DAYS days.
    """
    approve_url = (
        f"{_backend_url()}/api/auth/org-approval/approve"
        f"?token={approval_token}&org_id={org_id}"
    )
    reject_url = (
        f"{_backend_url()}/api/auth/org-approval/reject"
        f"?token={approval_token}&org_id={org_id}"
    )
    submitted_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return _render_and_send(
        admin_email,
        f"New AgentIQ organisation pending approval: {org_name}",
        "org_approval_request.html",
        org_name=org_name,
        registrant_email=registrant_email,
        submitted_at=submitted_at,
        approve_url=approve_url,
        reject_url=reject_url,
        expiry_days=APPROVAL_TOKEN_EXPIRY_DAYS,
    )


def send_org_approved_email(*, registrant_email: str, org_name: str) -> bool:
    """Send the approval-confirmation email to the registrant (org_approved.html)."""
    return _render_and_send(
        registrant_email,
        f"Your AgentIQ organisation has been approved: {org_name}",
        "org_approved.html",
        org_name=org_name,
        login_url=f"{_base_url()}/login",
    )


def send_org_rejected_email(*, registrant_email: str, org_name: str) -> bool:
    """Send the rejection email to the registrant (org_rejected.html)."""
    return _render_and_send(
        registrant_email,
        f"Your AgentIQ organisation registration was not approved: {org_name}",
        "org_rejected.html",
        org_name=org_name,
    )
