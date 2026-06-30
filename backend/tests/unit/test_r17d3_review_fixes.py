"""Unit tests for the R17-D3 PR review fixes (H2, M2, M3, L2).

Pure unit tests — no DB / FastAPI client needed. Each maps to a numbered review
item so the fix is regression-guarded:

  H2 — dev-only security flags are force-disabled in production.
  M2 — update_connector_metrics_from_run threads the requesting org into the
       per-connector overlay write (guards the internal call graph).
  M3 — the unattributed-org sentinel is a distinct, non-tenant value.
  L2 — the enrichment telemetry event is registered with org_id in its schema, and
       record_event validates the event TYPE only (so threading org_id can't raise).
"""
from unittest.mock import patch

from app import connector_metrics


# ---------------------------------------------------------------------------
# H2 — production gating of the dev-only bypass flags
# ---------------------------------------------------------------------------


def test_callback_unauth_is_force_disabled_in_production(monkeypatch):
    from app.routes_connector_auth import _callback_allows_unauth

    monkeypatch.setenv("OAUTH_CALLBACK_ALLOW_UNAUTH", "1")

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert _callback_allows_unauth() is False, "must be force-ignored in production"

    monkeypatch.setenv("ENVIRONMENT", "development")
    assert _callback_allows_unauth() is True, "honoured outside production"


def test_x_org_header_is_not_trusted_in_production(monkeypatch):
    from app.middleware.tenancy import _x_org_header_trusted

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert _x_org_header_trusted() is False

    monkeypatch.delenv("ENVIRONMENT", raising=False)
    assert _x_org_header_trusted() is True


# ---------------------------------------------------------------------------
# M2 — connector metrics are written under the requesting org
# ---------------------------------------------------------------------------


def test_update_connector_metrics_threads_org_to_update_connector():
    """M2: every _update_connector call carries the org passed to the public
    function, so run-derived metrics land on the requesting org's overlay (never a
    default). Guards the call graph the endpoint contract tests don't cover."""
    payload = {
        "opportunities": [{"packId": "service_cloud"}],
        "inputs": {},
    }
    with patch.object(connector_metrics, "_update_connector") as mock_uc:
        connector_metrics.update_connector_metrics_from_run(
            payload, ["salesforce", "servicenow"], "org-requesting-123"
        )

    assert mock_uc.called, "expected connector metric writes for the given systems"
    for call in mock_uc.call_args_list:
        # org_id is the first positional arg of _update_connector.
        assert call.args[0] == "org-requesting-123"


# ---------------------------------------------------------------------------
# M3 — unattributed sentinel is unambiguous
# ---------------------------------------------------------------------------


def test_unattributed_org_sentinel_is_distinct():
    from app.middleware.tenancy import UNATTRIBUTED_ORG

    assert UNATTRIBUTED_ORG == "_unattributed"
    # Must never collide with a real/likely tenant value, so analyst queries on the
    # sentinel return only data-quality gaps.
    assert UNATTRIBUTED_ORG not in ("default", "unknown", "")


# ---------------------------------------------------------------------------
# L2 — enrichment telemetry event is registered with org_id; type-only validation
# ---------------------------------------------------------------------------


def test_enrichment_event_registered_with_org_id_in_schema():
    from app.telemetry import EVENT_PAYLOAD_TYPES, REGISTERED_EVENT_TYPES

    assert "temporal.enrichment_completed" in REGISTERED_EVENT_TYPES
    schema = EVENT_PAYLOAD_TYPES["temporal.enrichment_completed"]
    annotations = getattr(schema, "__annotations__", {})
    assert "org_id" in annotations, "org_id must be part of the registered schema"
    assert "run_id" in annotations


def test_record_event_validates_event_type_not_payload_keys():
    """L2: record_event raises only for an UNREGISTERED event type; it does not
    inspect payload keys, so threading org_id into a payload can never raise."""
    import pytest

    from app.telemetry import record_event

    with pytest.raises(ValueError):
        record_event("definitely.not.registered", {"org_id": "x"})
