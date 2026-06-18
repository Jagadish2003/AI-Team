"""Transactional email seam — CS-3 (forgot/reset-password).

This module is the dispatch point for the password-reset email. It has NO
external mailer dependency on this branch: a real SendGrid/SES integration is
delivered by the separate CS-3 email-infrastructure task. Until then,
send_password_reset_email logs a non-sensitive line and returns, so the auth
flow works offline and tests have a stable function to monkeypatch.

Security: never log the raw token, the reset link, or the full recipient
address. The reset endpoint relies on this — a leak here would expose usable
reset links straight from application logs.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Public base URL used to build the reset link. Mirrors the doc's PUBLIC_HOSTNAME
# knob; the frontend reset-password page reads the token from this link.
_APP_BASE_URL = "PUBLIC_HOSTNAME"


def _redact(to: str) -> str:
    """Redact a recipient for logging — keep the domain, drop the local part."""
    if "@" in to:
        return f"<redacted>@{to.rsplit('@', 1)[1]}"
    return "<redacted>"


def _reset_link(reset_token: str) -> str:
    base = os.getenv(_APP_BASE_URL, "http://localhost:5173").rstrip("/")
    if "://" not in base:
        base = f"https://{base}"
    return f"{base}/reset-password?token={reset_token}"


def send_password_reset_email(email: str, reset_token: str) -> None:
    """Dispatch the password-reset email. Must never raise.

    On this branch there is no live mailer, so this builds the reset link
    (consumed by a real provider once wired) and logs only a redacted, tokenless
    line. The forgot-password route calls this in a try/except regardless, so a
    transport failure here can never change the route's response or leak whether
    the email is registered.
    """
    # Build the link so the call shape matches the eventual real implementation;
    # do NOT log it (it carries the token).
    _ = _reset_link(reset_token)
    logger.info("password reset email queued for %s", _redact(email))
