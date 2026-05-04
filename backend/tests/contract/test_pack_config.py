"""
ENG-SHARED-1 — Pack Configuration Architecture Tests
Sprint 5 — Wave 3

Tests:
  1. Both packs registered — service_cloud and ncino
  2. get_pack() returns correct config for each
  3. get_pack(None) returns service_cloud (safe default)
  4. get_pack("unknown") returns service_cloud (safe default)
  5. get_pack_domain() returns correct domain string
  6. is_ncino_pack() correct for both packs
  7. get_detector_modules() returns non-empty list for each pack
  8. get_ui_labels() returns None for service_cloud, dict for ncino
  9. get_llm_context() returns non-empty string for each pack
  10. ncino llm_context contains compliance instruction
  11. runner.run() accepts pack parameter
  12. runner.run() logs pack_id in output
  13. ComputeRequest accepts pack field
  14. CPQ pack slot commented but not registered (Sprint 6)

Run:
  pytest tests/contract/test_pack_config.py -v
"""
from __future__ import annotations

import os
import pytest
from unittest.mock import patch

from discovery.packs.pack_config import (
    get_pack,
    get_pack_domain,
    get_detector_modules,
    get_ui_labels,
    get_llm_context,
    is_ncino_pack,
    list_packs,
    PACK_REGISTRY,
    DEFAULT_PACK,
)


# ── Pack registry ─────────────────────────────────────────────────────────────

class TestPackRegistry:

    def test_service_cloud_pack_registered(self):
        assert "service_cloud" in PACK_REGISTRY

    def test_ncino_pack_registered(self):
        assert "ncino" in PACK_REGISTRY

    def test_cpq_pack_not_registered(self):
        """CPQ is deferred to Sprint 6 — must not be in registry yet."""
        assert "ncino_cpq" not in PACK_REGISTRY
        assert "cpq" not in PACK_REGISTRY

    def test_default_pack_is_service_cloud(self):
        assert DEFAULT_PACK == "service_cloud"

    def test_list_packs_returns_both(self):
        packs = list_packs()
        assert "service_cloud" in packs
        assert "ncino" in packs

    def test_each_pack_has_required_keys(self):
        required = ["packId", "packName", "domain", "pack_domain", "detectors", "llm_context"]
        for pack_id, config in PACK_REGISTRY.items():
            for key in required:
                assert key in config, f"Pack '{pack_id}' missing key '{key}'"


# ── get_pack() ────────────────────────────────────────────────────────────────

class TestGetPack:

    def test_get_service_cloud_pack(self):
        pack = get_pack("service_cloud")
        assert pack["packId"] == "service_cloud"
        assert pack["domain"] == "service_cloud"

    def test_get_ncino_pack(self):
        pack = get_pack("ncino")
        assert pack["packId"] == "ncino"
        assert pack["domain"] == "ncino"

    def test_none_returns_default(self):
        pack = get_pack(None)
        assert pack["packId"] == DEFAULT_PACK

    def test_unknown_returns_default(self):
        pack = get_pack("unknown_pack_xyz")
        assert pack["packId"] == DEFAULT_PACK

    def test_empty_string_returns_default(self):
        pack = get_pack("")
        assert pack["packId"] == DEFAULT_PACK


# ── get_pack_domain() ─────────────────────────────────────────────────────────

class TestGetPackDomain:

    def test_service_cloud_domain(self):
        assert get_pack_domain("service_cloud") == "service_cloud"

    def test_ncino_domain(self):
        assert get_pack_domain("ncino") == "ncino"

    def test_none_returns_service_cloud_domain(self):
        assert get_pack_domain(None) == "service_cloud"


# ── is_ncino_pack() ───────────────────────────────────────────────────────────

class TestIsNcinoPack:

    def test_ncino_is_ncino(self):
        assert is_ncino_pack("ncino") is True

    def test_service_cloud_is_not_ncino(self):
        assert is_ncino_pack("service_cloud") is False

    def test_none_is_not_ncino(self):
        assert is_ncino_pack(None) is False

    def test_unknown_is_not_ncino(self):
        assert is_ncino_pack("unknown") is False


# ── get_detector_modules() ────────────────────────────────────────────────────

