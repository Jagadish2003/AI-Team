"""Unit tests for CS-3 — transactional email templates (T6).

These validate the three customer-facing templates under app/templates/:
invite.html, welcome.html, reset_password.html. They check the content the task
calls for (clear CTA link, org context, time-limited reset, no extra/sensitive
variables) and mobile-readability hints (viewport meta + responsive @media), and
— when Jinja2 is available — that each renders cleanly with the variable
contract the email service passes (invite: link/org/role; welcome: org;
reset: link).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "app" / "templates"
TEMPLATES = ("invite.html", "welcome.html", "reset_password.html")


def _read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _jinja_vars(text: str) -> set[str]:
    """Names referenced via {{ var ... }} (ignores filters/whitespace)."""
    return set(re.findall(r"\{\{\s*([a-zA-Z_]\w*)", text))


# ── presence ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_exists(name):
    assert (TEMPLATES_DIR / name).is_file(), f"{name} is missing"


# ── mobile-readability + branding (all templates) ─────────────────────────────


@pytest.mark.parametrize("name", TEMPLATES)
def test_template_is_mobile_readable_and_branded(name):
    html = _read(name)
    assert "<!DOCTYPE html>" in html
    assert 'name="viewport"' in html and "width=device-width" in html  # mobile
    assert "@media" in html  # responsive rules for small screens
    assert "AgentIQ" in html  # branding, not a raw system message


# ── invite ──────────────────────────────────────────────────────────────────


def test_invite_template_has_org_and_accept_cta():
    html = _read("invite.html")
    assert re.search(r"invit", html, re.IGNORECASE)  # explains the invitation
    assert "{{ org }}" in html  # specific organization
    assert "role" in _jinja_vars(html)  # role conveyed
    # Clear call-to-action link wired to the invite URL (token-bearing link var).
    assert 'href="{{ link }}"' in html
    assert _jinja_vars(html) <= {"link", "org", "role"}  # no stray variables


# ── welcome ───────────────────────────────────────────────────────────────────


def test_welcome_template_confirms_registration():
    html = _read("welcome.html")
    assert re.search(r"welcome", html, re.IGNORECASE)
    assert "{{ org }}" in html
    assert "{{ recipient_name }}" in html
    # Onboarding feel: a clear "get started" / next-steps section.
    assert re.search(r"get started|started", html, re.IGNORECASE)
    assert _jinja_vars(html) <= {"org", "recipient_name"}


# ── reset password ────────────────────────────────────────────────────────────


def test_reset_template_has_secure_time_limited_link():
    html = _read("reset_password.html")
    assert 'href="{{ link }}"' in html  # secure reset link as a clear CTA
    # Clearly time-limited.
    assert re.search(r"expire", html, re.IGNORECASE)
    assert re.search(r"1 hour|hour", html, re.IGNORECASE)
    # Safety note for unrequested resets.
    assert re.search(r"didn'?t request|did not request", html, re.IGNORECASE)
    # No sensitive data exposed: the only variable is the (opaque) link.
    assert _jinja_vars(html) == {"link"}
    assert "{{ token" not in html and "password_hash" not in html.lower()


# ── Jinja2 render smoke (skips if Jinja2 isn't installed) ─────────────────────


def test_templates_render_with_jinja2():
    pytest.importorskip("jinja2")
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )

    invite = env.get_template("invite.html").render(
        link="https://app.example.com/accept-invite?token=ABC123",
        org="Acme Corp",
        role="analyst",
    )
    assert "Acme Corp" in invite
    assert "https://app.example.com/accept-invite?token=ABC123" in invite
    assert "Analyst" in invite  # role|capitalize

    welcome = env.get_template("welcome.html").render(
        org="Acme Corp",
        recipient_name="Jenny",
    )
    assert "Acme Corp" in welcome
    assert "Welcome Jenny," in welcome
    assert "Welcome to AgentIQ" in welcome

    reset = env.get_template("reset_password.html").render(
        link="https://app.example.com/reset-password?token=XYZ789",
    )
    assert "https://app.example.com/reset-password?token=XYZ789" in reset
    assert re.search(r"expire", reset, re.IGNORECASE)

    # No unrendered Jinja syntax left in any output.
    for out in (invite, welcome, reset):
        assert "{{" not in out and "{%" not in out
