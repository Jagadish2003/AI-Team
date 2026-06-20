"""Unit tests for CS-3 — validate_password_strength() in app.auth.user_auth.

The locked rule (CS-3 Section 1): at least 8 characters with at least one
uppercase letter, one lowercase letter, and one special character from
``!@#$%^&*()_+-=[]{}|;:,.<>?``. The function returns the list of UNMET
requirement messages; an empty list means the password is valid.

Acceptance criteria covered:
  AC1 — validate_password_strength('Password1!') == [] (all rules met).
  AC2 — 'password' is missing uppercase and special (length is met).
  AC3 — 'Pass1' reports the 8-character rule (it is 5 chars).

Doc-vs-rule notes (the doc's prose miscounts; the rule below is authoritative and
matches the doc's own reference implementation):
  * AC2 says "3 errors" but itemises only uppercase + special; 'password' is 8
    chars (length met) and there is no digit rule, so exactly those 2 are unmet.
  * AC3 says 'Pass1' has "all other rules met" with only the length error, but
    'Pass1' has no special character ('1' is a digit, not one of the special
    characters), so length AND special are unmet. These tests assert the real,
    secure behaviour.
"""
from __future__ import annotations

import pytest

from app.auth.user_auth import (
    PASSWORD_MIN_LENGTH,
    hash_password,
    validate_password_strength,
    verify_password,
)

UPPERCASE = "at least one uppercase letter"
LOWERCASE = "at least one lowercase letter"
SPECIAL = "at least one special character"
LENGTH = f"at least {PASSWORD_MIN_LENGTH} characters"


# ── Acceptance criteria ──────────────────────────────────────────────────────


def test_ac1_strong_password_returns_empty_list():
    """AC1 — a password meeting all four rules returns []."""
    assert validate_password_strength("Password1!") == []


def test_ac2_all_lowercase_missing_uppercase_and_special():
    """AC2 — 'password' (8 chars, lowercase only) is missing uppercase + special.

    Length is met and there is no digit rule, so exactly two requirements are
    unmet (the doc's "3 errors" wording is a miscount).
    """
    errors = validate_password_strength("password")
    assert errors == [UPPERCASE, SPECIAL]
    assert LENGTH not in errors  # minimum length is satisfied


def test_ac3_short_password_reports_length_rule():
    """AC3 — 'Pass1' (5 chars) reports the 8-character requirement.

    It also lacks a special character, so the full result is length + special.
    """
    errors = validate_password_strength("Pass1")
    assert LENGTH in errors  # the headline failure AC3 calls out
    assert errors == [LENGTH, SPECIAL]


# ── Each rule in isolation ────────────────────────────────────────────────────


def test_missing_only_uppercase():
    # 8+ chars, lowercase + special, no uppercase.
    assert validate_password_strength("password1!") == [UPPERCASE]


def test_missing_only_lowercase():
    # 8+ chars, uppercase + special, no lowercase.
    assert validate_password_strength("PASSWORD1!") == [LOWERCASE]


def test_missing_only_special_digit_does_not_count():
    # 9 chars, upper + lower + a digit, but NO special character. Proves a digit
    # does not satisfy the special-character rule.
    assert validate_password_strength("Password1") == [SPECIAL]


def test_empty_password_fails_every_rule():
    assert validate_password_strength("") == [LENGTH, UPPERCASE, LOWERCASE, SPECIAL]


def test_exactly_eight_chars_is_long_enough():
    # Boundary: 8 chars meets the length rule (>=, not >).
    assert LENGTH not in validate_password_strength("Abcdef1!")
    assert validate_password_strength("Abcdef1!") == []


def test_seven_chars_is_too_short():
    errors = validate_password_strength("Abcde1!")  # 7 chars, otherwise valid
    assert errors == [LENGTH]


@pytest.mark.parametrize(
    "special",
    list("!@#$%^&*()_+-=[]{}|;:,.<>?"),
)
def test_each_special_character_satisfies_the_rule(special):
    password = f"Abcdefg1{special}"
    assert SPECIAL not in validate_password_strength(password)
    assert validate_password_strength(password) == []


def test_error_order_is_length_then_upper_lower_special():
    # A password failing everything returns the messages in a stable order so a
    # route can join them deterministically.
    assert validate_password_strength("!") == [LENGTH, UPPERCASE, LOWERCASE]


# ── Independence from hashing / login ─────────────────────────────────────────


def test_validation_does_not_truncate_long_passwords():
    """Runs on the full input — a >72-byte password is judged whole, not capped.

    bcrypt's 72-byte truncation lives in hashing and is independent of this check.
    """
    long_valid = "Aa1!" + ("x" * 100)  # 104 chars, all four rules met
    assert validate_password_strength(long_valid) == []


def test_login_path_is_unaffected_weak_password_still_verifies():
    """A pre-CS-3 weak password must still hash and verify.

    Login never calls validate_password_strength(); it only verifies against the
    stored hash. So an existing user whose password predates the rule is not
    locked out, even though the password would now be rejected at creation time.
    """
    weak = "weakpass"  # fails the strength rule (no uppercase, no special)
    assert validate_password_strength(weak) != []
    stored = hash_password(weak)
    assert verify_password(weak, stored) is True


def test_never_raises_on_unusual_input():
    # Whitespace / unicode must not blow up — it just reports unmet rules.
    for value in ("        ", "пароль", "🔐🔐🔐🔐🔐🔐🔐🔐"):
        result = validate_password_strength(value)
        assert isinstance(result, list)