class TestGetDetectorModules:

    def test_service_cloud_has_detectors(self):
        mods = get_detector_modules("service_cloud")
        assert len(mods) > 0
        assert all(isinstance(m, str) for m in mods)

    def test_ncino_has_detectors(self):
        mods = get_detector_modules("ncino")
        assert len(mods) > 0

    def test_ncino_detectors_contain_lending_patterns(self):
        mods = get_detector_modules("ncino")
        mod_str = " ".join(mods)
        assert "covenant" in mod_str or "checklist" in mod_str or "spreading" in mod_str

    def test_service_cloud_detectors_contain_sc_patterns(self):
        mods = get_detector_modules("service_cloud")
        mod_str = " ".join(mods)
        assert "repetition" in mod_str or "handoff" in mod_str or "approval_delay" in mod_str


# ── get_ui_labels() ───────────────────────────────────────────────────────────

class TestGetUiLabels:

    def test_service_cloud_returns_none(self):
        """Service Cloud has no UI labels file."""
        assert get_ui_labels("service_cloud") is None

    def test_ncino_returns_dict_or_none(self):
        """nCino returns dict if labels file exists, None if not found."""
        result = get_ui_labels("ncino")
        assert result is None or isinstance(result, dict)

    def test_ncino_labels_have_expected_detectors(self):
        result = get_ui_labels("ncino")
        if result is None:
            pytest.skip("ncino_ui_labels.json not found — skipping content test")
        assert "COVENANT_TRACKING_GAP" in result
        assert "CHECKLIST_BOTTLENECK" in result
        assert "SPREADING_BOTTLENECK" in result


# ── get_llm_context() ─────────────────────────────────────────────────────────

class TestGetLlmContext:

    def test_service_cloud_context_non_empty(self):
        ctx = get_llm_context("service_cloud")
        assert isinstance(ctx, str) and len(ctx) > 0

    def test_ncino_context_non_empty(self):
        ctx = get_llm_context("ncino")
        assert isinstance(ctx, str) and len(ctx) > 0

    def test_ncino_context_contains_compliance_instruction(self):
        """Non-negotiable: nCino LLM context must instruct against automated credit decisions."""
        ctx = get_llm_context("ncino")
        assert "credit decision" in ctx.lower() or "automated" in ctx.lower()

    def test_ncino_context_contains_banking_language(self):
        ctx = get_llm_context("ncino")
        assert "lending" in ctx.lower() or "loan" in ctx.lower() or "banking" in ctx.lower()


# ── runner.run() accepts pack ─────────────────────────────────────────────────

class TestRunnerPackIntegration:

    def test_runner_accepts_pack_parameter(self):
        """runner.run() must accept pack kwarg without error."""
        import os
        os.environ["INGEST_MODE"] = "offline"
        try:
            from discovery.runner import run
            import inspect
            sig = inspect.signature(run)
            assert "pack" in sig.parameters, "runner.run() missing pack parameter"
        finally:
            os.environ.pop("INGEST_MODE", None)

    def test_runner_output_contains_pack_id(self):
        """runner.run() output must include packId field."""
        import os
        os.environ["INGEST_MODE"] = "offline"
        try:
            from discovery.runner import run
            result = run(mode="offline", pack="service_cloud")
            assert "packId" in result
            assert result["packId"] == "service_cloud"
        finally:
            os.environ.pop("INGEST_MODE", None)

    def test_runner_ncino_pack_id_in_output(self):
        """runner.run(pack='ncino') must return packId='ncino'."""
        import os
        os.environ["INGEST_MODE"] = "offline"
        try:
            from discovery.runner import run
            result = run(mode="offline", pack="ncino")
            assert result.get("packId") == "ncino"
        finally:
            os.environ.pop("INGEST_MODE", None)

    def test_runner_none_pack_returns_default(self):
        import os
        os.environ["INGEST_MODE"] = "offline"
        try:
            from discovery.runner import run
            result = run(mode="offline", pack=None)
            assert result.get("packId") == DEFAULT_PACK
        finally:
            os.environ.pop("INGEST_MODE", None)


# ── ComputeRequest pack field ─────────────────────────────────────────────────

class TestComputeRequestPackField:

    def test_compute_request_accepts_pack(self):
        from app.routes_sprint4_t1 import ComputeRequest
        req = ComputeRequest(mode="offline", systems=["salesforce"], pack="ncino")
        assert req.pack == "ncino"

    def test_compute_request_pack_defaults_to_none(self):
        from app.routes_sprint4_t1 import ComputeRequest
        req = ComputeRequest(mode="offline", systems=["salesforce"])
        assert req.pack is None

    def test_compute_request_service_cloud_pack(self):
        from app.routes_sprint4_t1 import ComputeRequest
        req = ComputeRequest(mode="offline", pack="service_cloud")
        assert req.pack == "service_cloud"
