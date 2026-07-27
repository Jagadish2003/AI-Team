"""Regression cover for two live-instance defects in the MSP-B3/MSP-B11 rails.

Both were invisible offline (fixtures store plain scalars) and only appeared
against a real ServiceNow instance, where every field of a
``sysparm_display_value=all`` response arrives as
``{"value": <raw>, "display_value": <human>}``.

  1. MSP-B3 (CMDB, AC1) — the bounded class scope and the server-side
     ``sys_class_nameIN`` filter both speak canonical ``cmdb_ci_*`` identifiers,
     but the response's DISPLAY value is a human label ("Network Gear" for
     ``cmdb_ci_netgear``). Comparing the label against the scope made every
     in-scope CI look out of scope and failed the whole CMDB stream closed.
     Datetime fields have the same split: the raw value is ServiceNow's
     canonical ``YYYY-MM-DD HH:MM:SS`` UTC, the display value is rendered in the
     instance's format and the user's timezone — so checkpoints and every
     time-to-resolve calculation must read the raw value.

  2. MSP-B11 (SecOps) — the Security Incident Response and Vulnerability
     Response tables exist only where their plugins are activated. An instance
     without them answered every SecOps read with HTTP 400 "Invalid table",
     failing four streams on every run and rendering the full encoded query URL
     into run health. An absent module is a deployment fact, not an ingestion
     fault: it degrades to ``status='unavailable'`` with a named reason, no
     error, and no checkpoint movement.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from discovery.ingest import servicenow as sn
from discovery.ingest import set_ingest_org


NOW = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# MSP-B3 — display value vs raw value on cmdb_ci
# ─────────────────────────────────────────────────────────────────────────────


def _display_all_ci(sys_id: str, raw_class: str, label: str) -> dict:
    """A cmdb_ci record shaped exactly as ``display_value=all`` returns it."""
    return {
        "sys_id": {"value": sys_id, "display_value": sys_id},
        "name": {"value": f"name-{sys_id}", "display_value": f"name-{sys_id}"},
        "sys_class_name": {"value": raw_class, "display_value": label},
        "operational_status": {"value": "1", "display_value": "Operational"},
        "assignment_group": {"value": "grp-1", "display_value": "Network Operations"},
        "owned_by": {"value": "user-1", "display_value": "Ops Team"},
        "environment": {"value": "production", "display_value": "Production"},
        "sys_updated_on": {
            "value": "2026-07-24 10:49:55",
            # The instance renders dates in its own format and the user's zone.
            "display_value": "07/24/2026 06:49:55",
        },
    }


def test_in_scope_ci_admitted_when_class_arrives_as_a_display_label(monkeypatch):
    """'Network Gear' is cmdb_ci_netgear — an in-scope CI, not a scope breach."""

    class Client:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            return [
                _display_all_ci("ci-net-1", "cmdb_ci_netgear", "Network Gear"),
                _display_all_ci("ci-srv-1", "cmdb_ci_server", "Server"),
            ]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(
        sn, "_load_org_cmdb_config", lambda org_id: ["cmdb_ci_netgear", "cmdb_ci_server"]
    )
    set_ingest_org("org-display")

    items = sn.get_cmdb_configuration_items(Client())

    assert [item.ci_class for item in items] == ["cmdb_ci_netgear", "cmdb_ci_server"]
    # The canonical identifier reaches the graph, never the human label.
    assert all("gear" != item.ci_class for item in items)
    # Human labels are still preferred where a label IS the value.
    assert items[0].operational_status == "Operational"
    assert items[0].assignment_group == "Network Operations"


def test_ci_timestamp_uses_the_canonical_raw_value(monkeypatch):
    """Checkpoints and delta windows need UTC 'YYYY-MM-DD HH:MM:SS', not display."""

    class Client:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            return [_display_all_ci("ci-net-1", "cmdb_ci_netgear", "Network Gear")]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: ["cmdb_ci_netgear"])
    set_ingest_org("org-display-ts")

    (item,) = sn.get_cmdb_configuration_items(Client())

    assert item.updated_at == "2026-07-24 10:49:55"
    # Parsing it back with the cursor format is what the checkpoint path does.
    assert sn._validated_cmdb_cursor(item.updated_at) == "2026-07-24 10:49:55"


def test_genuinely_out_of_scope_class_still_fails_closed(monkeypatch):
    """The bounded-scope guard survives the fix — it just compares raw to raw."""

    class Client:
        instance_url = "https://acme.service-now.com"

        def table_query(self, table, params, max_records):
            return [_display_all_ci("ci-p-1", "cmdb_ci_printer", "Printer")]

    monkeypatch.setattr(sn, "is_live", lambda: True)
    monkeypatch.setattr(sn, "_load_org_cmdb_config", lambda org_id: ["cmdb_ci_server"])
    set_ingest_org("org-bounded-display")

    with pytest.raises(sn.ServiceNowIngestError, match="out-of-scope"):
        sn.get_cmdb_configuration_items(Client())


# ─────────────────────────────────────────────────────────────────────────────
# MSP-B11 — a SecOps module that is not activated on this instance
# ─────────────────────────────────────────────────────────────────────────────


class _Response:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.reason = "Bad Request"
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _HTTPError(response=self)


class _HTTPError(Exception):
    def __init__(self, response):
        super().__init__("400 Client Error: Bad Request for url: https://x/api/now/...")
        self.response = response


class _Session:
    """Answers like an instance with no Security Operations plugins."""

    def __init__(self, missing_tables):
        self.missing_tables = set(missing_tables)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        table = url.rsplit("/", 1)[-1]
        self.calls.append(table)
        if table in self.missing_tables:
            return _Response(
                400,
                {
                    "error": {
                        "message": "Invalid table sn_si_incident",
                        "detail": None,
                    },
                    "status": "failure",
                },
            )
        return _Response(200, {"result": []})


def _client_without_secops(missing_tables):
    client = sn.ServiceNowClient("https://dev1.service-now.com", token="t")
    client._session = _Session(missing_tables)
    return client


def test_missing_table_is_classified_as_unavailable_not_a_query_failure():
    client = _client_without_secops({sn.SIR_TABLE})

    with pytest.raises(sn.ServiceNowTableUnavailable) as excinfo:
        client.table_query(sn.SIR_TABLE, {"sysparm_fields": "sys_id"})

    error = excinfo.value
    assert error.table == sn.SIR_TABLE
    assert "not activated" in error.reason
    # The encoded query URL never reaches the message — it is what made run
    # health unreadable.
    assert "sysparm_query" not in str(error)
    assert "service-now.com/api" not in str(error)


def test_forbidden_table_is_unavailable_with_a_role_reason():
    client = sn.ServiceNowClient("https://dev1.service-now.com", token="t")

    class Forbidden(_Session):
        def get(self, url, params=None, timeout=None):
            return _Response(403, {"error": {"message": "Insufficient rights"}})

    client._session = Forbidden(set())

    with pytest.raises(sn.ServiceNowTableUnavailable) as excinfo:
        client.table_query(sn.VR_VULN_ITEM_TABLE, {})

    assert "role" in excinfo.value.reason


def test_table_availability_is_probed_once_per_client():
    client = _client_without_secops({sn.SIR_TABLE})

    first = client.table_available(sn.SIR_TABLE)
    second = client.table_available(sn.SIR_TABLE)

    assert first == second
    assert first[0] is False
    assert client._session.calls == [sn.SIR_TABLE]  # memoized, not re-probed


def test_sir_ingest_degrades_to_unavailable_without_touching_the_checkpoint(monkeypatch):
    monkeypatch.setattr(sn, "is_live", lambda: True)
    client = _client_without_secops({sn.SIR_TABLE})
    saved = []

    result = sn.ingest_sir_changes(
        org_id="org-1",
        run_id="run-1",
        client=client,
        clock=lambda: NOW,
        read_checkpoint=lambda org, connector: None,
        save_checkpoint=lambda cp: saved.append(cp),
    )

    stream = result["streams"][sn.SIR_TABLE]
    assert stream["status"] == "unavailable"
    assert stream["error"] is None
    assert stream["checkpoint_advanced"] is False
    assert "not activated" in stream["unavailable_reason"]
    assert result["security_incidents"] == []
    assert saved == []  # nothing to resume past — the table was never read


def test_vr_streams_degrade_independently(monkeypatch):
    """One absent VR table must not decide the fate of the others."""
    monkeypatch.setattr(sn, "is_live", lambda: True)
    client = _client_without_secops({sn.VR_VULN_ITEM_TABLE})
    saved = []

    result = sn.ingest_vr_changes(
        org_id="org-1",
        run_id="run-1",
        client=client,
        clock=lambda: NOW,
        read_checkpoint=lambda org, connector: None,
        save_checkpoint=lambda cp: saved.append(cp),
    )

    assert result["streams"][sn.VR_VULN_ITEM_TABLE]["status"] == "unavailable"
    # The other two tables answered normally (empty), so they ran as usual.
    assert result["streams"][sn.VR_GROUP_TABLE]["status"] == "ok"
    assert result["streams"][sn.VR_REMEDIATION_TASK_TABLE]["status"] == "ok"


def test_a_real_query_failure_is_still_an_error(monkeypatch):
    """Degrading an absent table must not swallow genuine failures."""
    client = sn.ServiceNowClient("https://dev1.service-now.com", token="t")

    class Broken(_Session):
        def get(self, url, params=None, timeout=None):
            return _Response(500, {"error": {"message": "Instance unavailable"}})

    client._session = Broken(set())

    with pytest.raises(sn.ServiceNowIngestError) as excinfo:
        client.table_query(sn.SIR_TABLE, {})

    assert not isinstance(excinfo.value, sn.ServiceNowTableUnavailable)
    assert "Instance unavailable" in str(excinfo.value)
