"""Unit tests for app.email_service — CS-3 (Section 3 / AC14).

The central contract: the email service NEVER raises and reports success/failure
as a bool. Without a configured provider + installed SDK (the default in CI/dev),
sends degrade to False and are logged — which is what keeps auth flows unbroken.
"""
from __future__ import annotations

import app.email_service as es


# ── send_email dispatch / degraded behaviour ──────────────────────────────────


def test_send_email_returns_false_for_unknown_provider(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "carrier-pigeon")
    assert es.send_email("a@b.com", "subj", "<p>body</p>") is False


def test_send_email_sendgrid_without_key_returns_false(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.delenv("SENDGRID_API_KEY", raising=False)
    assert es.send_email("a@b.com", "subj", "<p>body</p>") is False


def test_send_email_never_raises_even_if_provider_throws(monkeypatch):
    # Force the sendgrid branch past the key check, then make the provider helper
    # raise — send_email must still swallow it and return False (AC14).
    monkeypatch.setenv("EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "SG.fake")

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(es, "_send_sendgrid", _boom)
    assert es.send_email("a@b.com", "subj", "<p>body</p>") is False


# ── public helpers never raise and return bool ────────────────────────────────


def test_transactional_helpers_return_bool_and_never_raise(monkeypatch):
    # No provider configured → every helper returns False without raising.
    monkeypatch.setenv("EMAIL_PROVIDER", "none")
    assert es.send_welcome_email("a@b.com", "Acme") is False
    assert es.send_invite_email("a@b.com", "tok-123", "Acme", "analyst") is False
    assert es.send_password_reset_email("a@b.com", "rtok-456") is False


def test_helpers_tolerate_missing_optional_fields(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "none")
    # org_name may be None (e.g. an org with no display name) — must not crash.
    assert es.send_welcome_email("a@b.com", None) is False
    assert es.send_invite_email("a@b.com", "tok", None, "viewer") is False


# ── link building + template rendering ────────────────────────────────────────


def test_invite_email_builds_accept_invite_link(monkeypatch):
    monkeypatch.setenv("PUBLIC_HOSTNAME", "https://app.example.com")
    captured = {}
    monkeypatch.setattr(
        es, "send_email",
        lambda to, subject, html: captured.update(to=to, subject=subject, html=html) or True,
    )

    assert es.send_invite_email("invitee@x.com", "TOK", "Acme", "analyst") is True
    assert "https://app.example.com/accept-invite?token=TOK" in captured["html"]
    assert "Acme" in captured["subject"]


def test_reset_email_builds_reset_link_and_adds_scheme(monkeypatch):
    # A scheme-less PUBLIC_HOSTNAME gets https:// prefixed.
    monkeypatch.setenv("PUBLIC_HOSTNAME", "app.example.com")
    captured = {}
    monkeypatch.setattr(
        es, "send_email",
        lambda to, subject, html: captured.update(html=html) or True,
    )

    assert es.send_password_reset_email("u@x.com", "RTOK") is True
    assert "https://app.example.com/reset-password?token=RTOK" in captured["html"]


def test_render_template_falls_back_to_inline_body_when_absent():
    # No templates dir shipped yet (T6) → inline fallback HTML is returned, not an
    # error, and it carries the link/org context.
    html = es.render_template("invite.html", link="https://x/accept?token=t", org="Acme")
    assert "<html>" in html.lower()
    assert "https://x/accept?token=t" in html
    assert "Acme" in html


def test_redact_keeps_domain_drops_local_part():
    assert es._redact("alice@corp.com") == "<redacted>@corp.com"
    assert es._redact("not-an-email") == "<redacted>"
