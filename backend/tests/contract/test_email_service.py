"""Unit tests for CS-3 email_service.py using SMTP / Office 365."""
from __future__ import annotations

import logging

import pytest

from app import email_service


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


def test_send_email_false_when_smtp_password_missing(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.office365.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "notifications@example.com")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)

    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        assert email_service.send_email("user@example.com", "s", "<p>b</p>") is False
    assert any("SMTP_PASSWORD" in r.getMessage() for r in caplog.records)


def test_send_email_false_for_unknown_provider(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_PROVIDER", "unsupported-provider")
    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        assert email_service.send_email("user@example.com", "s", "<p>b</p>") is False
    assert any("EMAIL_PROVIDER" in r.getMessage() for r in caplog.records)


def test_send_email_never_raises_on_transport_exception(monkeypatch, caplog):
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_HOST", "smtp.office365.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USERNAME", "notifications@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-from-env")

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(email_service.smtplib, "SMTP", boom)
    with caplog.at_level(logging.ERROR, logger="app.email_service"):
        result = email_service.send_email("user@example.com", "s", "<p>b</p>")
    assert result is False
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_render_template_renders_and_autoescapes():
    html = email_service.render_template(
        "invite.html",
        link="https://x.example/accept-invite?token=T",
        org="A&B Co",
        role="owner",
    )
    assert "https://x.example/accept-invite?token=T" in html
    assert "A&amp;B Co" in html
    assert "Owner" in html


def test_render_template_raises_for_missing_template():
    with pytest.raises(Exception):
        email_service.render_template("does_not_exist.html")


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
    assert "Analyst" in captured["html"]


def test_send_welcome_email_builds_subject_and_html(monkeypatch):
    captured = _capture_send_email(monkeypatch)
    assert email_service.send_welcome_email("jenny.smith@example.com", "Acme Corp") is True
    assert (
        captured["subject"]
        == "Welcome to AgentIQ – Your Organization Has Been Submitted for Approval"
    )
    assert "Welcome Jenny Smith" in captured["html"]
    assert "Acme Corp" in captured["html"]
    assert "submitted for approval" in captured["html"]
    assert "created successfully" in captured["html"]


def test_send_welcome_email_uses_exact_full_name_when_given(monkeypatch):
    captured = _capture_send_email(monkeypatch)
    assert (
        email_service.send_welcome_email(
            "jenny.smith@example.com", "Acme Corp", "Sreedhar M"
        )
        is True
    )
    # The exact registered name is greeted, not the email-derived one.
    assert "Welcome Sreedhar M" in captured["html"]
    assert "Jenny Smith" not in captured["html"]


def test_send_welcome_email_falls_back_to_email_name_when_full_name_blank(monkeypatch):
    captured = _capture_send_email(monkeypatch)
    assert (
        email_service.send_welcome_email("jenny.smith@example.com", "Acme Corp", "  ")
        is True
    )
    assert "Welcome Jenny Smith" in captured["html"]


def test_send_password_reset_email_strips_trailing_slash_in_base(monkeypatch):
    monkeypatch.setenv("PUBLIC_HOSTNAME", "https://app.example.com/")
    captured = _capture_send_email(monkeypatch)
    assert email_service.send_password_reset_email("user@example.com", "RTOK") is True
    assert captured["subject"] == "Reset your AgentIQ password"
    assert "https://app.example.com/reset-password?token=RTOK" in captured["html"]


def test_helper_returns_false_when_render_fails(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("template blew up")

    monkeypatch.setattr(email_service, "render_template", boom)
    assert (
        email_service.send_invite_email("user@example.com", "T", "Org", "analyst")
        is False
    )


def test_helper_returns_false_when_provider_unconfigured(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "unsupported-provider")
    assert (
        email_service.send_password_reset_email("user@example.com", "RTOK") is False
    )


# ---------------------------------------------------------------------------
# AUTH-2 T5 — org approval emails + templates
# ---------------------------------------------------------------------------


def test_org_approval_request_email_to_admin_with_distinct_links(monkeypatch):
    """T5-AC1: sent to AGENTIQ_ADMIN_EMAIL with org name, registrant, and
    distinct approve and reject links."""
    monkeypatch.setenv("AGENTIQ_BACKEND_URL", "https://api.example.com")
    captured = _capture_send_email(monkeypatch)

    ok = email_service.send_org_approval_request_email(
        admin_email="agentiqadmin@dwpglobal.com",
        org_name="Acme Bank",
        registrant_email="owner@acme.test",
        approval_token="TOK_ABC123",
        org_id="org-xyz-789",
    )

    assert ok is True
    assert captured["to"] == "agentiqadmin@dwpglobal.com"
    assert captured["subject"] == "New AgentIQ organisation pending approval: Acme Bank"
    assert not captured["subject"].startswith("Welcome")
    assert "needs approval" not in captured["subject"]
    html = captured["html"]
    assert "Organisation approval" in html
    assert "New organisation pending approval" in html
    assert "<strong>Owner</strong> has registered" in html
    assert "Registrant name" in html
    assert "Registrant email" in html
    assert "Welcome Owner to AgentIQ" not in html
    assert "Acme Bank" in html
    assert "owner@acme.test" in html
    # Distinct approve and reject endpoints both present.
    assert "/api/auth/org-approval/approve" in html
    assert "/api/auth/org-approval/reject" in html


def test_org_approval_request_links_carry_token_and_org_id(monkeypatch):
    """T5-AC2: both links contain the signed token and org_id as query params."""
    monkeypatch.setenv("AGENTIQ_BACKEND_URL", "https://api.example.com")
    captured = _capture_send_email(monkeypatch)

    email_service.send_org_approval_request_email(
        admin_email="agentiqadmin@dwpglobal.com",
        org_name="Acme Bank",
        registrant_email="owner@acme.test",
        approval_token="TOK_ABC123",
        org_id="org-xyz-789",
    )
    html = captured["html"]

    # Token and org_id present as query parameters (autoescaped & is fine in HTML).
    assert "token=TOK_ABC123" in html
    assert "org_id=org-xyz-789" in html
    # Both the approve and the reject URL carry them: two occurrences each.
    assert html.count("token=TOK_ABC123") >= 2
    assert html.count("org_id=org-xyz-789") >= 2


def test_org_approval_links_resolve_against_internal_deployment_url(monkeypatch):
    """R18-A3 T7 / AC6: in a no-public-inbound deployment the AUTH-2 approve/reject
    links resolve against the INTERNAL deployment URL (AGENTIQ_BACKEND_URL), so an
    internal admin completes the flow from inside the network with no inbound
    exposure. No localhost default and no public host leaks into the links."""
    internal_url = "https://agentiq.internal.bank.local"
    monkeypatch.setenv("AGENTIQ_BACKEND_URL", internal_url)
    captured = _capture_send_email(monkeypatch)

    email_service.send_org_approval_request_email(
        admin_email="agentiqadmin@dwpglobal.com",
        org_name="Acme Bank",
        registrant_email="owner@acme.test",
        approval_token="TOK_ABC123",
        org_id="org-xyz-789",
    )
    html = captured["html"]

    # Both action links are absolute against the internal deployment host.
    assert f"{internal_url}/api/auth/org-approval/approve" in html
    assert f"{internal_url}/api/auth/org-approval/reject" in html
    # No stray localhost default and no public host — the link never points outside
    # the internal network.
    assert "localhost" not in html
    assert "cloudfulcrum.com" not in html


def test_org_approved_login_link_uses_public_hostname(monkeypatch):
    """R18-A3 T7 / AC6: the follow-on 'approved' email login link is built from
    PUBLIC_HOSTNAME, so it too points at the internal deployment URL in a no-inbound
    environment (the newly approved registrant lands on the internal login page)."""
    internal_url = "https://agentiq.internal.bank.local"
    monkeypatch.setenv("PUBLIC_HOSTNAME", internal_url)
    captured = _capture_send_email(monkeypatch)

    email_service.send_org_approved_email(
        registrant_email="owner@acme.test", org_name="Acme Bank"
    )
    assert f"{internal_url}/login" in captured["html"]
    assert "localhost" not in captured["html"]


def test_org_approval_request_states_seven_day_expiry(monkeypatch):
    """T5-AC3: the request email states the links expire in 7 days."""
    captured = _capture_send_email(monkeypatch)
    email_service.send_org_approval_request_email(
        admin_email="agentiqadmin@dwpglobal.com",
        org_name="Acme Bank",
        registrant_email="owner@acme.test",
        approval_token="TOK",
        org_id="org-1",
    )
    assert "7 days" in captured["html"]


def test_send_org_approved_email_uses_template_to_registrant(monkeypatch):
    """T5-AC4: org_approved.html is rendered and sent to the registrant."""
    captured = _capture_send_email(monkeypatch)
    ok = email_service.send_org_approved_email(
        registrant_email="owner@acme.test", org_name="Acme Bank"
    )
    assert ok is True
    assert captured["to"] == "owner@acme.test"
    assert "approved" in captured["subject"].lower()
    assert "Acme Bank" in captured["html"]
    assert "approved" in captured["html"].lower()


def test_send_org_rejected_email_uses_template_to_registrant(monkeypatch):
    """T5-AC5: org_rejected.html is rendered and sent to the registrant."""
    captured = _capture_send_email(monkeypatch)
    ok = email_service.send_org_rejected_email(
        registrant_email="owner@acme.test", org_name="Acme Bank"
    )
    assert ok is True
    assert captured["to"] == "owner@acme.test"
    assert "not approved" in captured["subject"].lower()
    assert "Acme Bank" in captured["html"]


def test_all_three_org_templates_render_without_errors():
    """T5-AC6: all three templates render with valid variables and escape input."""
    request_html = email_service.render_template(
        "org_approval_request.html",
        org_name="A&B Bank",
        registrant_name="Owner",
        registrant_email="owner@acme.test",
        submitted_at="2026-06-19 10:00 UTC",
        approve_url="https://api.example.com/api/auth/org-approval/approve?token=T&org_id=O",
        reject_url="https://api.example.com/api/auth/org-approval/reject?token=T&org_id=O",
        expiry_days=7,
    )
    assert "owner@acme.test" in request_html
    assert "A&amp;B Bank" in request_html  # autoescaped
    assert "7 days" in request_html

    approved_html = email_service.render_template(
        "org_approved.html", org_name="Acme Bank", login_url="https://app.example.com/login"
    )
    assert "Acme Bank" in approved_html
    assert "https://app.example.com/login" in approved_html

    rejected_html = email_service.render_template(
        "org_rejected.html", org_name="Acme Bank"
    )
    assert "Acme Bank" in rejected_html


def test_org_approved_email_renders_when_login_url_absent():
    """org_approved.html must render even if login_url is not supplied."""
    html = email_service.render_template("org_approved.html", org_name="Acme Bank")
    assert "Acme Bank" in html
