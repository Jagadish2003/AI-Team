"""Unit tests for CS-3 — app/email_service.py (T5) + templates (T6).

Acceptance:
  AC14 — email_service never raises into the caller. Every failure path
          (unknown provider, missing API key, transport exception, template
          render error) returns False and logs the cause; success returns True.

Also covers the CS-3 helpers: each builds the right link + subject, renders its
template, and delegates to send_email. No network is touched — requests.post and
send_email are monkeypatched.
"""
from __future__ import annotations

import logging

import pytest

from app import email_service


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text


# ── send_email: provider dispatch, success, and failure (AC14) ────────────────


def test_send_email_returns_true_on_sendgrid_2xx(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")
    monkeypatch.setenv("EMAIL_FROM", "noreply@dwp.example")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp(202)

    monkeypatch.setattr("requests.post", fake_post)

    assert email_service.send_email("user@example.com", "Hi", "<p>hi</p>") is True
    assert captured["url"] == email_service.SENDGRID_API_URL
    assert captured["headers"]["Authorization"] == "Bearer SG.test-key"
    assert captured["timeout"] == email_service._HTTP_TIMEOUT_SECONDS
    p = captured["json"]
    assert p["personalizations"][0]["to"][0]["email"] == "user@example.com"
    assert p["from"]["email"] == "noreply@dwp.example"
    assert p["subject"] == "Hi"
    assert p["content"][0]["value"] == "<p>hi</p>"


def test_send_email_returns_false_on_sendgrid_non_2xx(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(401, "unauthorized"))

    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        assert email_service.send_email("user@example.com", "s", "<p>b</p>") is False
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_send_email_false_when_api_key_missing(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        assert email_service.send_email("user@example.com", "s", "<p>b</p>") is False
    assert any("SENDGRID_API_KEY" in r.getMessage() for r in caplog.records)


def test_send_email_false_for_unknown_provider(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_PROVIDER", "carrier-pigeon")
    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        assert email_service.send_email("user@example.com", "s", "<p>b</p>") is False
    assert any("EMAIL_PROVIDER" in r.getMessage() for r in caplog.records)


def test_send_email_never_raises_on_transport_exception(monkeypatch, caplog):
    """AC14: a provider/network blow-up is swallowed into a logged False."""
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.test-key")

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.post", boom)
    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        result = email_service.send_email("user@example.com", "s", "<p>b</p>")
    assert result is False  # did not raise
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_ses_provider_without_boto3_returns_false(monkeypatch, caplog):
    """EMAIL_PROVIDER=ses degrades to a logged False when boto3 is absent."""
    monkeypatch.setenv("EMAIL_PROVIDER", "ses")
    # boto3 is not a dependency; the lazy import fails -> False (no raise).
    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        assert email_service.send_email("user@example.com", "s", "<p>b</p>") is False


# ── render_template (Jinja2) ──────────────────────────────────────────────────


def test_send_email_returns_true_on_smtp_office365(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.office365.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "notifications@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-from-env")
    monkeypatch.setenv("SMTP_USE_STARTTLS", "true")
    monkeypatch.setenv("EMAIL_FROM", "notifications@example.com")
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            captured["closed"] = True

        def starttls(self):
            captured["starttls"] = True

        def login(self, username, password):
            captured.update(username=username, password=password)

        def send_message(self, message):
            captured["message"] = message

    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)

    assert email_service.send_email("user@example.com", "Hi", "<p>hi</p>") is True
    assert captured["host"] == "smtp.office365.com"
    assert captured["port"] == 587
    assert captured["timeout"] == email_service._SMTP_TIMEOUT_SECONDS
    assert captured["starttls"] is True
    assert captured["username"] == "notifications@example.com"
    assert captured["password"] == "secret-from-env"
    assert captured["message"]["To"] == "user@example.com"
    assert captured["message"]["From"] == "AgentIQ <notifications@example.com>"
    assert "<p>hi</p>" in captured["message"].get_body(("html",)).get_content()


def test_send_email_false_when_smtp_password_missing(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.office365.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "notifications@example.com")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        assert email_service.send_email("user@example.com", "s", "<p>b</p>") is False
    assert any("SMTP_PASSWORD" in r.getMessage() for r in caplog.records)


def test_send_email_accepts_office365_host_as_provider(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp.office365.com")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "notifications@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-from-env")
    captured = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            captured.update(host=host, port=port)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def starttls(self):
            return None

        def login(self, username, password):
            return None

        def send_message(self, message):
            return None

    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSMTP)

    assert email_service.send_email("user@example.com", "Hi", "<p>hi</p>") is True
    assert captured["host"] == "smtp.office365.com"


def test_render_template_renders_and_autoescapes():
    html = email_service.render_template(
        "invite.html",
        link="https://x.example/accept-invite?token=T",
        org="A&B Co",
        role="owner",
    )
    assert "https://x.example/accept-invite?token=T" in html
    assert "A&amp;B Co" in html  # org is HTML-escaped (autoescape on)
    assert "Owner" in html  # role|capitalize


def test_render_template_raises_for_missing_template():
    # render_template itself may raise; the send_* helpers catch it (see below).
    with pytest.raises(Exception):
        email_service.render_template("does_not_exist.html")


# ── CS-3 helpers: link + subject + template, delegating to send_email ─────────


def _capture_send_email(monkeypatch):
    captured = {}

    def fake_send(to, subject, html_body):
        captured.update(to=to, subject=subject, html=html_body)
        return True

    monkeypatch.setattr(email_service, "send_email", fake_send)
    return captured


def test_send_invite_email_builds_link_subject_and_html(monkeypatch):
    monkeypatch.setenv("PUBLIC_HOSTNAME", "https://app.example.com")
    captured = _capture_send_email(monkeypatch)

    ok = email_service.send_invite_email(
        "invitee@example.com", "TOK123", "Acme Corp", "analyst"
    )
    assert ok is True
    assert captured["to"] == "invitee@example.com"
    assert captured["subject"] == "You have been invited to Acme Corp on AgentIQ"
    assert "https://app.example.com/accept-invite?token=TOK123" in captured["html"]
    assert "Acme Corp" in captured["html"]
    assert "Analyst" in captured["html"]  # role rendered, capitalized


def test_send_welcome_email_builds_subject_and_html(monkeypatch):
    captured = _capture_send_email(monkeypatch)
    assert email_service.send_welcome_email("user@example.com", "Acme Corp") is True
    assert captured["subject"] == "Welcome to AgentIQ — Acme Corp"
    assert "Acme Corp" in captured["html"]


def test_send_password_reset_email_strips_trailing_slash_in_base(monkeypatch):
    monkeypatch.setenv("PUBLIC_HOSTNAME", "https://app.example.com/")  # trailing slash
    captured = _capture_send_email(monkeypatch)
    assert email_service.send_password_reset_email("user@example.com", "RTOK") is True
    assert captured["subject"] == "Reset your AgentIQ password"
    # No double slash and no double scheme — link is well-formed.
    assert "https://app.example.com/reset-password?token=RTOK" in captured["html"]


def test_helper_returns_false_when_render_fails(monkeypatch):
    """AC14 end-to-end: a template error inside a helper is a logged False."""
    def boom(*a, **k):
        raise RuntimeError("template blew up")

    monkeypatch.setattr(email_service, "render_template", boom)
    assert (
        email_service.send_invite_email("user@example.com", "T", "Org", "analyst")
        is False
    )


def test_helper_returns_false_when_provider_unconfigured(monkeypatch):
    """Real render + real send_email with no API key -> False, never raises."""
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    assert (
        email_service.send_password_reset_email("user@example.com", "RTOK") is False
    )
