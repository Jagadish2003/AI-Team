"""R191-H1 / T1 (AC1) — operational-app credential path fails CLOSED on a vault miss.

The Release 1.8 verification's critical F1 finding: the Java/.NET operational-app
credential path fell back to ``os.environ`` when the vault lookup missed — the exact
env-fallback pattern R17-D3 Addendum A eliminated everywhere else.

T1 removes that fallback. A vault miss is now **fail-closed**:

  * :func:`operational_config.resolve_target_secret` raises
    :class:`OperationalCredentialMissing` on a miss — it never reads the environment;
  * the shared :class:`OperationalChangeIngestor` catches that per target, skips ONLY
    that target (the run continues for other targets), leaves its cursor untouched
    (retried once the credential is connected), and records an actionable,
    credential-free connector-health entry naming the org, the target, and the
    credential ref.

These tests exercise the shared base directly (no DB, no live HTTP) so the behaviour
is proven independent of either platform's collection edge.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

from discovery.ingest.base import ChangeKind
from discovery.ingest.operational_config import (
    OperationalCredentialMissing,
    credential_missing_health,
    resolve_target_secret,
)
from discovery.ingest.operational_ingest import OperationalChangeIngestor


ORG = "org-h1"


class _Target:
    """Minimal operational target: an app_id, a service, and a credential_ref."""

    def __init__(self, app_id: str, *, credential_ref: str | None = "java_app"):
        self.app_id = app_id
        self.service = app_id
        self.credential_ref = credential_ref
        self.actuator_url = f"https://{app_id}/actuator"
        self.log_source = f"https://{app_id}/logs"


class _FakeIngestor(OperationalChangeIngestor):
    """Shared base wired to in-memory targets, with a vault whose contents are injectable.

    ``_raw_operational`` resolves the target's credential exactly as the real
    ingestors do (via :func:`resolve_target_secret`) — so a target whose credential
    is absent from the injected ``vault`` fails closed, just like live mode. When the
    credential resolves, the target yields one metric sample so we can prove the
    non-failing targets still ingest.
    """

    connector_id = "java_app"
    source_system = "java_app"
    health_system = "Java Application"

    def __init__(self, targets: List[_Target], vault: Dict[str, Dict[str, str]]):
        super().__init__()
        self._targets = targets
        self._vault = vault

    def _load_targets(self, org_id: str) -> List[_Target]:
        return list(self._targets)

    def _raw_operational(self, org_id: str, target: _Target) -> Dict[str, Any]:
        # Fail-closed on a vault miss — mirrors the live _client() path.
        resolve_target_secret(
            org_id,
            app_id=target.app_id,
            credential_ref=target.credential_ref,
            connector_lookup=lambda ref: self._vault.get(ref),
        )
        return {
            "metrics": [{"sample_ts": "2026-06-10T08:00:00+00:00", "health": "UP"}],
            "logs": [],
        }

    def _to_metric_record(self, target: _Target, sample: Dict[str, Any], seq_index: int = 0):
        return self._metric_record(
            target, sample, seq_index,
            endpoint_field="actuator_url", endpoint_url=target.actuator_url,
        )

    def _to_log_record(self, target: _Target, entry: Dict[str, Any]):
        return self._log_record(target, entry, log_source=target.log_source)


def _drive(ingestor: OperationalChangeIngestor):
    """Flatten the ingestor's delta batches into a record list."""
    batches = list(ingestor.ingest_changes(ORG, None))
    return [r for b in batches for r in b.records]


# ─────────────────────────────────────────────────────────────────────────────
# resolve_target_secret — fail closed, never touch the environment
# ─────────────────────────────────────────────────────────────────────────────
def test_resolve_fails_closed_and_ignores_env(monkeypatch):
    # An env token present must be irrelevant — a vault miss fails closed.
    monkeypatch.setenv("JAVA_APP_TOKEN", "ENV-TOKEN-SHOULD-NEVER-BE-USED")
    with pytest.raises(OperationalCredentialMissing) as exc:
        resolve_target_secret(
            ORG,
            app_id="payments-api",
            credential_ref="java_app",
            connector_lookup=lambda ref: None,   # nothing in the vault
        )
    assert exc.value.org_id == ORG
    assert exc.value.app_id == "payments-api"
    assert exc.value.credential_ref == "java_app"
    assert "ENV-TOKEN-SHOULD-NEVER-BE-USED" not in str(exc.value)


