"""
Contract tests for MSP-B12 T1 — Security Operations Discovery Pack scaffold.

T1 registers the pack using the same configuration-driven structure as the
existing packs (B6's cloud-ops pack applied to a second operational domain) and
adds the pack-registration + boundary tests. This suite verifies:

  * The pack registers and its configuration can be LOADED.
  * Every registered DETECTOR PATH is valid (importable) — vacuously green in the
    scaffold, a real guard once T2 populates the detector list.
  * The pack VERSION is present and resolves.
  * SecOps TERMINOLOGY is externalized and covers every required term.
  * Every FINDING satisfies the inherited four-part contract, and the SecOps
    AGGREGATION FLOOR (no individual employee / host / host x vulnerability pair)
    is enforced at the pack boundary — with source records traced through valid
    evidence pointers.
  * A future detector or scoring change REQUIRES an intentional pack-version
    update (a pinned scoring-surface fingerprint fails the build otherwise).

Covers the T1-owned slices of AC1 (four-part contract, enforced by the inherited
pack-boundary test), AC7 (no detector output names an individual — pack-wide
sweep, inherited), and the finding-layer slice of AC2 (aggregation floor).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os

import pytest


def _pack_config():
    try:
        import backend.discovery.packs.pack_config as m
    except ModuleNotFoundError:
        import discovery.packs.pack_config as m
    return m


def _secops_config():
    try:
        import backend.discovery.packs.security_ops_config as m
    except ModuleNotFoundError:
        import discovery.packs.security_ops_config as m
    return m


def _secops_finding():
    try:
        import backend.discovery.packs.security_ops_finding as m
    except ModuleNotFoundError:
        import discovery.packs.security_ops_finding as m
    return m


PACK_ID = "security_ops"


# ── A valid four-part finding, used across the contract tests ────────────────────

def _valid_pointer():
    return {
        "source_system": "servicenow",
        "source_artifact": "SIR0012345",
        "source_timestamp": "2026-07-01T00:00:00+00:00",
        "origin": "observed",
    }


def _valid_finding_contract(f):
    return f.build_finding_contract(
        evidence={"cycles_observed": 6, "median_time_in_state_hours": 42.0},
        confidence=f.build_confidence(
            "MEDIUM", capped=True, eligible_for_high=False,
            cap_reason="single-source",
        ),
        corroboration=f.build_corroboration(
            f.STATUS_SINGLE_SOURCE, sources=["servicenow"], label=f.SINGLE_SOURCE_LABEL,
        ),
        source_trace=f.build_source_trace(
            systems=["servicenow"],
            artifacts=[{"type": "vulnerability_class", "id": "openssl",
                        "evidence_pointer": _valid_pointer()}],
        ),
    )


# ── Pack registration + version (AC1 scaffold) ───────────────────────────────────

class TestPackRegistration:

    def test_pack_id_in_list_packs(self):
        assert PACK_ID in _pack_config().list_packs()

    def test_pack_id_in_registry(self):
        assert PACK_ID in _pack_config().PACK_REGISTRY

    def test_get_pack_returns_correct_pack_id(self):
        assert _pack_config().get_pack(PACK_ID)["packId"] == PACK_ID

    def test_pack_name(self):
        assert _pack_config().get_pack(PACK_ID)["packName"] == "Security Operations"

    def test_domain_and_pack_domain(self):
        pack = _pack_config().get_pack(PACK_ID)
        assert pack["domain"] == "security_ops"
        assert pack["pack_domain"] == "security_ops"

    def test_pack_declares_a_version(self):
        assert _pack_config().get_pack(PACK_ID).get("packVersion")

    def test_get_pack_version_resolves(self):
        m = _pack_config()
        assert m.get_pack_version(PACK_ID) == m.PACK_REGISTRY[PACK_ID]["packVersion"]

    def test_is_security_ops_pack_true(self):
        assert _pack_config().is_security_ops_pack(PACK_ID) is True

    @pytest.mark.parametrize("other", ["service_cloud", "ncino", "cloud_ops", None, "nope"])
    def test_is_security_ops_pack_false_for_others(self, other):
        assert _pack_config().is_security_ops_pack(other) is False

    def test_required_registry_keys_present(self):
        pack = _pack_config().get_pack(PACK_ID)
        for key in ("packId", "packName", "domain", "pack_domain", "detectors",
                    "ui_labels_path", "config_path", "llm_context"):
            assert key in pack, f"registry entry missing {key!r}"

    def test_model_context_speaks_secops_and_forbids_automation(self):
        ctx = _pack_config().get_llm_context(PACK_ID).lower()
        assert ctx
        for term in ("remediation", "scan cycle", "deferral", "sla", "triage",
                     "severity band", "security queue", "ci class"):
            assert term in ctx, f"llm_context missing SecOps term {term!r}"
        assert "no automated" in ctx or "not automate" in ctx

    def test_existing_packs_undisturbed(self):
        m = _pack_config()
        for pid in ("service_cloud", "ncino", "strs_benefits", "sqlserver_opsignal",
                    "github_engineering", "cloud_ops"):
            assert pid in m.PACK_REGISTRY
        assert m.DEFAULT_PACK == "service_cloud"

    def test_pack_selectable_without_runner_conditional(self):
        """The pack resolves through the registry alone — no special runner logic.

        get_pack / get_detector_modules must serve it directly, exactly like every
        other registered pack, so selection needs no discovery-runner branch (T1).
        """
        m = _pack_config()
        assert m.get_pack(PACK_ID)["packId"] == PACK_ID
        assert isinstance(m.get_detector_modules(PACK_ID), list)


# ── Configuration can be loaded (AC5 scaffold / config-driven) ───────────────────

class TestConfigLoads:

    def test_config_path_registered_and_exists(self):
        m = _pack_config()
        path = m.get_pack_config_path(PACK_ID)
        assert path is not None
        assert path.endswith("security_ops_pack_config.json")
        assert os.path.isfile(path)

    def test_config_loads(self):
        c = _secops_config()
        cfg = c.load_security_ops_config()
        assert cfg.pack_version
        assert cfg.thresholds
        assert cfg.terminology.glossary

    def test_thresholds_load_from_config(self):
        c = _secops_config()
        thresholds = c.get_thresholds()
        assert "shared_infrastructure_concentration" in thresholds
        assert thresholds["shared_infrastructure_concentration"]["max_hops"] == 2

    def test_calibration_loads_from_config(self):
        c = _secops_config()
        calib = c.get_calibration()
        assert calib.impact_weights, "impact weights must load from config"
        assert calib.confidence.get("single_source_cap") == "MEDIUM"

    def test_severity_band_weighting_is_config_not_code(self):
        """AC6 posture at scaffold time: the severity-band weighting is config.

        Critical-band weight must exceed informational-band weight, and it must come
        from the file — the T6 scorer reads these to rank critical toil above
        informational toil at equal effort, alterable with no code deploy.
        """
        c = _secops_config()
        band = c.get_calibration().severity_band
        assert band["critical"] > band["informational"]

    def test_config_change_alters_behaviour_without_code_change(self, tmp_path):
        c = _secops_config()
        base = c.load_security_ops_config()

        cfg_file = tmp_path / "security_ops_pack_config.json"
        raw = {
            "packVersion": "9.9.9",
            "terminology": {"glossary": {t: f"def-{t}" for t in c.REQUIRED_SECOPS_TERMS}},
            "thresholds": {"shared_infrastructure_concentration": {"max_hops": 5}},
            "calibration": {
                "impact_weights": {"breadth": 1.0},
                "severity_band": {"critical": 2.0, "informational": 0.05},
                "confidence": {"single_source_cap": "LOW"},
            },
        }
        cfg_file.write_text(json.dumps(raw), encoding="utf-8")

        loaded = c.load_security_ops_config(str(cfg_file))
        assert loaded.thresholds["shared_infrastructure_concentration"]["max_hops"] == 5
        assert loaded.calibration.confidence["single_source_cap"] == "LOW"
        # The real config is unaffected — a different value proves no code constant.
        assert base.thresholds["shared_infrastructure_concentration"]["max_hops"] == 2

        # Edit in place -> new mtime -> cache invalidated, new value seen.
        raw["thresholds"]["shared_infrastructure_concentration"]["max_hops"] = 7
        cfg_file.write_text(json.dumps(raw), encoding="utf-8")
        os.utime(cfg_file, (os.path.getmtime(cfg_file) + 10, os.path.getmtime(cfg_file) + 10))
        reloaded = c.load_security_ops_config(str(cfg_file))
        assert reloaded.thresholds["shared_infrastructure_concentration"]["max_hops"] == 7

    def test_missing_file_is_rejected(self):
        c = _secops_config()
        with pytest.raises(c.SecurityOpsConfigError):
            c.load_security_ops_config("/no/such/security_ops_config.json")


# ── Terminology externalized + covers every required SecOps term ─────────────────

class TestTerminology:

    _REQUIRED = ("remediation", "scan_cycle", "deferral", "sla", "triage",
                 "severity_band", "security_queue", "ci_class")

    def test_required_terms_constant(self):
        c = _secops_config()
        assert set(c.REQUIRED_SECOPS_TERMS) == set(self._REQUIRED)

    def test_glossary_covers_all_terms(self):
        c = _secops_config()
        glossary = c.get_terminology().glossary
        for term in self._REQUIRED:
            assert term in glossary, f"terminology glossary missing {term!r}"
            assert glossary[term].strip(), f"terminology for {term!r} is empty"

    def test_get_secops_term_is_case_insensitive(self):
        c = _secops_config()
        assert c.get_secops_term("SLA") == c.get_secops_term("sla") != ""

    def test_language_map_present(self):
        c = _secops_config()
        lang = c.get_terminology().language_map
        assert isinstance(lang, dict) and lang

    def test_missing_term_is_rejected(self, tmp_path):
        """A config dropping a required SecOps term must fail loudly, not silently."""
        c = _secops_config()
        cfg_file = tmp_path / "bad.json"
        cfg_file.write_text(
            json.dumps({"terminology": {"glossary": {"remediation": "x", "sla": "y"}}}),
            encoding="utf-8",
        )
        with pytest.raises(c.SecurityOpsConfigError):
            c.load_security_ops_config(str(cfg_file))


# ── Every detector path is valid ─────────────────────────────────────────────────

class TestDetectorPathsValid:

    def test_detectors_is_a_list(self):
        assert isinstance(_pack_config().get_detector_modules(PACK_ID), list)

    def test_every_registered_detector_path_is_importable(self):
        """Every registered detector module path must import. Vacuously true for the
        scaffold's empty list; a real guard once T2 (Section 1) populates it."""
        m = _pack_config()
        for path in m.get_detector_modules(PACK_ID):
            assert isinstance(path, str) and path
            importlib.import_module(path)  # raises if the path is invalid


