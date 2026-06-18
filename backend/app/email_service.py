"""Transactional email service — CS-3 Section 3 (T5).

Centralizes all outbound transactional email so the auth routes never touch
SendGrid, SES, or template details. Public surface:

    send_email(to, subject, html_body) -> bool
    send_invite_email(to, invite_token, org_name, role) -> bool
    send_welcome_email(to, org_name) -> bool
    send_password_reset_email(to, reset_token) -> bool

Contract (AC14): NOTHING here raises into the caller. Every path returns a bool
— True on a confirmed send, False on any failure (missing/unknown provider,
missing API key, template error, network/provider error) — and logs the cause.
Auth flows must return their normal response even when the email provider is
down, so registration / invitation / reset never crash on a mail outage.

Providers are pluggable behind send_email():
  * SendGrid (default, required by CS-3) — called via its v3 REST API using the
    already-vendored ``requests`` dependency, so no extra SDK is needed.
  * AWS SES — supported through the SAME interface when EMAIL_PROVIDER=ses and
    boto3 is installed/configured (lazy import; absent boto3 => logged False).

Configuration is read from the environment AT CALL TIME (not import time) so one
build works unchanged across local / staging / production and is easy to test:
  EMAIL_PROVIDER (default 'sendgrid'), EMAIL_FROM, EMAIL_FROM_NAME,
  SENDGRID_API_KEY, PUBLIC_HOSTNAME (base URL used to build links).
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

# Defaults mirror CS-3 Section 3.
DEFAULT_FROM = "noreply@cloudfulcrum.com"
DEFAULT_FROM_NAME = "AgentIQ"
DEFAULT_BASE_URL = "http://localhost:3000"

SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_HTTP_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Configuration accessors — read from the environment on every call.
# ---------------------------------------------------------------------------


def _provider() -> str:
    return os.getenv("EMAIL_PROVIDER", "sendgrid").strip().lower()


def _from_email() -> str:
    return os.getenv("EMAIL_FROM", DEFAULT_FROM)


def _from_name() -> str:
    return os.getenv("EMAIL_FROM_NAME", DEFAULT_FROM_NAME)


def _base_url() -> str:
    """Public base URL for links, with any trailing slash removed.

    NOTE: CS-3 Section 3 writes ``f'https://{APP_BASE_URL}/...'`` where
    APP_BASE_URL already carries a scheme — that yields ``https://http://...``.
    We instead use PUBLIC_HOSTNAME verbatim (it already includes the scheme), so
    links are well-formed in every environment.
    """
    return os.getenv("PUBLIC_HOSTNAME", DEFAULT_BASE_URL).rstrip("/")


# ---------------------------------------------------------------------------
# Template rendering (Jinja2)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _jinja_env():
    """Build (once) a Jinja2 environment over app/templates/.

    Jinja2 is imported lazily so a missing install degrades to a logged False in
    the send helpers rather than breaking module import / app startup.
    """
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),  # escape org/role into HTML
    )


def render_template(template_name: str, **context) -> str:
    """Render an HTML email template from app/templates/ with the given context.

    May raise (missing Jinja2, missing template, bad context). Callers are the
    send_* helpers, which catch and convert any failure to a logged False so the
    AC14 no-raise contract holds end to end.
    """
    return _jinja_env().get_template(template_name).render(**context)


# ---------------------------------------------------------------------------
# Transport — send_email() dispatches on EMAIL_PROVIDER. Never raises.
# ---------------------------------------------------------------------------


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send one HTML email. Returns True on success, False on any failure.

    Never raises: an unknown/misconfigured provider, a missing key, or a
    transport exception are all logged and reported as False.
    """
    provider = _provider()
    try:
        if provider == "sendgrid":
            return _send_sendgrid(to, subject, html_body)
        if provider == "ses":
            return _send_ses(to, subject, html_body)
        logger.error("EMAIL_PROVIDER not configured or unknown: %r", provider)
        return False
    except Exception:  # defensive backstop — never propagate to the auth route
        logger.exception("send_email failed for %s (provider=%s)", to, provider)
        return False


def _send_sendgrid(to: str, subject: str, html_body: str) -> bool:
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        logger.error("SENDGRID_API_KEY is not set; cannot email %s", to)
        return False

    import requests  # already a vendored dependency

    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": _from_email(), "name": _from_name()},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }
    resp = requests.post(
        SENDGRID_API_URL,
        json=payload,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    if 200 <= resp.status_code < 300:
        return True
    logger.error(
        "SendGrid send to %s failed: HTTP %s %s", to, resp.status_code, resp.text[:300]
    )
    return False


def _send_ses(to: str, subject: str, html_body: str) -> bool:
    try:
        import boto3  # optional — only when EMAIL_PROVIDER=ses
    except ImportError:
        logger.error(
            "EMAIL_PROVIDER=ses but boto3 is not installed; cannot email %s", to
        )
        return False

    client = boto3.client("ses")
    resp = client.send_email(
        Source=f"{_from_name()} <{_from_email()}>",
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": html_body}},
        },
    )
    if resp.get("MessageId"):
        return True
    logger.error("SES send to %s returned no MessageId: %r", to, resp)
    return False


# ---------------------------------------------------------------------------
# CS-3 transactional emails — build link + subject, render template, send.
# ---------------------------------------------------------------------------


def _render_and_send(to: str, subject: str, template_name: str, **context) -> bool:
    """Render a template and send it. Never raises — a render failure is logged
    and reported as False, just like a transport failure."""
    try:
        html_body = render_template(template_name, **context)
    except Exception:
        logger.exception("Failed to render %s for %s", template_name, to)
        return False
    return send_email(to, subject, html_body)


def send_invite_email(to: str, invite_token: str, org_name: str, role: str) -> bool:
    """Invitation email with an accept-invite link. CS-3 Section 3 / 5."""
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
    """Welcome email sent after a successful registration. CS-3 Section 3."""
    return _render_and_send(
        to,
        f"Welcome to AgentIQ — {org_name}",
        "welcome.html",
        org=org_name,
    )


def send_password_reset_email(to: str, reset_token: str) -> bool:
    """Password-reset email with a reset link. CS-3 Section 3 / 5."""
    link = f"{_base_url()}/reset-password?token={reset_token}"
    return _render_and_send(
        to,
        "Reset your AgentIQ password",
        "reset_password.html",
        link=link,
    )