def test_resolve_returns_token_when_vault_has_it():
    token = resolve_target_secret(
        ORG,
        app_id="payments-api",
        credential_ref="java_app",
        connector_lookup=lambda ref: {"token": "  VAULT-XYZ  "},
    )
    assert token == "VAULT-XYZ"  # stripped


def test_resolve_none_credential_ref_needs_no_secret():
    assert resolve_target_secret(
        ORG, app_id="a", credential_ref=None, connector_lookup=lambda ref: None
    ) is None


def test_empty_vault_token_fails_closed():
    with pytest.raises(OperationalCredentialMissing):
        resolve_target_secret(
            ORG, app_id="a", credential_ref="java_app",
            connector_lookup=lambda ref: {"token": ""},   # present but empty → miss
        )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — the ingestor fail-closes for the missing target, continues for the rest,
# and surfaces an actionable connector-health record
# ─────────────────────────────────────────────────────────────────────────────
def test_missing_credential_skips_only_that_target(monkeypatch):
    monkeypatch.setenv("JAVA_APP_TOKEN", "ENV-TOKEN-SHOULD-NEVER-BE-USED")
    # payments-api references a ref the vault does not hold (miss → fail closed);
    # orders-api references a ref the vault DOES hold (ingests normally).
    ingestor = _FakeIngestor(
        targets=[
            _Target("payments-api", credential_ref="payments_ref"),
            _Target("orders-api", credential_ref="orders_ref"),
        ],
        vault={"orders_ref": {"token": "OK"}},   # only orders-api is credentialled
    )

    records = _drive(ingestor)

    # The healthy target ingested; the failed one produced nothing.
    app_ids = {r["app_id"] for r in records}
    assert app_ids == {"orders-api"}
    assert all(r["change_kind"] == ChangeKind.CREATED for r in records)
    # And the miss is surfaced for the skipped target only.
    assert {h["appId"] for h in ingestor.credential_health} == {"payments-api"}


def test_missing_credential_surfaces_actionable_health():
    ingestor = _FakeIngestor(
        targets=[_Target("payments-api", credential_ref="java_app")],
        vault={},  # empty vault → miss
    )
    _drive(ingestor)

    assert len(ingestor.credential_health) == 1
    health = ingestor.credential_health[0]
    assert health["system"] == "Java Application"
    assert health["status"] == "error"
    assert health["isLive"] is False
    # Actionable: names the target and the credential ref.
    assert health["appId"] == "payments-api"
    assert health["credentialRef"] == "java_app"
    assert "payments-api" in health["message"]
    assert "java_app" in health["message"]


def test_health_slate_is_reset_each_pass():
    ingestor = _FakeIngestor(
        targets=[_Target("payments-api", credential_ref="java_app")],
        vault={},
    )
    _drive(ingestor)
    assert len(ingestor.credential_health) == 1
    # Fix the credential and re-run: the prior miss must not linger.
    ingestor._vault = {"java_app": {"token": "OK"}}
    _drive(ingestor)
    assert ingestor.credential_health == []


def test_all_targets_missing_yields_empty_but_records_all_health():
    ingestor = _FakeIngestor(
        targets=[_Target("a", credential_ref="java_app"),
                 _Target("b", credential_ref="java_app")],
        vault={},
    )
    records = _drive(ingestor)
    assert records == []                       # nothing ingested
    assert {h["appId"] for h in ingestor.credential_health} == {"a", "b"}


# ─────────────────────────────────────────────────────────────────────────────
# The health record shape carries no secret and matches the ConnectorHealth dict
# ─────────────────────────────────────────────────────────────────────────────
def test_credential_missing_health_shape_and_no_secret():
    exc = OperationalCredentialMissing(
        org_id=ORG, app_id="orders-api", credential_ref="orders_ref"
    )
    rec = credential_missing_health(system=".NET Application", exc=exc)
    assert set(rec) >= {"system", "status", "message", "latencyMs", "isLive"}
    assert rec["system"] == ".NET Application"
    assert rec["status"] == "error"
    assert rec["latencyMs"] is None
    assert rec["isLive"] is False
    assert rec["appId"] == "orders-api"
    assert rec["credentialRef"] == "orders_ref"
    assert rec["orgId"] == ORG