# ── Inherited four-part finding contract (AC1) ───────────────────────────────────

class TestFourPartContract:

    def test_four_part_fields_inherited_from_operational_scaffold(self):
        f = _secops_finding()
        assert f.FOUR_PART_CONTRACT_FIELDS == (
            "evidence", "confidence", "corroboration", "source_trace"
        )

    def test_valid_finding_carries_all_four_parts(self):
        f = _secops_finding()
        contract = _valid_finding_contract(f)
        assert f.is_contract_complete(contract)
        assert f.missing_contract_parts(contract) == []

    def test_enforcement_passes_a_valid_finding(self):
        f = _secops_finding()
        contract = _valid_finding_contract(f)
        f.enforce_finding_contract(contract, detector_id="remediation_recurrence")

    def test_enforcement_fails_when_a_part_is_missing(self):
        f = _secops_finding()
        contract = _valid_finding_contract(f)
        del contract["corroboration"]
        with pytest.raises(f.SecurityOpsContractViolation):
            f.enforce_finding_contract(contract, detector_id="d")

    def test_enforcement_fails_on_missing_contract(self):
        f = _secops_finding()
        with pytest.raises(f.SecurityOpsContractViolation):
            f.enforce_finding_contract(None, detector_id="d")

    def test_evidence_must_carry_a_number(self):
        f = _secops_finding()
        with pytest.raises(ValueError):
            f.build_finding_contract(
                evidence={"note": "no numbers here"},
                confidence=f.build_confidence("LOW", capped=True, eligible_for_high=False),
                corroboration=f.build_corroboration(
                    f.STATUS_SINGLE_SOURCE, sources=["servicenow"], label="x"),
                source_trace=f.build_source_trace(
                    systems=["servicenow"],
                    artifacts=[{"type": "t", "evidence_pointer": _valid_pointer()}]),
            )

    def test_source_trace_requires_valid_evidence_pointer(self):
        """Trace-back must go through VALID evidence pointers (MSP-B12 four-part)."""
        f = _secops_finding()
        with pytest.raises(ValueError):
            f.build_source_trace(
                systems=["servicenow"],
                artifacts=[{"type": "vulnerability_class", "id": "openssl"}],  # no pointer
            )

    def test_source_trace_rejects_incomplete_pointer(self):
        f = _secops_finding()
        broken = dict(_valid_pointer())
        del broken["source_timestamp"]  # violates the mandatory spine
        with pytest.raises(ValueError):
            f.build_source_trace(
                systems=["servicenow"],
                artifacts=[{"type": "t", "evidence_pointer": broken}],
            )

    def test_enforce_pack_findings_counts_valid_findings(self):
        f = _secops_finding()
        contract = _valid_finding_contract(f)
        results = [{"detector_id": "d1", "raw_evidence": {"finding_contract": contract}},
                   {"detector_id": "d2", "raw_evidence": {"finding_contract": contract}}]
        assert f.enforce_pack_findings(results) == 2

    def test_enforce_pack_findings_fails_the_run_on_a_bad_finding(self):
        f = _secops_finding()
        good = _valid_finding_contract(f)
        results = [
            {"detector_id": "ok", "raw_evidence": {"finding_contract": good}},
            {"detector_id": "bad", "raw_evidence": {}},  # no contract
        ]
        with pytest.raises(f.SecurityOpsContractViolation):
            f.enforce_pack_findings(results)


