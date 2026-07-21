"""R191-H1 / T5 — Consolidated acceptance suite for Credential-Path Hardening (AC1–AC5).

This module is the single, auditable entry-point that maps every R191-H1
acceptance criterion (Section 2 of the 1.9.1 release stories) to a concrete,
self-contained assertion against the built behaviour — not against the task
list. Tasks T1–T4 each shipped their own focused tests alongside the code they
introduced; T5 consolidates them here so a verifier can confirm "all H1 ACs
pass" from one file, and fills the coverage gaps those per-task tests left.

Acceptance criteria (verbatim intent)
--------------------------------------
AC1  A missing vault credential for an operational-app target fail-closes: no
     env read occurs, the run continues for other targets, and connector
     health shows the failed target with an actionable reason.
AC2  No module under ``backend/discovery/ingest/`` reads credential-shaped keys
     from the environment except allow-listed, justified entries — enforced by
     the generalised guard test, which dynamically discovers modules.
AC3  Adding a new ingest module containing ``os.getenv("X_TOKEN")`` makes CI
     fail without any test edit.
AC4  Salesforce/Jira connect using the URL from the credential record; a record
     missing its URL produces a loud configuration error naming the record —
     never a silent env default.
AC5  Under the production deployment profile, the customer-tenant provider never
     reads ``CUSTOMER_TENANT_API_KEY`` from env (vault only); under dev,
     unchanged.

Each class below re-asserts one AC directly. Where a class adds coverage that
the T1–T4 tests did not have, the method docstring says so ("gap-fill"). The
authoritative per-task suites remain the primary evidence and are untouched:

  * AC1 — discovery/tests/test_operational_credential_fail_closed.py
                 test_operational_credential_health_surfacing.py
  * AC2/AC3 — tests/contract/test_no_env_credential_reads.py (Section B)
  * AC4 — discovery/tests/test_sf22_salesforce_ingest.py
                 test_sf24_jira_ingest.py
  * AC5 — tests/contract/test_customer_tenant_vault.py
                 test_customer_tenant_startup_validation.py

FAKE CREDENTIALS: every ``*-FAKE-*`` / ``env-key-*`` value below is a non-real,
test-only string. None is a live credential, and none is ever logged.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List

import pytest

# Resolves to backend/
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


# =============================================================================
# AC1 — operational-app credential path fails CLOSED on a vault miss.
# =============================================================================


class _Target:
    """Minimal operational target: app_id, service, and a credential_ref."""

    def __init__(self, app_id: str, *, credential_ref: str | None = "java_app"):
        self.app_id = app_id
        self.service = app_id
        self.credential_ref = credential_ref
        self.actuator_url = f"https://{app_id}/actuator"
        self.log_source = f"https://{app_id}/logs"


def _fake_ingestor(targets: List[_Target], vault: Dict[str, Dict[str, str]]):
    """Wire the SHARED OperationalChangeIngestor base to in-memory targets + vault.

    ``_raw_operational`` resolves each target's credential exactly as the live
    ingestors do (via ``resolve_target_secret``), so a target whose credential is
    absent from ``vault`` fails closed identically to live mode — no DB, no HTTP.
    """
    from discovery.ingest.operational_config import resolve_target_secret
    from discovery.ingest.operational_ingest import OperationalChangeIngestor

    class _FakeIngestor(OperationalChangeIngestor):
        connector_id = "java_app"
        source_system = "java_app"
        health_system = "Java Application"

        def _load_targets(self, org_id: str) -> List[_Target]:
            return list(targets)

        def _raw_operational(self, org_id: str, target: _Target) -> Dict[str, Any]:
            resolve_target_secret(
                org_id,
                app_id=target.app_id,
                credential_ref=target.credential_ref,
                connector_lookup=lambda ref: vault.get(ref),
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

    return _FakeIngestor()


def _drive(ingestor) -> List[Dict[str, Any]]:
    batches = list(ingestor.ingest_changes("org-h1-t5", None))
    return [r for b in batches for r in b.records]


class TestAC1_FailClosedVaultMiss:
    """AC1: a vault miss fails closed, never reads env, and surfaces health."""

    def test_resolve_raises_and_never_reads_env(self, monkeypatch):
        """resolve_target_secret raises on a miss and ignores an env token."""
        from discovery.ingest.operational_config import (
            OperationalCredentialMissing,
            resolve_target_secret,
        )

        # An env token present must be irrelevant — the vault miss fails closed.
        monkeypatch.setenv("JAVA_APP_TOKEN", "ENV-FAKE-TOKEN-NEVER-USED")
        with pytest.raises(OperationalCredentialMissing) as exc:
            resolve_target_secret(
                "org-h1-t5",
                app_id="payments-api",
                credential_ref="java_app",
                connector_lookup=lambda ref: None,  # empty vault
            )
        assert exc.value.app_id == "payments-api"
        assert exc.value.credential_ref == "java_app"
        assert "ENV-FAKE-TOKEN-NEVER-USED" not in str(exc.value)

    def test_no_credential_ref_needs_no_secret(self):
        """A target with no credential_ref resolves to None (unauthenticated)."""
        from discovery.ingest.operational_config import resolve_target_secret

        assert resolve_target_secret(
            "org-h1-t5", app_id="a", credential_ref=None,
            connector_lookup=lambda ref: None,
        ) is None

    def test_run_continues_for_other_targets(self, monkeypatch):
        """The missing-credential target is skipped; other targets still ingest."""
        from discovery.ingest.base import ChangeKind

        monkeypatch.setenv("JAVA_APP_TOKEN", "ENV-FAKE-TOKEN-NEVER-USED")
        ingestor = _fake_ingestor(
            targets=[
                _Target("payments-api", credential_ref="payments_ref"),  # miss
                _Target("orders-api", credential_ref="orders_ref"),      # ok
            ],
            vault={"orders_ref": {"token": "OK"}},
        )
        records = _drive(ingestor)
        assert {r["app_id"] for r in records} == {"orders-api"}
        assert all(r["change_kind"] == ChangeKind.CREATED for r in records)
        assert {h["appId"] for h in ingestor.credential_health} == {"payments-api"}

    def test_health_record_is_actionable_and_secret_free(self):
        """The connector-health record names the target + credential ref, no secret."""
        ingestor = _fake_ingestor(
            targets=[_Target("payments-api", credential_ref="java_app")],
            vault={},  # miss
        )
        _drive(ingestor)
        assert len(ingestor.credential_health) == 1
        h = ingestor.credential_health[0]
        assert h["system"] == "Java Application"
        assert h["status"] == "error"
        assert h["isLive"] is False
        assert h["appId"] == "payments-api"
        assert h["credentialRef"] == "java_app"        # a vault KEY name, not a secret
        assert "payments-api" in h["message"]
        assert "java_app" in h["message"]

    def test_miss_surfaces_into_run_connector_health_kv(self, monkeypatch):
        """gap-fill glue: the runner helper writes the miss into the run's
        connector_health KV keyed per target (the store the health API reads)."""
        from discovery import runner as runner_mod
        from discovery.ingest.operational_config import (
            OperationalCredentialMissing,
            credential_missing_health,
        )

        store: Dict[Any, Any] = {}
        import app.db as db
        monkeypatch.setattr(db, "run_kv_get", lambda k, r, default=None: store.get((k, r), default))
        monkeypatch.setattr(db, "run_kv_set", lambda k, r, v: store.__setitem__((k, r), v))

        rec = credential_missing_health(
            system="Java Application",
            exc=OperationalCredentialMissing(
                org_id="org-h1-t5", app_id="payments-api", credential_ref="java_app"
            ),
        )
        runner_mod._surface_operational_credential_health("run-t5", [rec])
        stored = store[("connector_health", "run-t5")]
        assert "Java Application (payments-api)" in stored
        assert stored["Java Application (payments-api)"]["status"] == "error"


# =============================================================================
# AC2 / AC3 — the generalised guard test dynamically sweeps the ingest layer.
# The authoritative implementation lives in test_no_env_credential_reads.py;
# here we drive its public helpers so this module fails too if the guard is
# ever weakened, and so the AC→assertion mapping is complete in one place.
# =============================================================================


def _load_guard_module():
    """Import the guard test module by path (it is a sibling test file)."""
    path = _BACKEND_ROOT / "tests" / "contract" / "test_no_env_credential_reads.py"
    spec = importlib.util.spec_from_file_location("_r191_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestAC2_GuardCoversIngestLayer:
    """AC2: the ingest layer is clean today, enforced by dynamic discovery."""

    def test_ingest_layer_has_no_unjustified_env_reads(self):
        """No unjustified credential-shaped/dynamic env read under discovery/ingest/."""
        guard = _load_guard_module()
        offenders = guard._unjustified_ingest_violations()
        assert offenders == [], "\n".join(offenders)

    def test_scan_is_dynamic_not_an_enumerated_list(self):
        """The guard walks the tree (no fixed module list), so it reaches modules
        added after it was written — including a nested subpackage."""
        guard = _load_guard_module()
        swept = {p.resolve() for p in guard._iter_ingest_python_files(guard.INGEST_ROOT)}
        for rel in ("salesforce.py", "jira.py", "operational_config.py", "extraction/pdf.py"):
            assert (guard.INGEST_ROOT / rel).resolve() in swept

    def test_allowlist_has_no_stale_entries(self):
        """Every allow-list entry still corresponds to a real read — the fence
        tracks the code rather than accumulating dead exemptions."""
        guard = _load_guard_module()
        live = set()
        for py_file in guard._iter_ingest_python_files(guard.INGEST_ROOT):
            rel = py_file.relative_to(guard.INGEST_ROOT).as_posix()
            for finding in guard._scan_ingest_module(py_file):
                live.add(guard._allowlist_key(rel, finding))
        stale = [f"{p} / {m}" for p, m in guard._INGEST_ALLOWLIST if (p, m) not in live]
        assert stale == [], f"stale allow-list entries: {stale}"


class TestAC3_UnlistedEnvReadFailsCI:
    """AC3: a new ingest module with an unlisted env read fails CI, no test edit."""

    def test_new_module_with_token_read_is_flagged(self, tmp_path):
        """A brand-new module reading os.getenv("X_TOKEN") is caught by the sweep
        that walks the tree at test time — proving no test edit is needed."""
        guard = _load_guard_module()
        (tmp_path / "some_future_connector.py").write_text(
            "import os\n\ndef get_token():\n    return os.getenv('FUTURE_CONNECTOR_TOKEN')\n",
            encoding="utf-8",
        )
        offenders = guard._unjustified_ingest_violations(root=tmp_path)
        assert any("FUTURE_CONNECTOR_TOKEN" in o for o in offenders)

    def test_dynamic_variable_keyed_read_is_flagged_like_f1(self, tmp_path):
        """The exact 1.8 F1 shape — a credential resolved via a variable-keyed
        os.environ.get(key), not a literal — is flagged (Section A missed this)."""
        guard = _load_guard_module()
        (tmp_path / "rogue_operational_config.py").write_text(
            "import os\n\n"
            "def resolve_target_secret(ref):\n"
            "    key = ref.upper() + '_TOKEN'\n"
            "    return os.environ.get(key)\n",
            encoding="utf-8",
        )
        offenders = guard._unjustified_ingest_violations(root=tmp_path)
        assert any("resolve_target_secret" in o for o in offenders)

    def test_new_credential_var_in_allowlisted_file_still_fails(self, tmp_path):
        """The allow-list is keyed per (file, name): justifying one var in a file
        never blanket-exempts the whole file, so a NEW credential-shaped var in an
        already-listed file still fails."""
        guard = _load_guard_module()
        (tmp_path / "confluence.py").write_text(
            "import os\nurl = os.getenv('CONFLUENCE_API_KEY')\n", encoding="utf-8"
        )
        offenders = guard._unjustified_ingest_violations(root=tmp_path)
        assert any("CONFLUENCE_API_KEY" in o for o in offenders)


# =============================================================================
# AC4 — Salesforce/Jira connect using the credential-record URL; a record
# missing its URL is a loud, named error, never a silent env default.
# =============================================================================


class TestAC4_CredentialRecordUrlEnforcement:
    """AC4: connection URL comes from the credential record only."""

    def test_salesforce_url_from_record_not_env(self, monkeypatch):
        import discovery.ingest as ingest_pkg
        from discovery.ingest import salesforce as sf_mod

        monkeypatch.setattr(sf_mod, "is_live", lambda: True)
        monkeypatch.setenv("SF_INSTANCE_URL", "https://env-should-never-be-used")
        monkeypatch.setattr(
            ingest_pkg, "get_live_connector",
            lambda cid: {"url": "https://record.my.salesforce.com", "token": "tok"}
            if cid == "salesforce" else None,
        )
        monkeypatch.setattr(ingest_pkg, "resolve_vault_connector", lambda cid: None)

        client = sf_mod._get_client()
        assert client is not None
        assert client.instance_url == "https://record.my.salesforce.com"

    def test_salesforce_missing_url_raises_named_error_no_env_leak(self, monkeypatch):
        import discovery.ingest as ingest_pkg
        from discovery.ingest import salesforce as sf_mod

        monkeypatch.setattr(sf_mod, "is_live", lambda: True)
        monkeypatch.setenv("SF_INSTANCE_URL", "https://env-should-never-be-used")
        monkeypatch.setattr(
            ingest_pkg, "get_live_connector",
            lambda cid: {"token": "tok"} if cid == "salesforce" else None,
        )
        monkeypatch.setattr(ingest_pkg, "resolve_vault_connector", lambda cid: None)

        with pytest.raises(sf_mod.IngestError) as exc:
            sf_mod._get_client()
        msg = str(exc.value)
        assert "instance URL" in msg
        assert "salesforce" in msg                      # names the record
        assert "env-should-never-be-used" not in msg    # never uses/leaks the env value

    def test_jira_missing_url_raises_named_error_no_env_leak(self, monkeypatch):
        monkeypatch.setenv("INGEST_MODE", "live")
        monkeypatch.setenv("JIRA_URL", "https://env-should-never-be-used")
        import importlib
        import discovery.ingest as pkg
        import discovery.ingest.jira as jira_mod
        importlib.reload(pkg)
        importlib.reload(jira_mod)
        monkeypatch.setattr(
            pkg, "get_live_connector",
            lambda cid: {"token": "tok"} if cid == "jira" else None,
        )
        monkeypatch.setattr(pkg, "resolve_vault_connector", lambda cid: None)

        try:
            with pytest.raises(jira_mod.JiraIngestError) as exc:
                jira_mod._get_client()
            msg = str(exc.value)
            assert "base URL" in msg
            assert "jira" in msg                             # names the record
            assert "env-should-never-be-used" not in msg
        finally:
            # Reload back to a clean offline state so module globals do not leak.
            monkeypatch.setenv("INGEST_MODE", "offline")
            importlib.reload(pkg)
            importlib.reload(jira_mod)


# =============================================================================
# AC5 — under the production profile the customer-tenant provider never reads
# CUSTOMER_TENANT_API_KEY from env; under dev, unchanged.
# =============================================================================


_FAKE_ENV_KEY = "env-FAKE-KEY-never-used-in-prod"


class TestAC5_CustomerTenantProductionProfile:
    """AC5: env fallback is impossible under production; dev behaviour unchanged."""

    def test_is_production_recognises_both_signals(self, monkeypatch):
        """Either ENVIRONMENT=production or REQUIRE_CONNECTOR_SECRETS=1 is prod."""
        from app.deployment_profile import is_production

        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
        assert is_production() is False

        monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
        assert is_production() is True

        monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "production")
        assert is_production() is True

        monkeypatch.setenv("ENVIRONMENT", "staging")  # anything else is dev
        assert is_production() is False

    def test_resolve_returns_empty_in_prod_with_env_only(self, monkeypatch):
        """Production + env-only credential (no usable vault) → "" (env ignored)."""
        from app.middleware import tenancy
        from app.model_gateway.customer_tenant_config import CONFIG_KEY_API_KEY
        from app.model_gateway.customer_tenant_vault import resolve_customer_tenant_api_key

        monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
        monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)
        monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_ENV_KEY)
        token = tenancy._current_org_id.set("org-t5-prod")
        try:
            assert resolve_customer_tenant_api_key() == ""
        finally:
            tenancy._current_org_id.reset(token)

    def test_resolve_uses_env_in_dev(self, monkeypatch):
        """Dev/standalone + env credential (no usable vault) → the env value is used."""
        from app.middleware import tenancy
        from app.model_gateway.customer_tenant_config import CONFIG_KEY_API_KEY
        from app.model_gateway.customer_tenant_vault import resolve_customer_tenant_api_key

        monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)
        monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_ENV_KEY)
        token = tenancy._current_org_id.set("org-t5-dev")
        try:
            assert resolve_customer_tenant_api_key() == _FAKE_ENV_KEY
        finally:
            tenancy._current_org_id.reset(token)

    def test_provider_makes_no_network_call_in_prod_with_env_only(self, monkeypatch):
        """gap-fill: 'impossible under production' proven end-to-end. In prod with
        an env-only credential the provider short-circuits to ok=False / [] BEFORE
        any HTTP call — the strongest form of AC5. If the env fallback ever leaked
        back in, urlopen would be reached and this test would fail loudly."""
        import app.model_gateway.customer_tenant_provider as prov_mod
        from app.model_gateway._interface import GenerationRequest
        from app.model_gateway.customer_tenant_config import CONFIG_KEY_API_KEY

        monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")           # production
        monkeypatch.delenv("CREDENTIAL_VAULT_KEY", raising=False)      # no usable vault
        monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_ENV_KEY)          # present but ignored
        monkeypatch.setenv("CUSTOMER_TENANT_ENDPOINT", "https://tenant.example/openai")
        monkeypatch.setenv("CUSTOMER_TENANT_DEPLOYMENT", "gpt-x")

        # Any network attempt is a test failure — the credential short-circuit
        # must happen first. Telemetry is best-effort; stub it so a missing test
        # DB never masks the assertion or adds noise.
        def _no_network(*a, **k):
            raise AssertionError(
                "customer-tenant provider made a network call in production "
                "without a vault credential — the env fallback leaked back in"
            )

        monkeypatch.setattr(prov_mod.urllib.request, "urlopen", _no_network)
        monkeypatch.setattr(prov_mod, "_record_customer_tenant_telemetry", lambda *a, **k: None)

        provider = prov_mod.CustomerTenantModelProvider()
        gen = provider.generate(GenerationRequest(prompt="hi", max_tokens=8, timeout_ms=2000))
        assert gen.ok is False
        assert gen.provider == provider.name
        assert provider.embed(["a", "b"]) == []

    def test_startup_warning_fires_in_prod_when_env_set(self, monkeypatch, caplog):
        """The paired startup-visibility check warns (naming the var, never its
        value) when the stale env var is present under production."""
        import logging

        from app.model_gateway.customer_tenant_config import CONFIG_KEY_API_KEY
        from app.model_gateway.customer_tenant_vault import (
            validate_no_production_env_fallback,
        )

        monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
        monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_ENV_KEY)
        with caplog.at_level(logging.WARNING):
            validate_no_production_env_fallback()  # must not raise
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any(CONFIG_KEY_API_KEY in m for m in msgs), msgs
        assert not any(_FAKE_ENV_KEY in m for m in msgs), "the value must never be logged"

    def test_startup_warning_silent_in_dev(self, monkeypatch, caplog):
        """Dev/standalone: the env var being set produces no production warning."""
        import logging

        from app.model_gateway.customer_tenant_config import CONFIG_KEY_API_KEY
        from app.model_gateway.customer_tenant_vault import (
            validate_no_production_env_fallback,
        )

        monkeypatch.delenv("REQUIRE_CONNECTOR_SECRETS", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv(CONFIG_KEY_API_KEY, _FAKE_ENV_KEY)
        with caplog.at_level(logging.WARNING):
            validate_no_production_env_fallback()
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not any(CONFIG_KEY_API_KEY in m for m in msgs), msgs
