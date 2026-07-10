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
    # The password value is credential-shaped (mixed letter+digit+symbol) so the
    # generic assignment rule fires — an all-lowercase word would be treated as a
    # benign identifier (see test_credential_named_assignment_of_benign_value_*).
    text = (
        f"aws=AKIAIOSFODNN7EXAMPLE\n"
        f"gh={_FAKE_GH_TOKEN}\n"
        'password="t0psecret-value"'
    )
    outcome = scan_and_redact(text)
    assert outcome.count == 3
    assert set(outcome.pattern_types) == {
        "aws_access_key_id",
        "github_token",
        "secret_assignment",
    }
    for secret in ("AKIAIOSFODNN7EXAMPLE", _FAKE_GH_TOKEN, "t0psecret-value"):
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


# ─────────────────────────────────────────────────────────────────────────────
# AT-531 review — false-positive fix: credential-named key + benign value (#2)
# ─────────────────────────────────────────────────────────────────────────────
_BENIGN_ASSIGNMENTS = [
    'token = next_item',
    'password = get_input()',
    'secret = compute_value()',
    'self.token = response.json',
    'access_token = handler.default',
    'retries = 5',
    'password_changed_at = 2026-06-20',
    'last_updated_by = current_user',
    'api_key = None',
    'is_secret = True',
    'token: active',
    'client_secret = settings.CLIENT_SECRET',
]


@pytest.mark.parametrize("line", _BENIGN_ASSIGNMENTS)
def test_credential_named_assignment_of_benign_value_is_not_redacted(line):
    # A variable NAMED like a credential but assigned ordinary code/values must
    # not be redacted — that would silently corrupt source before indexing.
    outcome = scan_and_redact(line)
    assert outcome.redacted is False, f"false-positive redaction of: {line!r}"
    assert outcome.text == line


_CREDENTIAL_ASSIGNMENTS = [
    'password = "s3cr3t-p@ssw0rd"',
    'api_key = "sk-live-0123456789abcdef"',
    'client_secret=abcdef123456ZZ',
    'auth_token = "aBcD1234efGH5678"',
    'password: "hunter2xyz"',
]


@pytest.mark.parametrize("line", _CREDENTIAL_ASSIGNMENTS)
def test_credential_named_assignment_of_secret_value_is_redacted(line):
    outcome = scan_and_redact(line)
    assert outcome.pattern_types == ["secret_assignment"]
    assert "[REDACTED:secret_assignment]" in outcome.text


# ─────────────────────────────────────────────────────────────────────────────
# AT-531 review — JWT minimum segment length (#3)
# ─────────────────────────────────────────────────────────────────────────────
def test_short_dotted_identifier_starting_with_eyj_is_not_a_jwt():
    # 'eyJa.b.c' and dotted version strings must not be mistaken for a JWT.
    for line in ("value = eyJa.b.c", "tag eyJ1.2.3 released", "eyJx.y.z"):
        outcome = scan_and_redact(line)
        assert "jwt" not in outcome.pattern_types, line
        assert outcome.text == line


def test_realistic_jwt_is_redacted():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    outcome = scan_and_redact(f"Authorization: Bearer {jwt}")
    assert outcome.pattern_types == ["jwt"]
    assert jwt not in outcome.text


# ─────────────────────────────────────────────────────────────────────────────
# AT-531 review — RedactionOutcome API: count vs distinct types (#5)
# ─────────────────────────────────────────────────────────────────────────────
def test_pattern_types_is_deduplicated_while_count_is_per_match():
    outcome = scan_and_redact(
        "a=AKIAIOSFODNN7EXAMPLE b=AKIAIOSFODNN7EXAMPLE c=AKIAIOSFODNN7EXAMPLE"
    )
    # Three matches of ONE type: count reflects matches, pattern_types is distinct.
    assert outcome.count == 3
    assert outcome.pattern_types == ["aws_access_key_id"]
    assert outcome.counts_by_type == {"aws_access_key_id": 3}
