"""R18-A2 / AT-531 — unit tests for the secret-pattern scanner (AC5).

Covers the content-source-agnostic redaction primitive that the Git content
ingestor's ``_secret_scan`` seam wires in: every key/token/password signature is
redacted, clean content is left untouched, and the returned outcome NEVER carries
the secret value it removed.
"""
from __future__ import annotations

import pytest

from discovery.ingest.secret_redaction import RedactionOutcome, scan_and_redact

# Fake, non-functional provider tokens used to exercise the scanner's regexes.
# They are ASSEMBLED at runtime (never written as a contiguous literal) so that
# repository push-protection secret scanners don't misfire on the test data — the
# very thing this scanner exists to catch. Each still matches its regex at runtime.
_FAKE_GH_TOKEN = "ghp" + "_0123456789abcdefghijklmnopqrstuvwxyz"
_FAKE_SLACK_TOKEN = "xox" + "b-123456789012-abcdefABCDEF012345"
_FAKE_GOOGLE_KEY = "AIza" + "B" * 35

# (label, text-with-secret, the exact secret substring that must NOT survive,
#  expected pattern-type name)
_SECRET_CASES = [
    (
        "aws_access_key_id",
        "aws creds AKIAIOSFODNN7EXAMPLE in config",
        "AKIAIOSFODNN7EXAMPLE",
        "aws_access_key_id",
    ),
    (
        "github_pat",
        f"use token {_FAKE_GH_TOKEN} now",
        _FAKE_GH_TOKEN,
        "github_token",
    ),
    (
        "slack_token",
        f"SLACK={_FAKE_SLACK_TOKEN}",
        _FAKE_SLACK_TOKEN,
        "slack_token",
    ),
    (
        "google_api_key",
        f"gmaps key={_FAKE_GOOGLE_KEY}",
        _FAKE_GOOGLE_KEY,
        "google_api_key",
    ),
    (
        "jwt",
        "auth: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4",
        "jwt",
    ),
    (
        "pem_private_key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIBhushbudEEP\n-----END RSA PRIVATE KEY-----",
        "MIIBhushbudEEP",
        "private_key",
    ),
    (
        "password_assignment",
        'db_config:\npassword: "hunter2-super-secret"',
        "hunter2-super-secret",
        "secret_assignment",
    ),
    (
        "api_key_assignment",
        'API_KEY = "sk-live-0123456789abcdef"',
        "sk-live-0123456789abcdef",
        "secret_assignment",
    ),
    (
        "client_secret_assignment",
        "client_secret=abcdef123456ZZ",
        "abcdef123456ZZ",
        "secret_assignment",
    ),
]


@pytest.mark.parametrize(
    "label,text,secret,expected_type",
    _SECRET_CASES,
    ids=[c[0] for c in _SECRET_CASES],
)
def test_each_signature_is_redacted_and_never_leaks(label, text, secret, expected_type):
    outcome = scan_and_redact(text)
    assert outcome.redacted
    assert expected_type in outcome.pattern_types
    # The secret value is gone from the redacted text …
    assert secret not in outcome.text
    # … and NEVER surfaces on the outcome metadata (no value re-leak).
    assert all(secret not in pt for pt in outcome.pattern_types)
    assert "[REDACTED:" in outcome.text


def test_assignment_keeps_key_name_and_structure():
    outcome = scan_and_redact('DATABASE_PASSWORD_X\npassword = "s3cr3tvalue"')
    # The key/operator/quoting is preserved; only the value is replaced.
    assert outcome.text == 'DATABASE_PASSWORD_X\npassword = "[REDACTED:secret_assignment]"'
    assert outcome.count == 1


def test_clean_content_passes_through_unchanged():
    code = "def compute_discount(total):\n    return total * 0.1\n"
    outcome = scan_and_redact(code)
    assert outcome.text == code
    assert outcome.pattern_types == []
    assert outcome.redacted is False
    assert outcome.count == 0


def test_empty_and_none_are_safe():
    assert scan_and_redact("").text == ""
    assert scan_and_redact("").pattern_types == []
    assert scan_and_redact(None).text == ""  # type: ignore[arg-type]


def test_multiple_secrets_counted_and_typed_in_order():
    text = (
        f"aws=AKIAIOSFODNN7EXAMPLE\n"
        f"gh={_FAKE_GH_TOKEN}\n"
        'password="topsecretvalue"'
    )
    outcome = scan_and_redact(text)
    assert outcome.count == 3
    assert set(outcome.pattern_types) == {
        "aws_access_key_id",
        "github_token",
        "secret_assignment",
    }
    for secret in ("AKIAIOSFODNN7EXAMPLE", _FAKE_GH_TOKEN, "topsecretvalue"):
        assert secret not in outcome.text


def test_placeholder_is_not_re_redacted():
    # A provider token inside a secret-named assignment: the provider rule fires
    # first; the assignment rule must NOT double-redact the resulting placeholder.
    outcome = scan_and_redact(f'token = "{_FAKE_GH_TOKEN}"')
    assert outcome.count == 1
    assert outcome.pattern_types == ["github_token"]
    assert outcome.text == 'token = "[REDACTED:github_token]"'


def test_hashes_and_uuids_are_not_flagged():
    # Content SHAs / commit ids / UUIDs must not be mistaken for secrets — that
    # would redact legitimate provenance en masse.
    text = (
        "commit c3c3c3c3d4e5f6 merged\n"
        "id: 550e8400-e29b-41d4-a716-446655440000\n"
        "sha256 = e3b0c44298fc1c149afbf4c8996fb924"
    )
    outcome = scan_and_redact(text)
    # 'sha256 = ...' is not a secret-named key, so it is left alone.
    assert outcome.pattern_types == []
    assert outcome.text == text
