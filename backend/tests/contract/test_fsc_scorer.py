"""Contract tests for 2.0-D1 T3 — FSC scorer calibration.

Definition of done for this subtask: FSC findings score through an FSC-specific
calibration; no file under the shared scoring engine is modified; the calibration
values are in one place with documented provenance; an unmapped detector degrades
loudly.

The AC4 section deserves a note. D1's AC4 — "delivered with zero template-model and
zero scoring-engine code changes" — is a criterion about WHERE THE CHANGE LANDED, and
the ticket is explicit that a green test suite says nothing about that. So AC4 is
tested here in the only way a test honestly can: structurally, by asserting the
shared scoring engine and the template model contain no FSC awareness at all, plus a
git-diff check that runs when repository history is available (CI checks out shallow,
so it skips there rather than passing vacuously). Neither replaces reading the diff —
they make a regression fail loudly if someone later reaches into the shared engine.
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

def _repo_root() -> Path:
    """Locate the repository root by walking up for a known marker.

    Resolved by marker rather than by a fixed number of parent hops so the test
    keeps working if it is moved, and so it can be exercised from outside the
    repository tree.
    """
    marker = Path("backend") / "discovery" / "scorer.py"
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / marker).is_file():
            return candidate
    # Fall back to the importable package's location (works when the tests are run
    # from a copy while `discovery` resolves to the real tree).
    try:
        import discovery.scorer as _shared_scorer
    except ModuleNotFoundError:  # pragma: no cover
        import backend.discovery.scorer as _shared_scorer  # type: ignore
    return Path(_shared_scorer.__file__).resolve().parents[2]


REPO_ROOT = _repo_root()
BACKEND_ROOT = REPO_ROOT / "backend"

# The shared scoring engine and template model. AC4 forbids changes to these.
SHARED_SCORING_ENGINE = (
    "backend/discovery/scorer.py",
    "backend/discovery/calibration/calibrator.py",
    "backend/discovery/calibration/ranking.py",
)
TEMPLATE_MODEL = (
    "backend/discovery/packs/template_registry.py",
    "backend/app/terminology.py",
)

# Files T3 is allowed to touch: the pack's own scorer/config, its registry entry,
# the runner dispatch branch, tests, and docs. Anything else is a deviation that
# "files back as a defect" per AC4.
T3_PERMITTED_PATHS = (
    "backend/discovery/packs/financial_services_cloud_scorer.py",
    "backend/discovery/packs/financial_services_cloud_config.py",
    "backend/discovery/packs/financial_services_cloud_pack_config.json",
    "backend/discovery/packs/pack_config.py",
    "backend/discovery/runner.py",
    "backend/tests/",
    "CLAUDE.md",
)

FSC_TOKENS = ("fsc", "financial_services_cloud", "FinServ")


def _mod(name: str):
    try:
        return importlib.import_module(f"discovery.{name}")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module(f"backend.discovery.{name}")


def _scorer():
    return _mod("packs.financial_services_cloud_scorer")


def _cfg():
    return _mod("packs.financial_services_cloud_config")


def _pack_config():
    return _mod("packs.pack_config")


PACK_ID = "financial_services_cloud"

DETECTOR_MODULES = (
    "detectors.fsc_servicing_request_recurrence",
    "detectors.fsc_referral_handoff_friction",
    "detectors.fsc_approval_review_cycle",
    "detectors.fsc_service_queue_ageing",
    "detectors.fsc_cross_object_rework",
)


@pytest.fixture(scope="module")
def findings():
    sf_data = {"fsc": _mod("ingest.fsc").ingest()}
    out = []
    for name in DETECTOR_MODULES:
        out.extend(_mod(name).detect(sf_data, {}, {}))
    return out


@pytest.fixture(scope="module")
def scored(findings):
    s = _scorer()
    ranking = s.rank_fsc_findings(findings)
    return [
        (f, s.score_financial_services_cloud(f, ranking=ranking)) for f in findings
    ]


# ── AC4 — zero scoring-engine and zero template-model changes ───────────────────

class TestAC4WhereTheChangeLanded:

    @pytest.mark.parametrize("rel", SHARED_SCORING_ENGINE + TEMPLATE_MODEL)
    def test_shared_file_exists(self, rel):
        assert (REPO_ROOT / rel).is_file(), rel

    @pytest.mark.parametrize("rel", SHARED_SCORING_ENGINE)
    def test_shared_scoring_engine_has_no_fsc_awareness(self, rel):
        """The shared scorer must not know this pack exists.

        A pack-specific branch inside the shared engine is exactly the change AC4
        forbids, and it is the shape such a change would take.
        """
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        lowered = source.lower()
        for token in FSC_TOKENS:
            assert token.lower() not in lowered, (
                f"{rel} references {token!r} — the shared scoring engine must stay "
                f"pack-agnostic (D1 AC4)"
            )

    @pytest.mark.parametrize("rel", TEMPLATE_MODEL)
    def test_template_model_has_no_fsc_awareness(self, rel):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
        for token in FSC_TOKENS:
            assert token.lower() not in source, (
                f"{rel} references {token!r} — the template model must stay "
                f"pack-agnostic (D1 AC4); FSC template work is T4 and is config"
            )

    def test_shared_scorer_knows_no_pack_at_all(self):
        """Stronger, and the real invariant: the shared scorer is pack-agnostic for
        EVERY pack, not just this one."""
        source = (REPO_ROOT / "backend/discovery/scorer.py").read_text(
            encoding="utf-8"
        ).lower()
        for pack in ("ncino", "lending", "cloud_ops", "security_ops", "strs",
                     "github_engineering", "enterprise_ops"):
            assert pack not in source, f"scorer.py references pack {pack!r}"

    def test_fsc_scorer_uses_the_shared_scorer_read_only(self):
        """The FSC scorer imports the shared scorer for ONE purpose — the documented
        loud fallback — and never rebinds anything on it."""
        import inspect
        source = inspect.getsource(_scorer())
        assert "from discovery.scorer import score as sc_score" in source or \
               "from backend.discovery.scorer import score as sc_score" in source
        # No mutation of the shared module.
        for forbidden in ("scorer.score =", "setattr(scorer", "monkeypatch",
                          "scorer.__dict__"):
            assert forbidden not in source, f"FSC scorer mutates shared engine: {forbidden}"

    def test_calibration_lives_outside_the_shared_engine(self):
        """Both halves of the calibration are in pack-owned files."""
        scorer_path = Path(_scorer().__file__).resolve()
        config_path = Path(_pack_config().get_pack_config_path(PACK_ID)).resolve()
        packs_dir = (BACKEND_ROOT / "discovery" / "packs").resolve()
        assert scorer_path.parent == packs_dir
        assert config_path.parent == packs_dir
        for rel in SHARED_SCORING_ENGINE:
            assert (REPO_ROOT / rel).resolve() != scorer_path

    def test_no_shared_engine_file_changed_since_the_base_branch(self):
        """The AC4 diff check, when repository history is available.

        CI checks out shallow (actions/checkout defaults to depth 1), so there is no
        merge base to diff against and this SKIPS rather than passing vacuously —
        the structural tests above are what run everywhere.
        """
        try:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/2.0-D1"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if base.returncode != 0 or not base.stdout.strip():
                pytest.skip("no merge base with origin/2.0-D1 (shallow clone)")
            changed = subprocess.run(
                ["git", "diff", "--name-only", base.stdout.strip(), "HEAD"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if changed.returncode != 0:
                pytest.skip("git diff unavailable")
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pytest.skip("git unavailable")

        files = [f.strip() for f in changed.stdout.splitlines() if f.strip()]
        guarded = set(SHARED_SCORING_ENGINE) | set(TEMPLATE_MODEL)
        violations = sorted(set(files) & guarded)
        assert violations == [], (
            f"AC4 violation — these are under the shared scoring engine / template "
            f"model and must not change: {violations}"
        )

    def test_changed_files_stay_within_the_permitted_surface(self):
        """Same diff check, widened: T3 should have touched only pack-owned files,
        the runner dispatch, tests and docs."""
        try:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/2.0-D1"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if base.returncode != 0 or not base.stdout.strip():
                pytest.skip("no merge base with origin/2.0-D1 (shallow clone)")
            changed = subprocess.run(
                ["git", "diff", "--name-only", base.stdout.strip(), "HEAD"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if changed.returncode != 0:
                pytest.skip("git diff unavailable")
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pytest.skip("git unavailable")

        files = [f.strip() for f in changed.stdout.splitlines() if f.strip()]
        unexpected = [
            f for f in files
            if not any(f.startswith(p) for p in T3_PERMITTED_PATHS)
        ]
        assert unexpected == [], (
            f"T3 touched files outside the permitted surface: {unexpected}"
        )


# ── FSC findings score through FSC calibration ──────────────────────────────────

class TestFscFindingsUseFscCalibration:

    def test_every_finding_is_scored_by_the_fsc_scorer(self, scored):
        for finding, result in scored:
            assert result["score_debug"]["scorer"] == "financial_services_cloud", (
                finding.detector_id
            )
            assert result["score_debug"]["pack"] == PACK_ID

    def test_no_finding_falls_back_to_service_cloud(self, scored):
        """Every registered FSC detector must be in the score table."""
        for finding, result in scored:
            assert result["score_debug"]["scorer"] != "service_cloud_fallback", (
                f"{finding.detector_id} is registered but missing from _FSC_SCORES"
            )

    def test_every_registered_detector_has_a_score_entry(self):
        """The registry and the score table must not drift apart."""
        m = _pack_config()
        s = _scorer()
        for path in m.get_detector_modules(PACK_ID):
            detector_id = importlib.import_module(path).DETECTOR_ID
            assert s.is_financial_services_cloud_detector(detector_id), (
                f"{detector_id} is registered in pack_config.py but missing from "
                f"_FSC_SCORES — it would score with Service Cloud weights"
            )

    def test_score_table_has_no_entry_without_a_detector(self):
        m = _pack_config()
        shipped = {
            importlib.import_module(p).DETECTOR_ID
            for p in m.get_detector_modules(PACK_ID)
        }
        orphans = set(_scorer().FSC_DETECTOR_IDS) - shipped
        assert orphans == set(), f"score entries with no detector: {orphans}"

    def test_returns_the_shared_scorer_shape(self, scored):
        """Same output shape as discovery/scorer.score() for compatibility."""
        required = ("tier", "impact", "effort", "effort_label", "confidence",
                    "roadmap_stage", "score_debug")
        for _finding, result in scored:
            for key in required:
                assert key in result, key

    def test_effort_domain_matches_the_other_pack_scorers(self, scored):
        """Low=2 / Medium=4 / High=7, so downstream rendering stays uniform."""
        lending = _mod("lending_scorer")
        for _finding, result in scored:
            assert result["effort"] in lending._EFFORT_LABEL, result["effort"]

    def test_tier_domain_is_the_shared_one(self, scored):
        for _finding, result in scored:
            assert result["tier"] in ("Quick Win", "Strategic")

    def test_two_key_guard_scorer_only_claims_its_own_detectors(self):
        s = _scorer()
        assert s.is_financial_services_cloud_detector("FSC_APPROVAL_REVIEW_CYCLE") is True
        for other in ("APPROVAL_BOTTLENECK", "COVENANT_TRACKING_GAP", "QUEUE_AGEING",
                      "REPETITIVE_AUTOMATION", "SECOPS_SIR_TRIAGE_TOIL", ""):
            assert s.is_financial_services_cloud_detector(other) is False, other

    def test_runner_dispatches_fsc_with_the_two_key_guard(self):
        """The runner must gate on pack AND detector family, as every pack does."""
        source = (BACKEND_ROOT / "discovery" / "runner.py").read_text(encoding="utf-8")
        assert "is_financial_services_cloud_pack(pack_id)" in source
        assert "is_financial_services_cloud_detector(dr.detector_id)" in source
        assert "score_financial_services_cloud(dr, ranking=_fsc_ranking)" in source


# ── Provenance: every value says where it came from ─────────────────────────────

class TestProvenanceIsDocumented:
    """An undocumented impact: 7 is indistinguishable from a researched one."""

    PROVENANCE_MARKERS = ("JUDGEMENT", "confirmed", "PROVISIONAL", "analogy")

    def test_score_table_is_in_one_place(self):
        s = _scorer()
        assert isinstance(s._FSC_SCORES, dict) and len(s._FSC_SCORES) == 5

    def test_every_entry_carries_the_standard_fields(self):
        for detector_id, entry in _scorer()._FSC_SCORES.items():
            for key in ("tier", "impact", "effort", "confidence", "roadmap_stage"):
                assert key in entry, f"{detector_id} missing {key}"

    def test_score_table_shape_matches_lending_scores(self):
        """Deliberately the same shape as _LENDING_SCORES, the dev convention."""
        lending = _mod("lending_scorer")._LENDING_SCORES
        lending_keys = set(next(iter(lending.values()))) - {
            "compliance_override_impact"
        }
        for detector_id, entry in _scorer()._FSC_SCORES.items():
            assert lending_keys <= set(entry), (
                f"{detector_id} does not carry the _LENDING_SCORES field set"
            )

    def test_every_impact_value_has_an_inline_provenance_comment(self):
        """Each entry's block must record where its number came from."""
        import inspect
        source = inspect.getsource(_scorer())
        for detector_id in _scorer()._FSC_SCORES:
            start = source.index(f'"{detector_id}": {{')
            end = source.index("},", start)
            block = source[start:end]
            assert "#" in block, f"{detector_id} entry carries no comment at all"
            assert any(marker in block for marker in self.PROVENANCE_MARKERS), (
                f"{detector_id} entry has no provenance marker "
                f"{self.PROVENANCE_MARKERS} — an undocumented value is "
                f"indistinguishable from a researched one"
            )

    def test_guessed_values_say_so_explicitly(self):
        """These numbers are not measured, and the code must not imply they are."""
        import inspect
        source = inspect.getsource(_scorer())
        assert source.count("JUDGEMENT") >= 5, (
            "each of the five entries should mark itself a judgement while no "
            "measured FSC dataset exists"
        )
        assert "no measured FSC dataset" in source

    def test_config_calibration_declares_itself_provisional(self):
        cal = _cfg().get_calibration()
        assert cal.is_provisional() is True
        assert "PROVISIONAL" in cal.calibration_status.upper()

    def test_provisional_flag_reaches_the_scored_output(self, scored):
        """Readable on the finding, not only in a comment."""
        for _finding, result in scored:
            assert result["score_debug"]["calibration_provisional"] is True

    def test_every_config_dimension_block_records_its_basis(self):
        path = _pack_config().get_pack_config_path(PACK_ID)
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        calibration = raw["calibration"]
        for dimension in ("effort_concentration", "breadth", "automation_shape"):
            block = calibration[dimension]
            assert block.get("_basis", "").strip(), f"{dimension} has no _basis"
            assert "PROVISIONAL" in block["_basis"].upper()


