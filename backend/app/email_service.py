"""Transactional email service — CS-3 (Section 3).

Sends the three CS-3 transactional emails: invite, welcome, and password reset.

Design contract (CS-3 AC14): **this module never raises.** Every public helper
returns ``True`` on a confirmed send and ``False`` on any failure, logging the
failure. Auth routes call these helpers in a fire-and-forget fashion and return
their normal response regardless of the result — email delivery must never break
an auth flow.

Provider dispatch (``EMAIL_PROVIDER``) supports ``sendgrid`` and ``ses``. The
concrete provider integrations and the branded HTML templates are delivered by
the separate CS-3 email-infrastructure tasks (T5/T6); until a provider is
configured *and* its SDK is installed, :func:`send_email` logs and returns
``False``. That is the correct degraded behaviour here: the route still succeeds,
the failure is logged, and the invite token is still surfaced in non-production
so onboarding is testable without a live mail provider.

Security: never log full tokens, reset links, or message bodies. Recipients are
redacted to their domain in logs (see :func:`_redact`).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read lazily so tests / deploys can set env before first send)
# ---------------------------------------------------------------------------

EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@cloudfulcrum.com")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "AgentIQ")

# Directory holding the branded HTML templates (CS-3 T6). Optional: if a template
# is absent we fall back to a minimal inline body so sends still work pre-T6.
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _email_provider() -> str:
    """Resolve the configured provider at call time, normalised."""
    return os.getenv("EMAIL_PROVIDER", "sendgrid").strip().lower()


def _app_base_url() -> str:
    """Public base URL used to build accept-invite / reset links.

    Mirrors the doc's PUBLIC_HOSTNAME knob. The invite/reset link builders prefix
    this with ``https://`` only when the value has no scheme, so a fully-qualified
    ``http://localhost:5173`` dev value is preserved as-is.
    """
    return os.getenv("PUBLIC_HOSTNAME", "http://localhost:5173")


def _link(path_and_query: str) -> str:
    """Join the app base URL with ``path_and_query`` (which starts with '/')."""
    base = _app_base_url().rstrip("/")
    if "://" not in base:
        base = f"https://{base}"
    return f"{base}{path_and_query}"


def _redact(to: str) -> str:
    """Redact a recipient address for logging — keep the domain, drop the local
    part, so logs never carry a full PII email address."""
    if "@" in to:
        return f"<redacted>@{to.rsplit('@', 1)[1]}"
    return "<redacted>"


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def render_template(template_name: str, **context: object) -> str:
    """Render an HTML email body.

    Reads ``app/templates/<template_name>`` and substitutes ``{key}`` placeholders
    with ``context`` values using ``str.format_map`` (a default dict so an unknown
    placeholder is left intact rather than raising). When the template file is not
    present yet (pre-T6), returns a minimal inline HTML body built from the context
    so callers still produce a valid message.

    Never raises: any rendering error degrades to the inline fallback.
    """
    try:
        path = _TEMPLATE_DIR / template_name
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            return raw.format_map(_SafeDict(context))
    except Exception:  # pragma: no cover - defensive; fall through to inline body
        logger.exception("email template render failed: %s", template_name)
    return _inline_fallback(template_name, context)


class _SafeDict(dict):
    """dict whose missing keys render as the original ``{key}`` placeholder."""

    def __missing__(self, key: str) -> str:  # type: ignore[override]
        return "{" + key + "}"


def _inline_fallback(template_name: str, context: dict) -> str:
    """Minimal, dependency-free HTML body used until branded templates land."""
    link = context.get("link")
    org = context.get("org")
    parts = ["<html><body style=\"font-family:sans-serif\">"]
    parts.append("<h2>AgentIQ</h2>")
    if "invite" in template_name:
        parts.append(f"<p>You have been invited to {org or 'a workspace'} on AgentIQ.</p>")
    elif "welcome" in template_name:
        parts.append(f"<p>Welcome to AgentIQ{f' — {org}' if org else ''}.</p>")
    elif "reset" in template_name:
        parts.append("<p>We received a request to reset your AgentIQ password.</p>")
    if link:
        parts.append(f'<p><a href="{link}">{link}</a></p>')
    parts.append("</body></html>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------


def send_email(to: str, subject: str, html_body: str) -> bool:
    """Send one HTML email. Never raises. Returns True on success, else False.

    Dispatches on ``EMAIL_PROVIDER``. If the provider is unknown / unconfigured
    or its SDK is unavailable, logs and returns False — the caller keeps its
    normal response either way (AC14).
    """
    provider = _email_provider()
    try:
        if provider == "sendgrid":
            return _send_sendgrid(to, subject, html_body)
        if provider == "ses":
            return _send_ses(to, subject, html_body)
        logger.error("EMAIL_PROVIDER not configured or unknown: %r", provider)
        return False
    except Exception:
        # Belt-and-braces: a provider helper should already swallow its own
        # errors, but this guarantees send_email itself never propagates one.
        logger.exception("send_email failed for %s via %s", _redact(to), provider)
        return False


def _send_sendgrid(to: str, subject: str, html_body: str) -> bool:
    """SendGrid provider. The full SendGrid integration is delivered by CS-3 T5.

    Resolves the API key from ``SENDGRID_API_KEY`` and posts via the SendGrid SDK
    if it is installed. Until then this logs and returns False so auth flows keep
    working without a live mail provider.
    """
    api_key = os.getenv("SENDGRID_API_KEY")
    if not api_key:
        logger.warning(
            "SENDGRID_API_KEY not set; skipping email to %s (subject=%r)",
            _redact(to),
            subject,
        )
        return False
    try:
        # Imported lazily: the SendGrid SDK is an optional dependency added with
        # the T5 provider work. Absent it, we degrade rather than crash.
        from sendgrid import SendGridAPIClient  # type: ignore
        from sendgrid.helpers.mail import Mail  # type: ignore
    except ImportError:
        logger.warning(
            "sendgrid SDK not installed; skipping email to %s (subject=%r)",
            _redact(to),
            subject,
        )
        return False

    message = Mail(
        from_email=(EMAIL_FROM, EMAIL_FROM_NAME),
        to_emails=to,
        subject=subject,
        html_content=html_body,
    )
    response = SendGridAPIClient(api_key).send(message)
    ok = 200 <= int(getattr(response, "status_code", 0)) < 300
    if not ok:
        logger.error(
            "SendGrid returned %s sending to %s",
            getattr(response, "status_code", "?"),
            _redact(to),
        )
    return ok


def _send_ses(to: str, subject: str, html_body: str) -> bool:
    """AWS SES provider. Full integration is delivered by CS-3 T5; degrades to
    False (logged) until boto3 + SES config are present."""
    try:
        import boto3  # type: ignore
    except ImportError:
        logger.warning(
            "boto3 not installed; skipping SES email to %s (subject=%r)",
            _redact(to),
            subject,
        )
        return False
    try:
        client = boto3.client("ses")
        client.send_email(
            Source=f"{EMAIL_FROM_NAME} <{EMAIL_FROM}>",
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject},
                "Body": {"Html": {"Data": html_body}},
            },
        )
        return True
    except Exception:
        logger.exception("SES send failed for %s", _redact(to))
        return False


# ---------------------------------------------------------------------------
# Public transactional helpers (called by auth routes)
# ---------------------------------------------------------------------------


def send_invite_email(to: str, invite_token: str, org_name: str | None, role: str) -> bool:
    """Send the 'you've been invited' email with an accept-invite link.

    The link embeds the raw invite token as the ``token`` query param, matching
    the frontend accept-invite page. Never raises (AC14)."""
    link = _link(f"/accept-invite?token={invite_token}")
    org = org_name or "your workspace"
    return send_email(
        to,
        f"You have been invited to {org} on AgentIQ",
        render_template("invite.html", link=link, org=org, role=role),
    )


def send_welcome_email(to: str, org_name: str | None) -> bool:
    """Send the post-registration welcome email. Never raises (AC14)."""
    org = org_name or "AgentIQ"
    return send_email(
        to,
        f"Welcome to AgentIQ — {org}",
        render_template("welcome.html", org=org),
    )


def send_password_reset_email(to: str, reset_token: str) -> bool:
    """Send the password-reset email with a reset link. Never raises (AC14).

    Provided here so the CS-3 forgot/reset-password endpoints can call it; the
    reset token is embedded in the link only and is never logged."""
    link = _link(f"/reset-password?token={reset_token}")
    return send_email(
        to,
        "Reset your AgentIQ password",
        render_template("reset_password.html", link=link),
    )