# ── Aggregation floor / no-individual sweep (AC7 + AC2 finding-layer) ────────────

class TestAggregationFloor:

    def test_clean_finding_has_no_violations(self):
        f = _secops_finding()
        assert f.find_aggregation_floor_violations(_valid_finding_contract(f)) == []

    def test_individual_employee_reference_is_flagged(self):
        f = _secops_finding()
        assert f.find_aggregation_floor_violations({"assignee": "someone"})
        assert f.find_aggregation_floor_violations({"contact": "a@b.com"})

    def test_individual_host_reference_is_flagged(self):
        f = _secops_finding()
        assert f.find_host_or_asset_references({"hostname": "web-01.corp"})
        assert f.find_host_or_asset_references({"ip_address": "10.0.0.4"})
        assert f.find_host_or_asset_references({"asset_id": "A-1029"})

    def test_vulnerability_instance_reference_is_flagged(self):
        f = _secops_finding()
        assert f.find_vulnerability_instance_references({"qid": "150004"})
        assert f.find_vulnerability_instance_references({"vulnerability_id": "V-88"})

    def test_host_vulnerability_pair_is_flagged(self):
        f = _secops_finding()
        pair = {"host": "db-07", "cve": "CVE-2021-44228", "plugin_id": "12345"}
        hits = f.find_aggregation_floor_violations(pair)
        assert any("host" in h for h in hits)
        assert any("plugin_id" in h for h in hits)

    def test_vulnerability_class_by_cve_is_allowed(self):
        """A CVE names a vulnerability CLASS — allowed. Only host/instance pairing is not."""
        f = _secops_finding()
        assert f.find_aggregation_floor_violations(
            {"vulnerability_class": "Log4Shell", "cve": "CVE-2021-44228",
             "service": "payments", "ci_class": "app-server"}
        ) == []

    def test_building_a_finding_with_a_host_is_rejected(self):
        f = _secops_finding()
        with pytest.raises(ValueError):
            f.build_finding_contract(
                evidence={"count": 3, "hostname": "web-01"},
                confidence=f.build_confidence("LOW", capped=True, eligible_for_high=False),
                corroboration=f.build_corroboration(
                    f.STATUS_SINGLE_SOURCE, sources=["servicenow"], label="x"),
                source_trace=f.build_source_trace(
                    systems=["servicenow"],
                    artifacts=[{"type": "t", "evidence_pointer": _valid_pointer()}]),
            )

    def test_enforcement_rejects_a_finding_that_names_a_host(self):
        f = _secops_finding()
        contract = _valid_finding_contract(f)
        contract["evidence"]["hostname"] = "web-01"
        with pytest.raises(f.SecurityOpsContractViolation):
            f.enforce_finding_contract(contract, detector_id="d")


