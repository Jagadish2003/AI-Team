"""Contract tests — telemetry/audit org attribution (R17-D3 / AT-450, T5).

T5-AC1  Telemetry events include the authenticated organization.
T5-AC2  Audit events are attributed to the correct organization.
T5-AC3  No telemetry or audit event uses an incorrect or default organization.

Both trails resolve org through the single shared helper
``app.middleware.tenancy.resolve_event_org_id`` so they can never disagree:
authenticated request context wins, an explicit org_id (background callers) is
the fallback, and an unresolved event is marked UNATTRIBUTED — never the real
"default" tenant.
"""
from __future__ import annotations

import contextlib
import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from app import db
from app.middleware import tenancy
from app.middleware.tenancy import (
    UNATTRIBUTED_ORG,
    resolve_event_org_id,
)
from app.telemetry import record_event


@contextlib.contextmanager
def org_context(org_id: str | None):
    """Set the tenancy ContextVar for the duration of the block (mirrors what
    TenancyMiddleware does per request), then restore it."""
    token = tenancy._current_org_id.set(org_id)
    try:
        yield
    finally:
        tenancy._current_org_id.reset(token)


# ---------------------------------------------------------------------------
# resolve_event_org_id — the single attribution rule (unit)
# ---------------------------------------------------------------------------


def test_resolver_prefers_authenticated_context():
    with org_context("org_ctx"):
        assert resolve_event_org_id("org_explicit") == "org_ctx"


def test_resolver_falls_back_to_explicit_when_no_context():
    with org_context(None):
        assert resolve_event_org_id("org_explicit") == "org_explicit"


def test_resolver_uses_unattributed_sentinel_when_nothing_resolves():
    with org_context(None):
        assert resolve_event_org_id(None) == UNATTRIBUTED_ORG


def test_unattributed_sentinel_is_not_a_real_tenant():
    # T5-AC3: the sentinel must never be a real tenant org such as "default".
    assert UNATTRIBUTED_ORG != "default"


# ---------------------------------------------------------------------------
# Telemetry — record_event attribution (T5-AC1, T5-AC3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def capture_telemetry():
    """Capture the TelemetryEvent objects record_event would persist."""
    written: list = []
    session = MagicMock()
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.add.side_effect = written.append
    session.commit = MagicMock()
    with patch("app.telemetry.get_db_session", return_value=session):
        yield written


def test_telemetry_uses_authenticated_org_over_payload(capture_telemetry):
    """AC1 — inside a request, the authenticated org wins over any payload org_id."""
    with org_context("org_authn"):
        record_event("run.started", {"org_id": "org_from_payload", "source": "t"})
    assert capture_telemetry[0].org_id == "org_authn"


def test_telemetry_uses_payload_org_in_background(capture_telemetry):
    """AC1 — background emitters (no request context) attribute via payload org_id."""
    with org_context(None):
        record_event("run.started", {"org_id": "org_bg", "source": "run_pipeline"})
    assert capture_telemetry[0].org_id == "org_bg"


def test_telemetry_never_defaults_to_a_real_tenant(capture_telemetry):
    """AC3 — an event with no resolvable org is UNATTRIBUTED, not 'default'."""
    with org_context(None):
        record_event("run.started", {"source": "run_pipeline"})  # no org_id
    assert capture_telemetry[0].org_id == UNATTRIBUTED_ORG
    assert capture_telemetry[0].org_id != "default"


# ---------------------------------------------------------------------------
# Audit — log_event attribution (T5-AC2, T5-AC3)
# ---------------------------------------------------------------------------


def _new_org_id() -> str:
    return f"attrib-{uuid.uuid4().hex[:12]}"


def _read_audit_org(con_org: str) -> list[dict]:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT org_id, event_type, payload FROM audit_log WHERE org_id = %s",
            (con_org,),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [{"org_id": r[0], "event_type": r[1],
             "payload": json.loads(r[2]) if r[2] else None} for r in rows]


