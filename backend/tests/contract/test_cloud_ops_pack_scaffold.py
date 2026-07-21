"""
Contract tests for MSP-B6 T1 (AT-736) — Cloud-Operations Discovery Pack scaffold.

Covers the T1 acceptance criteria:
  AC1 — Pack registers with the framework and stamps a version number on every run.
  AC2 — Calibration values and thresholds load from config; a config change alters
        behaviour with no code deploy.
  AC3 — Terminology set is externalized and covers all listed NOC terms.
  AC4 — No detector or scorer logic exists in this ticket's diff.
"""
from __future__ import annotations

import json
import os

import pytest


def _pack_config():
    try:
        import backend.discovery.packs.pack_config as m
    except ModuleNotFoundError:
        import discovery.packs.pack_config as m
    return m


def _cloud_ops_config():
    try:
        import backend.discovery.packs.cloud_ops_config as m
    except ModuleNotFoundError:
        import discovery.packs.cloud_ops_config as m
    return m


# ── AC1 — pack registered + versioned ───────────────────────────────────────────

class TestAC1PackRegistration:

    def test_pack_id_in_list_packs(self):
        m = _pack_config()
        assert "cloud_ops" in m.list_packs()

    def test_pack_id_in_registry(self):
        m = _pack_config()
        assert "cloud_ops" in m.PACK_REGISTRY

    def test_get_pack_returns_correct_pack_id(self):
        m = _pack_config()
        assert m.get_pack("cloud_ops")["packId"] == "cloud_ops"

    def test_pack_name(self):
        m = _pack_config()
        assert m.get_pack("cloud_ops")["packName"] == "Cloud Operations"

    def test_domain_and_pack_domain(self):
        m = _pack_config()
        pack = m.get_pack("cloud_ops")
        assert pack["domain"] == "cloud_ops"
        assert pack["pack_domain"] == "cloud_ops"

    def test_pack_declares_a_version(self):
        m = _pack_config()
        assert m.get_pack("cloud_ops").get("packVersion")

    def test_get_pack_version_resolves(self):
        """AC1: the framework stamps this pack's declared version, not the fallback."""
        m = _pack_config()
        assert m.get_pack_version("cloud_ops") == m.PACK_REGISTRY["cloud_ops"]["packVersion"]

    def test_is_cloud_ops_pack_true(self):
        m = _pack_config()
        assert m.is_cloud_ops_pack("cloud_ops") is True

    @pytest.mark.parametrize("other", ["service_cloud", "ncino", "enterprise_ops", None, "nope"])
    def test_is_cloud_ops_pack_false_for_others(self, other):
        m = _pack_config()
        assert m.is_cloud_ops_pack(other) is False

    def test_has_llm_context_in_noc_language(self):
        m = _pack_config()
        ctx = m.get_llm_context("cloud_ops").lower()
        assert ctx
        for term in ("alerts", "incidents", "runbooks", "mttr", "toil", "escalation"):
            assert term in ctx, f"llm_context missing NOC term {term!r}"

    def test_llm_context_forbids_automated_remediation(self):
        m = _pack_config()
        ctx = m.get_llm_context("cloud_ops").lower()
        assert "no automated" in ctx or "not automate" in ctx

    def test_existing_packs_undisturbed(self):
        m = _pack_config()
        for pid in ("service_cloud", "ncino", "strs_benefits", "sqlserver_opsignal", "github_engineering"):
            assert pid in m.PACK_REGISTRY
        assert m.DEFAULT_PACK == "service_cloud"


# ── AC4 — scaffold only, no detector/scorer logic ───────────────────────────────

class TestAC4NoDetectorOrScorerLogic:

    def test_detectors_list_is_empty(self):
        """No detector logic in this ticket — detector module paths arrive in T2/T3."""
        m = _pack_config()
        assert m.get_detector_modules("cloud_ops") == []

    def test_no_scorer_module_yet(self):
        """The ops-impact scorer is MSP-B6 T4 — it must not exist in the T1 diff."""
        import importlib
        for mod in (
            "discovery.packs.cloud_ops_scorer",
            "backend.discovery.packs.cloud_ops_scorer",
        ):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(mod)


# ── AC2 — calibration + thresholds load from config ─────────────────────────────

