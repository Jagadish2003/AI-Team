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


def send_org_approval_request_email(
    *,
    admin_email: str,
    org_name: str,
    registrant_email: str,
    approval_token: str,
    org_id: str,
) -> bool:
    """Send the pending-approval request to the CloudFulcrum admin inbox."""
    approve_url = (
        f"{_backend_url()}/api/auth/org-approval/approve"
        f"?token={approval_token}&org_id={org_id}"
    )
    reject_url = (
        f"{_backend_url()}/api/auth/org-approval/reject"
        f"?token={approval_token}&org_id={org_id}"
    )

    submitted_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Org Approval Request</title></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#333">
  <h2>New AgentIQ Organisation Pending Approval</h2>
  <p>A new organisation has registered and is awaiting approval:</p>
  <table style="border-collapse:collapse;width:100%;margin:16px 0">
    <tr><td style="padding:8px;font-weight:bold">Organisation</td><td style="padding:8px">{_escape(org_name)}</td></tr>
    <tr><td style="padding:8px;font-weight:bold">Registrant</td><td style="padding:8px">{_escape(registrant_email)}</td></tr>
    <tr><td style="padding:8px;font-weight:bold">Submitted</td><td style="padding:8px">{_escape(submitted_at)}</td></tr>
  </table>
  <p>
    <a href="{approve_url}" style="background:#15803d;color:#fff;padding:10px 16px;text-decoration:none;border-radius:4px">Approve this organisation</a>
    <a href="{reject_url}" style="background:#b91c1c;color:#fff;padding:10px 16px;text-decoration:none;border-radius:4px;margin-left:8px">Reject this organisation</a>
  </p>
  <p style="color:#666;font-size:13px">This link expires in 7 days.</p>
</body>
</html>"""
    return send_email(
        admin_email,
        f"New AgentIQ organisation pending approval: {org_name}",
        html_body,
    )


def send_org_approved_email(*, registrant_email: str, org_name: str) -> bool:
    """Send approval-confirmation email to the registrant."""
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Organisation Approved</title></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#333">
  <h2>Your AgentIQ Organisation Has Been Approved</h2>
  <p><strong>{_escape(org_name)}</strong> has been approved.</p>
  <p>You can now log in with the email address and password you registered with.</p>
</body>
</html>"""
    return send_email(
        registrant_email,
        f"Your AgentIQ organisation has been approved: {org_name}",
        html_body,
    )


def send_org_rejected_email(*, registrant_email: str, org_name: str) -> bool:
    """Send rejection email to the registrant."""
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Organisation Registration Not Approved</title></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#333">
  <h2>Organisation Registration Not Approved</h2>
  <p>We are unable to approve the registration for <strong>{_escape(org_name)}</strong> at this time.</p>
  <p>If you believe this is an error, please contact your CloudFulcrum representative.</p>
</body>
</html>"""
    return send_email(
        registrant_email,
        f"Your AgentIQ organisation registration was not approved: {org_name}",
        html_body,
    )


def _escape(value: object) -> str:
    """Minimal HTML escaping for inline AUTH-2 email bodies."""
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
