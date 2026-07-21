"""R191-H1 / T1 (AC1) — fail-closed credential misses surface in run connector health.

The last leg of AC1: when the operational-app ingestor skips a target for a missing
vault credential, that miss must be visible in the run's ``connector_health`` KV — the
same store the connector-health API and S1 badges read — with an actionable reason.

These tests drive the runner's ``_surface_operational_credential_health`` helper and
the Java/.NET corroboration wrappers with in-memory KV + ingestor seams (no DB, no
live HTTP), asserting the health record lands keyed per target and carries no secret.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from discovery import runner as runner_mod
from discovery.ingest.operational_config import (
    OperationalCredentialMissing,
    credential_missing_health,
)

RUN = "run-h1"


class _KV:
    """In-memory stand-in for the run-scoped KV (connector_health)."""

    def __init__(self):
        self.store: Dict[str, Any] = {}

    def get(self, key: str, run_id: str, default: Any = None) -> Any:
        return self.store.get((key, run_id), default)

    def set(self, key: str, run_id: str, value: Any) -> None:
        self.store[(key, run_id)] = value


@pytest.fixture()
def kv(monkeypatch):
    _kv = _KV()
    # The helper imports run_kv_get/run_kv_set from app.db lazily; patch there.
    import app.db as db
    monkeypatch.setattr(db, "run_kv_get", _kv.get)
    monkeypatch.setattr(db, "run_kv_set", _kv.set)
    return _kv


def _health(app_id: str, credential_ref: str = "java_app", system: str = "Java Application"):
    return credential_missing_health(
        system=system,
        exc=OperationalCredentialMissing(
            org_id="org-1", app_id=app_id, credential_ref=credential_ref
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# _surface_operational_credential_health
# ─────────────────────────────────────────────────────────────────────────────
def test_surface_writes_actionable_health_keyed_per_target(kv):
    runner_mod._surface_operational_credential_health(
        RUN, [_health("payments-api"), _health("orders-api", system=".NET Application")]
    )
    stored = kv.get("connector_health", RUN)
    assert set(stored) == {"Java Application (payments-api)", ".NET Application (orders-api)"}
    rec = stored["Java Application (payments-api)"]
    assert rec["status"] == "error"
    assert rec["isLive"] is False
    assert "payments-api" in rec["message"] and "java_app" in rec["message"]


def test_surface_merges_with_existing_health(kv):
    # Pre-existing SaaS health (e.g. from check_all_connectors) must be preserved.
    kv.set("connector_health", RUN, {"Jira": {"system": "Jira", "status": "live"}})
    runner_mod._surface_operational_credential_health(RUN, [_health("payments-api")])
    stored = kv.get("connector_health", RUN)
    assert "Jira" in stored                                  # untouched
    assert "Java Application (payments-api)" in stored       # added


def test_surface_noop_when_no_health_records(kv):
    runner_mod._surface_operational_credential_health(RUN, [])
    assert kv.get("connector_health", RUN) is None


def test_surface_is_non_blocking_on_kv_error(monkeypatch):
    import app.db as db

    def _boom(*a, **k):
        raise RuntimeError("kv down")

    monkeypatch.setattr(db, "run_kv_get", _boom)
    monkeypatch.setattr(db, "run_kv_set", _boom)
    # Must not raise — surfacing health is best-effort.
    runner_mod._surface_operational_credential_health(RUN, [_health("payments-api")])


def test_surface_carries_only_safe_identifiers(kv):
    # The health record is built from safe identifiers only (org / app / ref) — no
    # secret value is ever attached. Prove the stored record's keys are exactly the
    # safe, credential-free set.
    runner_mod._surface_operational_credential_health(RUN, [_health("payments-api")])
    rec = kv.get("connector_health", RUN)["Java Application (payments-api)"]
    assert set(rec) == {
        "system", "status", "message", "latencyMs", "isLive",
        "appId", "credentialRef", "orgId",
    }
    # None of the safe fields is a secret; the ref is a vault KEY name, not a value.
    assert rec["credentialRef"] == "java_app"
    assert rec["orgId"] == "org-1"


# ─────────────────────────────────────────────────────────────────────────────
# The corroboration wrappers surface an ingestor's credential_health
# ─────────────────────────────────────────────────────────────────────────────
class _FakeResult:
    error = None
    first_run = True
    batches = 0
    records = 0
    checkpoint_advanced = False


def _fake_ingestor_with_health(health: List[Dict[str, Any]]):
    class _I:
        credential_health = health
    return _I()


def test_java_corroboration_surfaces_credential_health(kv, monkeypatch):
    health = [_health("payments-api")]

    # Stub the ingestor class + change_runner so no real ingest/DB happens; the
    # ingestor instance carries the health we expect to be surfaced.
    fake = _fake_ingestor_with_health(health)
    monkeypatch.setattr(
        "discovery.ingest.java_app.JavaAppIngestor", lambda: fake
    )
    monkeypatch.setattr(
        "discovery.ingest.change_runner.ingest_with_checkpoint",
        lambda ingestor, org_id, process_batch=None: _FakeResult(),
    )
    monkeypatch.setattr(
        "discovery.ingest.java_app_signals.build_java_app_corroboration_payload",
        lambda collected: {"java_app": {}},
    )

    runner_mod._ingest_java_app_corroboration("org-1", RUN)

    stored = kv.get("connector_health", RUN)
    assert stored and "Java Application (payments-api)" in stored


def test_dotnet_corroboration_surfaces_credential_health(kv, monkeypatch):
    health = [_health("orders-api", credential_ref="dotnet_app", system=".NET Application")]
    fake = _fake_ingestor_with_health(health)
    monkeypatch.setattr(
        "discovery.ingest.dotnet_app.DotNetAppIngestor", lambda: fake
    )
    monkeypatch.setattr(
        "discovery.ingest.change_runner.ingest_with_checkpoint",
        lambda ingestor, org_id, process_batch=None: _FakeResult(),
    )
    monkeypatch.setattr(
        "discovery.ingest.dotnet_app_signals.build_dotnet_app_corroboration_payload",
        lambda collected: {"dotnet_app": {}},
    )

    runner_mod._ingest_dotnet_app_corroboration("org-1", RUN)

    stored = kv.get("connector_health", RUN)
    assert stored and ".NET Application (orders-api)" in stored