class TestAC2ConfigDriven:

    def test_config_path_registered(self):
        m = _pack_config()
        path = m.get_pack_config_path("cloud_ops")
        assert path is not None
        assert path.endswith("cloud_ops_pack_config.json")
        assert os.path.isfile(path)

    def test_thresholds_load_from_config(self):
        c = _cloud_ops_config()
        thresholds = c.get_thresholds()
        assert "shared_ci_hotspot" in thresholds
        # Section 2: hotspot traversal is depth-bounded, default 2 hops.
        assert thresholds["shared_ci_hotspot"]["max_hops"] == 2

    def test_calibration_loads_from_config(self):
        c = _cloud_ops_config()
        calibration = c.get_calibration()
        assert calibration.impact_weights, "impact weights must load from config"
        # Honest-confidence caps (Section 2 / T6) are config, not code.
        assert calibration.confidence.get("single_source_cap") == "MEDIUM"

    def test_config_change_alters_behaviour_without_code_change(self, tmp_path):
        """AC2: editing the config file changes what the loader returns — no code deploy.

        Writes a modified copy, loads it, and asserts the new value is reflected.
        Also proves the mtime cache picks up an in-place edit.
        """
        c = _cloud_ops_config()
        base = c.load_cloud_ops_config()

        cfg_file = tmp_path / "cloud_ops_pack_config.json"
        raw = {
            "packVersion": "9.9.9",
            "terminology": {"glossary": {t: f"def-{t}" for t in c.REQUIRED_NOC_TERMS}},
            "thresholds": {"shared_ci_hotspot": {"max_hops": 5}},
            "calibration": {"impact_weights": {"breadth": 1.0}, "confidence": {"single_source_cap": "LOW"}},
        }
        cfg_file.write_text(json.dumps(raw), encoding="utf-8")

        loaded = c.load_cloud_ops_config(str(cfg_file))
        assert loaded.thresholds["shared_ci_hotspot"]["max_hops"] == 5
        assert loaded.calibration.confidence["single_source_cap"] == "LOW"
        # The real config is unaffected — different value proves no code constant.
        assert base.thresholds["shared_ci_hotspot"]["max_hops"] == 2

        # Edit the same file in place → new mtime → cache invalidated, new value seen.
        raw["thresholds"]["shared_ci_hotspot"]["max_hops"] = 7
        cfg_file.write_text(json.dumps(raw), encoding="utf-8")
        # Force the mtime forward so the cache-invalidation is deterministic even
        # when the two writes land within one filesystem timestamp tick.
        os.utime(cfg_file, (os.path.getmtime(cfg_file) + 10, os.path.getmtime(cfg_file) + 10))
        reloaded = c.load_cloud_ops_config(str(cfg_file))
        assert reloaded.thresholds["shared_ci_hotspot"]["max_hops"] == 7


# ── AC3 — terminology externalized + covers all NOC terms ───────────────────────

class TestAC3Terminology:

    _REQUIRED = ("alerts", "incidents", "runbooks", "mttr", "toil", "escalation")

    def test_required_noc_terms_constant(self):
        c = _cloud_ops_config()
        assert set(c.REQUIRED_NOC_TERMS) == set(self._REQUIRED)

    def test_glossary_covers_all_noc_terms(self):
        c = _cloud_ops_config()
        glossary = c.get_terminology().glossary
        for term in self._REQUIRED:
            assert term in glossary, f"terminology glossary missing {term!r}"
            assert glossary[term].strip(), f"terminology for {term!r} is empty"

    def test_get_noc_term_is_case_insensitive(self):
        c = _cloud_ops_config()
        assert c.get_noc_term("MTTR") == c.get_noc_term("mttr") != ""

    def test_language_map_present(self):
        c = _cloud_ops_config()
        lang = c.get_terminology().language_map
        assert isinstance(lang, dict) and lang

    def test_missing_noc_term_is_rejected(self, tmp_path):
        """A config that drops a required NOC term must fail loudly, not silently."""
        c = _cloud_ops_config()
        cfg_file = tmp_path / "bad.json"
        cfg_file.write_text(
            json.dumps({"terminology": {"glossary": {"alerts": "x", "incidents": "y"}}}),
            encoding="utf-8",
        )
        with pytest.raises(c.CloudOpsConfigError):
            c.load_cloud_ops_config(str(cfg_file))

    def test_missing_file_is_rejected(self):
        c = _cloud_ops_config()
        with pytest.raises(c.CloudOpsConfigError):
            c.load_cloud_ops_config("/no/such/cloud_ops_config.json")
