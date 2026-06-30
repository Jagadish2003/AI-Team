"""R17-D1 review fix (MEDIUM) - startup completeness validation for in_boundary.

validate_provider_config() previously only checked that the selected provider
*name* was registered. An operator who set MODEL_GENERATION_PROVIDER=in_boundary
without configuring any endpoint URL passed startup cleanly and then every
generate() call silently returned ok=False — no findings, no enrichment, no
error surfaced. The graceful-failure contract is right for runtime transience
but is not an acceptable *sole* signal for a boot-time misconfiguration.

These tests verify InBoundaryModelProvider.validate() warns on an incomplete
config, validate_provider_config() invokes it for the SELECTED provider, and
neither path ever raises (startup must not be blocked).
"""
from __future__ import annotations

import logging

import pytest

from app.model_gateway import validate_provider_config
from app.model_gateway.in_boundary_config import (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    CONFIG_KEY_MODEL,
    IN_BOUNDARY_PROVIDER_NAME,
)
from app.model_gateway.in_boundary_provider import InBoundaryModelProvider

_ALL_IN_BOUNDARY_KEYS = (
    CONFIG_KEY_API_KEY,
    CONFIG_KEY_BASE_URL,
    CONFIG_KEY_EMBEDDING_ENDPOINT,
    CONFIG_KEY_EMBEDDING_MODEL,
    CONFIG_KEY_GENERATION_ENDPOINT,
    CONFIG_KEY_GENERATION_MODEL,
    CONFIG_KEY_MODEL,
)


@pytest.fixture
def _clear_in_boundary_env(monkeypatch):
    """Start every test from a fully-unconfigured in-boundary environment."""
    for key in _ALL_IN_BOUNDARY_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# provider.validate() — warns on incomplete config, never raises
# ---------------------------------------------------------------------------


def test_validate_warns_when_no_endpoint_configured(caplog, _clear_in_boundary_env):
    """No base URL and no endpoint override → a clear startup warning."""
    with caplog.at_level(logging.WARNING):
        InBoundaryModelProvider().validate()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no endpoint URL is configured" in m for m in msgs), msgs
    # The warning names the env keys an operator must set.
    assert any(CONFIG_KEY_BASE_URL in m for m in msgs)


def test_validate_warns_when_endpoint_set_but_no_model(
    caplog, monkeypatch, _clear_in_boundary_env
):
    """Endpoint present but no model name is also a guaranteed-failure config."""
    monkeypatch.setenv(CONFIG_KEY_BASE_URL, "https://models.example.internal")

    with caplog.at_level(logging.WARNING):
        InBoundaryModelProvider().validate()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no model name" in m for m in msgs), msgs


def test_validate_silent_when_fully_configured(
    caplog, monkeypatch, _clear_in_boundary_env
):
    """A complete config produces no warning."""
    monkeypatch.setenv(CONFIG_KEY_BASE_URL, "https://models.example.internal")
    monkeypatch.setenv(CONFIG_KEY_MODEL, "customer-model")

    with caplog.at_level(logging.WARNING):
        InBoundaryModelProvider().validate()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], [r.getMessage() for r in warnings]


def test_validate_silent_with_endpoint_override_only(
    caplog, monkeypatch, _clear_in_boundary_env
):
    """An explicit endpoint override (no base URL) counts as configured."""
    monkeypatch.setenv(
        CONFIG_KEY_GENERATION_ENDPOINT, "https://gen.example.internal/v1/chat/completions"
    )
    monkeypatch.setenv(CONFIG_KEY_GENERATION_MODEL, "customer-gen")

    with caplog.at_level(logging.WARNING):
        InBoundaryModelProvider().validate()

    endpoint_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "no endpoint URL" in r.getMessage()
    ]
    assert endpoint_warnings == []


def test_validate_never_raises(_clear_in_boundary_env):
    """validate() must never raise — startup cannot be blocked by it."""
    InBoundaryModelProvider().validate()  # must not raise


# ---------------------------------------------------------------------------
# validate_provider_config() — invokes validate() for the SELECTED provider
# ---------------------------------------------------------------------------


def test_startup_validation_warns_when_in_boundary_selected_without_endpoint(
    caplog, monkeypatch, _clear_in_boundary_env
):
    """Selecting in_boundary for generation with no endpoint warns at startup."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    with caplog.at_level(logging.WARNING):
        validate_provider_config()  # must not raise

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("no endpoint URL is configured" in m for m in msgs), msgs


def test_startup_validation_silent_for_default_hosted(caplog, monkeypatch):
    """When neither provider is in_boundary, no in-boundary warning is emitted."""
    monkeypatch.delenv("MODEL_GENERATION_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_EMBEDDING_PROVIDER", raising=False)

    with caplog.at_level(logging.WARNING):
        validate_provider_config()

    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("no endpoint URL is configured" in m for m in msgs), msgs


def test_startup_validation_warns_once_when_in_boundary_serves_both_roles(
    caplog, monkeypatch, _clear_in_boundary_env
):
    """in_boundary selected for BOTH generation and embedding warns exactly once
    (the resolved provider instance is de-duplicated by identity)."""
    monkeypatch.setenv("MODEL_GENERATION_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", IN_BOUNDARY_PROVIDER_NAME)

    with caplog.at_level(logging.WARNING):
        validate_provider_config()

    endpoint_warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "no endpoint URL is configured" in r.getMessage()
    ]
    assert len(endpoint_warnings) == 1, [r.getMessage() for r in endpoint_warnings]