def test_audit_attributes_to_explicit_org_in_background():
    """AC2 — a background audit write (no context) is filed under its explicit org."""
    from app.middleware.audit import log_event

    org_id = _new_org_id()
    with org_context(None):
        log_event("connector_connected", org_id=org_id, connector_id="sf")
    rows = _read_audit_org(org_id)
    assert len(rows) == 1 and rows[0]["org_id"] == org_id


def test_audit_authenticated_context_wins_over_explicit_org():
    """AC2 — inside a request the authenticated org is authoritative."""
    from app.middleware.audit import log_event

    ctx_org = _new_org_id()
    with org_context(ctx_org):
        # Even if a caller passes a different org_id, context attribution wins.
        log_event("connector_connected", org_id="org_spoof", connector_id="sf")
    assert len(_read_audit_org(ctx_org)) == 1
    assert _read_audit_org("org_spoof") == []


def _audit_org_for_connector(connector_id: str) -> str | None:
    """Return the org_id the audit row for a (unique) connector_id was filed under."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT org_id FROM audit_log WHERE connector_id = %s", (connector_id,)
        )
        row = cur.fetchone()
    finally:
        con.close()
    return row[0] if row else None


def test_audit_never_defaults_to_a_real_tenant():
    """AC3 — an audit event with no resolvable org is UNATTRIBUTED, not 'default'.

    Uses a unique connector_id so the assertion is isolated from other rows in
    the shared audit_log table.
    """
    from app.middleware.audit import log_event

    marker = f"conn-{uuid.uuid4().hex[:12]}"
    with org_context(None):
        log_event("connector_disconnected", connector_id=marker)  # no org_id

    org = _audit_org_for_connector(marker)
    assert org == UNATTRIBUTED_ORG, "unresolved audit event must use the sentinel org"
    assert org != "default", "must never be filed under the real 'default' tenant"


# ---------------------------------------------------------------------------
# Emission points that previously dropped org_id now carry it (T5-AC1)
# ---------------------------------------------------------------------------


def test_execute_query_threads_org_id_into_telemetry():
    """End-to-end: the DB connector query path attributes both its audit and
    telemetry events to the org passed in (it runs in a background context)."""
    import sys

    import connectors.db.execute_query  # ensure the submodule is in sys.modules
    # connectors/db/__init__ re-exports the execute_query *function*, shadowing
    # the submodule attribute, so fetch the real module object via sys.modules.
    eq = sys.modules["connectors.db.execute_query"]

    tel: dict = {}
    aud: dict = {}

    def _cap_tel(event_type, payload=None):
        if event_type == "db.query_executed":
            tel.update(payload or {})

    def _cap_aud(event_type, **kwargs):
        aud["event_type"] = event_type
        aud.update(kwargs)

    # Fake connection/pool so no real DB is touched.
    cursor = MagicMock()
    cursor.fetchmany.return_value = []
    cursor.fetchall.return_value = []
    cursor.description = [("c", None)]
    connection = MagicMock()
    connection.cursor.return_value = cursor
    pool = MagicMock()
    pool.acquire.return_value = connection

    config = MagicMock()
    config.connector_id = "postgresql"
    config.driver = "psycopg2"
    scope = MagicMock()

    with patch.object(eq, "get_or_create_pool", return_value=pool), \
            patch.object(eq, "_ensure_driver_imported", lambda _c: None), \
            patch.object(eq, "validate_read_only", lambda _q: None), \
            patch.object(eq, "validate_scope", lambda _q, _s: None), \
            patch.object(eq, "record_event", side_effect=_cap_tel), \
            patch.object(eq, "log_event", side_effect=_cap_aud):
        eq.execute_query(
            config,
            query="SELECT 1",
            scope=scope,
            org_id="org_dbq",
            run_id="run_dbq",
        )

    assert tel.get("org_id") == "org_dbq", "db.query_executed telemetry must carry org_id"
    assert aud.get("org_id") == "org_dbq", "connector_queried audit must carry org_id"
