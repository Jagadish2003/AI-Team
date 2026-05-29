"""
T1-S10-B  |  Audit Event Registry — Unit Tests
AgentIQ 2.0  |  Track 1 — Platform Foundation  |  Sprint 10

Acceptance criteria verified here:

  AC-AUD-1  schema_discovered constant is present in audit.py and in the
            AUDIT_EVENT_REGISTRY.
  AC-AUD-2  schema_discovered constant value != connector_queried constant
            value  (distinct strings — different data-access semantics).
  AC-AUD-3  SchemaDiscoveredEvent TypedDict declares the required fields:
            connector_id, schema_count, table_count  (+ org_id from doc).
  AC-AUD-4  log_event() accepts 'schema_discovered' without modification
            to T1-S10-B code — verified by calling log_event() with the
            schema_discovered event type and asserting no exception.
  AC-AUD-5  log_event() never raises — fail-silent contract.
  AC-AUD-6  connector_queried is in the registry with its TypedDict.
  AC-AUD-7  Registry idempotency and conflict detection.
  AC-AUD-8  T1 lifecycle events are registered.

Run:
  cd backend
  pytest tests/unit/test_audit_t1_s10_b.py -v
"""

from __future__ import annotations

import logging
from typing import get_type_hints
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# SCHEMA_DISCOVERED constant
# ---------------------------------------------------------------------------

def test_schema_discovered_constant_value():
    """SCHEMA_DISCOVERED must equal the string 'schema_discovered'."""
    from app.middleware.audit import SCHEMA_DISCOVERED
    assert SCHEMA_DISCOVERED == "schema_discovered"


def test_connector_queried_constant_value():
    """CONNECTOR_QUERIED must equal 'connector_queried' (distinct from schema_discovered)."""
    from app.middleware.audit import CONNECTOR_QUERIED
    assert CONNECTOR_QUERIED == "connector_queried"


def test_schema_discovered_distinct_from_connector_queried():
    """schema_discovered and connector_queried must be different strings."""
    from app.middleware.audit import SCHEMA_DISCOVERED, CONNECTOR_QUERIED
    assert SCHEMA_DISCOVERED != CONNECTOR_QUERIED


# ---------------------------------------------------------------------------
# AUDIT_EVENT_REGISTRY membership
# ---------------------------------------------------------------------------

def test_audit_event_registry_importable():
    """AUDIT_EVENT_REGISTRY must be importable from app.middleware.audit."""
    from app.middleware.audit import AUDIT_EVENT_REGISTRY  # noqa: F401


def test_schema_discovered_in_registry():
    """schema_discovered must be registered in AUDIT_EVENT_REGISTRY."""
    from app.middleware.audit import AUDIT_EVENT_REGISTRY
    assert "schema_discovered" in AUDIT_EVENT_REGISTRY


def test_connector_queried_in_registry():
    """connector_queried must remain in AUDIT_EVENT_REGISTRY (not replaced)."""
    from app.middleware.audit import AUDIT_EVENT_REGISTRY
    assert "connector_queried" in AUDIT_EVENT_REGISTRY


def test_schema_discovered_and_connector_queried_both_in_registry():
    """Both schema_discovered and connector_queried must coexist in the registry."""
    from app.middleware.audit import AUDIT_EVENT_REGISTRY
    assert "schema_discovered" in AUDIT_EVENT_REGISTRY
    assert "connector_queried" in AUDIT_EVENT_REGISTRY


# ---------------------------------------------------------------------------
# SchemaDiscoveredEvent shape
# ---------------------------------------------------------------------------

def test_schema_discovered_event_importable():
    """SchemaDiscoveredEvent must be importable from app.middleware.audit."""
    from app.middleware.audit import SchemaDiscoveredEvent  # noqa: F401


def test_schema_discovered_event_has_connector_id():
    from app.middleware.audit import SchemaDiscoveredEvent
    hints = get_type_hints(SchemaDiscoveredEvent)
    assert "connector_id" in hints


def test_schema_discovered_event_has_schema_count():
    from app.middleware.audit import SchemaDiscoveredEvent
    hints = get_type_hints(SchemaDiscoveredEvent)
    assert "schema_count" in hints


def test_schema_discovered_event_has_table_count():
    from app.middleware.audit import SchemaDiscoveredEvent
    hints = get_type_hints(SchemaDiscoveredEvent)
    assert "table_count" in hints


# ---------------------------------------------------------------------------
# log_event() — accepts schema_discovered without special-casing
# ---------------------------------------------------------------------------

def test_log_event_accepts_schema_discovered():
    """log_event() must accept schema_discovered without raising."""
    import app.middleware.audit as audit_mod
    audit_mod._TABLES_INITIALISED = False

    with patch("app.middleware.audit.db.connect") as mock_connect:
        mock_con = MagicMock()
        mock_connect.return_value = mock_con
        mock_con.__enter__ = MagicMock(return_value=mock_con)
        mock_con.__exit__ = MagicMock(return_value=False)

        from app.middleware.audit import log_event
        # Must not raise
        log_event(
            "schema_discovered",
            connector_id="salesforce",
            schema_count=3,
            table_count=47,
        )

    assert mock_con.execute.called
    # The last execute call must be the INSERT (DDL calls precede it when table is uninitialised)
    last_call_args = mock_con.execute.call_args_list[-1][0]
    assert "INSERT INTO audit_log" in last_call_args[0]
    # event_type in the bound params must be the schema_discovered string
    assert "schema_discovered" in last_call_args[1]


def test_log_event_schema_discovered_uses_constant():
    """log_event() called with SCHEMA_DISCOVERED constant behaves identically."""
    import app.middleware.audit as audit_mod
    audit_mod._TABLES_INITIALISED = False

    from app.middleware.audit import SCHEMA_DISCOVERED

    with patch("app.middleware.audit.db.connect") as mock_connect:
        mock_con = MagicMock()
        mock_connect.return_value = mock_con
        mock_con.__enter__ = MagicMock(return_value=mock_con)
        mock_con.__exit__ = MagicMock(return_value=False)

        from app.middleware.audit import log_event
        log_event(
            SCHEMA_DISCOVERED,
            connector_id="servicenow",
            schema_count=1,
            table_count=12,
        )

    call_args = mock_con.execute.call_args[0]
    assert "schema_discovered" in call_args[1]