# ── Version-bump guard: detector/scoring change requires an intentional bump ─────

class TestVersionBumpGuard:

    # Pinned to security_ops packVersion 1.0.0. When the detector list, thresholds,
    # or calibration change, this fingerprint changes and the test fails — forcing
    # the change author to (1) bump packVersion in BOTH pack_config.py and
    # security_ops_pack_config.json and (2) update these pins. That is the
    # "intentional pack-version update" required by MSP-B12 T1.
    # Bumped 1.0.0 → 1.1.0 by MSP-B12 T2 (the five Section-1 detectors + the
    # min_hops calibration change) — the intentional pack-version update this guard
    # exists to force.
    PINNED_VERSION = "1.1.0"
    PINNED_FINGERPRINT = "245e0799e9b111584e9e7550c4298dc8a8a05a8e7a882876579eab5e521feb86"

    @staticmethod
    def _scoring_surface_fingerprint():
        c = _secops_config()
        m = _pack_config()
        cfg = c.load_security_ops_config()
        surface = {
            "detectors": m.get_detector_modules(PACK_ID),
            "thresholds": cfg.thresholds,
            "impact_weights": cfg.calibration.impact_weights,
            "severity_band": cfg.calibration.severity_band,
            "confidence": cfg.calibration.confidence,
        }
        blob = json.dumps(surface, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def test_version_matches_pin(self):
        assert _pack_config().get_pack_version(PACK_ID) == self.PINNED_VERSION

    def test_config_version_matches_registry_version(self):
        """Registry and externalized-config versions must not drift."""
        c = _secops_config()
        m = _pack_config()
        assert c.load_security_ops_config().pack_version == m.get_pack_version(PACK_ID)

    def test_scoring_surface_change_requires_version_bump(self):
        assert self._scoring_surface_fingerprint() == self.PINNED_FINGERPRINT, (
            "The security_ops detector/threshold/calibration surface changed. Bump "
            "packVersion in pack_config.py AND security_ops_pack_config.json, then "
            "update PINNED_VERSION/PINNED_FINGERPRINT in this test — an intentional "
            "pack-version update (MSP-B12 T1)."
        )
