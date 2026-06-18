"""Unit tests for validate_password_strength — CS-3 §1a (AC1–AC3).

The function returns a list of UNMET requirements (empty == valid) and never
raises. Rule: min 8 chars, >=1 uppercase, >=1 lowercase, >=1 special character.
"""
from __future__ import annotations

from app.auth.user_auth import validate_password_strength


def test_ac1_fully_valid_password_has_no_unmet_rules():
    # AC1: validate_password_strength('Password1!') returns [] (all rules met).
    assert validate_password_strength("Password1!") == []


def test_ac2_lowercase_word_is_missing_uppercase_and_special():
    # AC2: 'password' meets length + lowercase; fails uppercase + special.
    # (The doc's AC2 prose says "3 errors" but itself lists only the two concrete
    # failures and notes minimum length is met — so two unmet rules is correct.)
    unmet = validate_password_strength("password")
    assert "at least one uppercase letter" in unmet
    assert "at least one special character" in unmet
    assert "at least 8 characters" not in unmet  # length is satisfied
    assert "at least one lowercase letter" not in unmet


def test_ac3_short_password_fails_only_length():
    # AC3: 'Pass1!' satisfies upper/lower/special but is < 8 chars.
    unmet = validate_password_strength("Pass1!")
    assert unmet == ["at least 8 characters"]


def test_each_rule_can_fail_independently():
    assert "at least one uppercase letter" in validate_password_strength("alllower1!")
    assert "at least one lowercase letter" in validate_password_strength("ALLUPPER1!")
    assert "at least one special character" in validate_password_strength("NoSpecial1")
    assert "at least 8 characters" in validate_password_strength("Ab1!")


def test_variety_of_special_characters_are_accepted():
    for ch in "!@#$%^&*()_-+=[]{}|;:,.<>?/":
        assert validate_password_strength(f"Abcdefg1{ch}") == [], ch


def test_never_raises_on_empty_or_unicode():
    # Empty string: every rule unmet, but no exception.
    assert len(validate_password_strength("")) == 4
    # Unicode letters still count toward upper/lower; emoji is "special".
    assert validate_password_strength("Abcdefg1\U0001F600") == []
