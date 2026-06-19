"""Transactional email service — AUTH-2 / CS-3, AT-355.

Thin wrapper around SMTP (Office 365 / any STARTTLS-capable server). All config
comes from environment variables so no credentials live in code.

Environment variables (see backend/.env.example):
    EMAIL_PROVIDER     'smtp' (only supported value; future: 'sendgrid' etc.)
    SMTP_HOST          e.g. 'smtp.office365.com'
    SMTP_PORT          e.g. '587'
    SMTP_USERNAME      sender auth username
    SMTP_PASSWORD      sender auth password
    SMTP_USE_STARTTLS  'true' / 'false' (default 'true')
    EMAIL_FROM         From address, e.g. 'notifications@cloudfulcrum.com'
    EMAIL_FROM_NAME    Display name, e.g. 'AgentIQ'
    AGENTIQ_BACKEND_URL  Public base URL of the backend API, used to build
                         approve/reject links. Default: 'http://localhost:8000'
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SMTP configuration
# ---------------------------------------------------------------------------

_SMTP_HOST = os.getenv("SMTP_HOST", "smtp.office365.com")
_SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
_SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
_SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
_SMTP_USE_STARTTLS = os.getenv("SMTP_USE_STARTTLS", "true").strip().lower() == "true"
_EMAIL_FROM = os.getenv("EMAIL_FROM", "notifications@cloudfulcrum.com")
_EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "AgentIQ")
_AGENTIQ_BACKEND_URL = os.getenv("AGENTIQ_BACKEND_URL", "http://localhost:8000").rstrip("/")


# ---------------------------------------------------------------------------
# Low-level send helper
# ---------------------------------------------------------------------------


def send_email(to_address: str, subject: str, html_body: str) -> None:
    """Send a single transactional email via SMTP.

    Raises on connection / auth / send failure so callers can log and decide
    whether to surface the error.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{_EMAIL_FROM_NAME} <{_EMAIL_FROM}>"
    msg["To"] = to_address
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(_SMTP_HOST, _SMTP_PORT) as smtp:
        if _SMTP_USE_STARTTLS:
            smtp.starttls()
        if _SMTP_USERNAME and _SMTP_PASSWORD:
            smtp.login(_SMTP_USERNAME, _SMTP_PASSWORD)
        smtp.sendmail(_EMAIL_FROM, to_address, msg.as_string())

    logger.info("Email sent to %s: %s", to_address, subject)


# ---------------------------------------------------------------------------
# AUTH-2 approval emails
# ---------------------------------------------------------------------------


def send_org_approval_request_email(
    *,
    admin_email: str,
    org_name: str,
    registrant_email: str,
    approval_token: str,
    org_id: str,
) -> None:
    """Send the pending-approval notification to the CloudFulcrum admin inbox.

    Builds approve and reject links pointing to the backend API. The links
    embed the raw token (not its hash) — the endpoint hashes it on receipt.
    """
    approve_url = (
        f"{_AGENTIQ_BACKEND_URL}/api/auth/org-approval/approve"
        f"?token={approval_token}&org_id={org_id}"
    )
    reject_url = (
        f"{_AGENTIQ_BACKEND_URL}/api/auth/org-approval/reject"
        f"?token={approval_token}&org_id={org_id}"
    )

    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html_body = _render_approval_request(
        org_name=org_name,
        registrant_email=registrant_email,
        submitted_at=submitted_at,
        approve_url=approve_url,
        reject_url=reject_url,
    )

    subject = f"New AgentIQ organisation pending approval: {org_name}"
    send_email(admin_email, subject, html_body)


# ---------------------------------------------------------------------------
# Inline HTML templates
# ---------------------------------------------------------------------------


def _render_approval_request(
    *,
    org_name: str,
    registrant_email: str,
    submitted_at: str,
    approve_url: str,
    reject_url: str,
) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Org Approval Request</title></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#333">
  <h2 style="color:#1a1a2e">New AgentIQ Organisation Pending Approval</h2>
  <p>A new organisation has registered and is awaiting your approval:</p>
  <table style="border-collapse:collapse;width:100%;margin:16px 0">
    <tr><td style="padding:8px;font-weight:bold;width:140px">Organisation</td>
        <td style="padding:8px">{_escape(org_name)}</td></tr>
    <tr style="background:#f9f9f9">
        <td style="padding:8px;font-weight:bold">Registrant</td>
        <td style="padding:8px">{_escape(registrant_email)}</td></tr>
    <tr><td style="padding:8px;font-weight:bold">Submitted</td>
        <td style="padding:8px">{_escape(submitted_at)}</td></tr>
  </table>
  <p style="margin:24px 0">
    <a href="{approve_url}" style="background:#22c55e;color:#fff;padding:12px 24px;
       text-decoration:none;border-radius:4px;margin-right:12px;display:inline-block">
      ✓ Approve this organisation
    </a>
    <a href="{reject_url}" style="background:#ef4444;color:#fff;padding:12px 24px;
       text-decoration:none;border-radius:4px;display:inline-block">
      ✗ Reject this organisation
    </a>
  </p>
  <p style="color:#666;font-size:13px">This link expires in 7 days.</p>
  <p style="color:#666;font-size:12px">
    If you did not expect this email, ignore it — no action is required.
  </p>
</body>
</html>"""


def send_org_approved_email(*, registrant_email: str, org_name: str) -> None:
    """Send the approval-confirmation email to the registrant (T4 / AT-355).

    Called by the approve endpoint after the org is set to 'active'. The
    registrant can now log in with the credentials they registered with.
    """
    html_body = _render_org_approved(org_name=org_name)
    send_email(
        registrant_email,
        f"Your AgentIQ organisation has been approved: {org_name}",
        html_body,
    )


def send_org_rejected_email(*, registrant_email: str, org_name: str) -> None:
    """Send the rejection email to the registrant (T4 / AT-355).

    Called by the reject endpoint after the org is set to 'rejected'.
    """
    html_body = _render_org_rejected(org_name=org_name)
    send_email(
        registrant_email,
        f"Your AgentIQ organisation registration was not approved: {org_name}",
        html_body,
    )


def _render_org_approved(*, org_name: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Organisation Approved</title></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#333">
  <h2 style="color:#15803d">Your AgentIQ Organisation Has Been Approved</h2>
  <p>Great news! <strong>{_escape(org_name)}</strong> has been approved.</p>
  <p>You can now log in with the email address and password you registered with.</p>
  <p style="color:#666;font-size:12px">
    If you did not register for AgentIQ, please contact
    <a href="mailto:agentiqadmin@dwpglobal.com">agentiqadmin@dwpglobal.com</a>.
  </p>
</body>
</html>"""


def _render_org_rejected(*, org_name: str) -> str:
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Organisation Registration Not Approved</title></head>
<body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#333">
  <h2 style="color:#b91c1c">Organisation Registration Not Approved</h2>
  <p>We are unable to approve the registration for
     <strong>{_escape(org_name)}</strong> at this time.</p>
  <p>If you believe this is an error, please contact your CloudFulcrum representative.</p>
  <p style="color:#666;font-size:12px">
    Contact: <a href="mailto:agentiqadmin@dwpglobal.com">agentiqadmin@dwpglobal.com</a>
  </p>
</body>
</html>"""


def _escape(value: str) -> str:
    """Minimal HTML escaping for user-supplied values in email templates."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