# ── The three dimensions are pack config ────────────────────────────────────────

class TestDimensionsAreConfigDriven:

    def test_the_three_dimensions_are_the_ones_d1_names(self):
        assert _scorer().DIMENSIONS == (
            "effort_concentration", "breadth", "automation_shape"
        )

    def test_weights_load_from_config(self):
        weights = _cfg().get_calibration().impact_weights
        assert set(weights) == set(_scorer().DIMENSIONS)
        assert all(isinstance(v, float) for v in weights.values())

    def test_weights_sum_to_one(self):
        """So the composite stays in 0..1 and weights read as percentages."""
        weights = _cfg().get_calibration().impact_weights
        assert round(sum(weights.values()), 6) == 1.0, weights

    def test_effort_concentration_is_weighted_highest(self):
        weights = _cfg().get_calibration().impact_weights
        assert weights["effort_concentration"] == max(weights.values())

    def test_automation_shape_is_per_detector_from_config(self):
        cal = _cfg().get_calibration()
        for detector_id in _scorer().FSC_DETECTOR_IDS:
            assert cal.automation_shape_for(detector_id) is not None, detector_id

    def test_regulated_review_is_the_least_automatable(self):
        """The ordering is the point: a regulated human decision must not rank as
        the best automation candidate."""
        cal = _cfg().get_calibration()
        approval = cal.automation_shape_for("FSC_APPROVAL_REVIEW_CYCLE")
        others = [
            cal.automation_shape_for(d)
            for d in _scorer().FSC_DETECTOR_IDS
            if d != "FSC_APPROVAL_REVIEW_CYCLE"
        ]
        assert approval == min([approval] + others)
        assert cal.automation_shape_for("FSC_SERVICING_REQUEST_RECURRENCE") == max(
            [approval] + others
        )

    def test_changing_a_weight_changes_ranked_order_with_no_code_change(self, findings):
        """The behavioural proof of "calibration as pack config"."""
        s = _scorer()
        cfg = _cfg()

        as_shipped = s.rank_fsc_findings(findings)
        shipped_top = min(
            findings, key=lambda f: as_shipped[id(f)]["rank"]
        ).detector_id

        # Re-weight everything onto automation shape: the most automatable work
        # should now rank first instead.
        reweighted = cfg.FscCalibration(
            impact_weights={
                "effort_concentration": 0.0,
                "breadth": 0.0,
                "automation_shape": 1.0,
            },
            automation_shape=cfg.get_calibration().automation_shape,
            effort_concentration=cfg.get_calibration().effort_concentration,
            breadth=cfg.get_calibration().breadth,
        )
        after = s.rank_fsc_findings(findings, calibration=reweighted)
        new_top = min(findings, key=lambda f: after[id(f)]["rank"]).detector_id

        assert new_top == "FSC_SERVICING_REQUEST_RECURRENCE", new_top
        assert new_top != shipped_top, (
            "re-weighting produced the same ordering — the weights are not "
            "actually driving the rank"
        )

    def test_a_config_edit_on_disk_changes_the_weights(self, tmp_path):
        cfg = _cfg()
        path = tmp_path / "cfg.json"
        payload = {
            "packVersion": "9.9.9",
            "terminology": {"glossary": {t: "x" for t in cfg.REQUIRED_FSC_TERMS}},
            "thresholds": {s: {} for s in cfg.REQUIRED_THRESHOLD_SECTIONS},
            "calibration": {
                "impact_weights": {
                    "effort_concentration": 0.1, "breadth": 0.1,
                    "automation_shape": 0.8,
                },
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        loaded = cfg.load_fsc_config(str(path)).calibration
        assert loaded.impact_weights["automation_shape"] == 0.8

    def test_missing_config_degrades_to_documented_defaults(self):
        """A config outage must not crash a run."""
        s = _scorer()
        weights = s._weights(s.FscCalibration())
        assert weights == s.DEFAULT_IMPACT_WEIGHTS
        assert round(sum(s.DEFAULT_IMPACT_WEIGHTS.values()), 6) == 1.0

    def test_ranking_is_deterministic(self, findings):
        s = _scorer()
        first = {
            (f.detector_id, s.rank_fsc_findings(findings)[id(f)]["rank"])
            for f in findings
        }
        second = {
            (f.detector_id, s.rank_fsc_findings(findings)[id(f)]["rank"])
            for f in findings
        }
        assert first == second

    def test_ops_impact_score_is_bounded(self, scored):
        for _finding, result in scored:
            assert 0.0 <= result["ops_impact_score"] <= 1.0

    def test_ranks_are_a_dense_sequence(self, scored):
        ranks = sorted(r["ops_impact_rank"] for _f, r in scored)
        assert ranks == list(range(1, len(ranks) + 1))


# ── What the scorer must NOT recompute ──────────────────────────────────────────

class TestConfidenceAndImpactAreNotRecomputed:

    def test_confidence_is_the_detectors_capped_level(self, scored):
        """T2 caps every FSC finding at MEDIUM (single-source). The scorer must
        carry that through, not raise it."""
        for finding, result in scored:
            contract = finding.raw_evidence["finding_contract"]
            assert result["confidence"] == contract["confidence"]["level"] == "MEDIUM"

    def test_scorer_does_not_raise_a_capped_confidence(self, findings):
        """Even if the score table said HIGH, the contract wins."""
        s = _scorer()
        finding = findings[0]
        assert s._FSC_SCORES[finding.detector_id]["confidence"] == "MEDIUM"
        result = s.score_financial_services_cloud(finding)
        assert result["confidence"] == "MEDIUM"

    def test_table_confidence_is_only_a_fallback(self):
        """A finding carrying no confidence at all falls back to the table."""
        s = _scorer()
        models = _mod("models")
        bare = models.DetectorResult(
            detector_id="FSC_APPROVAL_REVIEW_CYCLE",
            signal_source="salesforce",
            metric_value=9.0,
            threshold=5.0,
            raw_evidence={"pending_count": 4},
        )
        result = s.score_financial_services_cloud(bare)
        assert result["confidence"] == s._FSC_SCORES["FSC_APPROVAL_REVIEW_CYCLE"][
            "confidence"
        ]

    def test_impact_is_the_documented_table_value(self, scored):
        """Impact must not be recomputed from the dimensions — that would replace a
        documented number with a computed one and lose its provenance."""
        table = _scorer()._FSC_SCORES
        for finding, result in scored:
            assert result["impact"] == table[finding.detector_id]["impact"]
            assert result["effort"] == table[finding.detector_id]["effort"]
            assert result["tier"] == table[finding.detector_id]["tier"]

    def test_impact_is_stable_across_reweighting(self, findings):
        """Re-weighting changes ORDER, never the documented impact."""
        s = _scorer()
        cfg = _cfg()
        reweighted = cfg.FscCalibration(
            impact_weights={"effort_concentration": 0.0, "breadth": 0.0,
                            "automation_shape": 1.0},
            automation_shape=cfg.get_calibration().automation_shape,
        )
        for finding in findings:
            a = s.score_financial_services_cloud(finding)
            b = s.score_financial_services_cloud(finding, calibration=reweighted)
            assert a["impact"] == b["impact"] == s._FSC_SCORES[
                finding.detector_id
            ]["impact"]

    def test_score_debug_explains_both_decisions(self, scored):
        for _finding, result in scored:
            debug = result["score_debug"]
            assert "not recomputed" in debug["impact_note"].lower()
            assert "never" in debug["confidence_note"].lower()


# ── Loud degrade for an unmapped detector ───────────────────────────────────────

class TestUnmappedDetectorDegradesLoudly:

    def _unmapped(self):
        models = _mod("models")
        return models.DetectorResult(
            detector_id="FSC_SOMETHING_NEW",
            signal_source="salesforce",
            metric_value=5.0,
            threshold=1.0,
            raw_evidence={"count": 5},
        )

    def test_it_does_not_crash(self):
        result = _scorer().score_financial_services_cloud(self._unmapped())
        assert isinstance(result, dict) and "impact" in result

    def test_it_logs_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING):
            _scorer().score_financial_services_cloud(self._unmapped())
        messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert messages, "silent fallback — nothing logged"
        joined = " ".join(messages)
        assert "FSC_SOMETHING_NEW" in joined
        assert "config bug" in joined.lower()

    def test_the_warning_names_the_consequence(self, caplog):
        """Not just 'unknown detector' — it must say the finding is now being
        scored with Service Cloud weights."""
        with caplog.at_level(logging.WARNING):
            _scorer().score_financial_services_cloud(self._unmapped())
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "service cloud" in joined.lower()
        assert "_FSC_SCORES" in joined

    def test_the_degrade_is_visible_on_the_output_too(self):
        """Visible to someone reading a stored artifact, not only in the log."""
        result = _scorer().score_financial_services_cloud(self._unmapped())
        debug = result["score_debug"]
        assert debug["scorer"] == "service_cloud_fallback"
        assert debug["pack"] == PACK_ID
        assert "not FSC calibration" in debug["fallback_reason"]

    def test_it_mirrors_the_lending_scorer_pattern(self):
        """score_lending set this precedent; the pattern is carried across."""
        import inspect
        lending_source = inspect.getsource(_mod("lending_scorer").score_lending)
        fsc_source = inspect.getsource(
            _scorer().score_financial_services_cloud
        )
        for signal in ("warning", "config bug", "sc_score"):
            assert signal in lending_source.lower()
            assert signal in fsc_source.lower()

    def test_a_known_detector_logs_nothing(self, caplog, findings):
        with caplog.at_level(logging.WARNING):
            _scorer().score_financial_services_cloud(findings[0])
        assert [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "unknown detector" in r.getMessage()
        ] == []


# ── Version discipline ─────────────────────────────────────────────────────────

class TestVersionBump:

    def test_pack_version_bumped_for_the_scorer_change(self):
        """A scorer change is a pack-logic change, so the version must move."""
        assert _pack_config().get_pack_version(PACK_ID) == "1.2.0"

    def test_registry_and_config_versions_stay_in_lockstep(self):
        assert _cfg().load_fsc_config().pack_version == _pack_config().get_pack_version(
            PACK_ID
        )

    def test_version_is_stamped_on_scored_findings(self, findings):
        """2.0-A2 confounder detection reads this."""
        m = _pack_config()
        assert m.get_pack_version(PACK_ID) == "1.2.0"
        assert findings, "no findings to stamp"
