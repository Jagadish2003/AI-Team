"""Contract tests for 2.0-D2 T5 — Record Insurance-specific detector needs as future scope.

T5 is a design-review / documentation task. It proves two things and enforces a
boundary:

  1. The seeded Insurance estate still produces MEANINGFUL findings from the
     EXISTING service_cloud detectors — the template is useful today without any
     domain detector (DoD + D2 AC1).
  2. Every genuine domain-specific gap is RECORDED as future scope in a
     future-story record (docs/2.0-D2_INSURANCE_FUTURE_SCOPE.md), with enough
     detail per gap to estimate later, and each gap is CLASSIFIED so a detector
     gap is distinguished from a connector / focus / terminology / configuration
     / seed-quality problem (D2 AC4 / T5 "Future-story record").
  3. STRUCTURAL BOUNDARY: D2 contains NO Insurance detector, pack, scorer,
     ingestion surface, or runner dispatch branch; the template still reuses the
     registered service_cloud pack and its seven shipped detectors (T5
     "Implementation boundary" / DoD "structural review").

The seed lives at fixtures/insurance_estate_seed.json and is structurally
identical to the offline Salesforce fixture, so it drives the REAL detector
inputs rather than a parallel test-only shape.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

TEMPLATE_ID = "insurance"
PACK_ID = "service_cloud"

# The seven shipped Service Cloud detectors the template reuses — all pre-existing.
EXPECTED_DETECTORS = {
    "REPETITIVE_AUTOMATION",
    "HANDOFF_FRICTION",
    "APPROVAL_BOTTLENECK",
    "KNOWLEDGE_GAP",
    "INTEGRATION_CONCENTRATION",
    "PERMISSION_BOTTLENECK",
    "CROSS_SYSTEM_ECHO",
}

SC_DETECTOR_MODULES = (
    "repetition",
    "handoff_friction",
    "approval_delay",
    "knowledge_gap",
    "integration_concentration",
    "permission_bottleneck",
    "cross_system_echo",
)

# The candidate domain patterns the D2 story names — each must be recorded.
CANDIDATE_PATTERNS = (
    "coverage-determination",
    "reserve-change",
    "subrogation",
    "underwriting-referral",
    "claims leakage",
    "policy-renewal",
)

# Every per-gap field the future-story record must carry (T5 "Future-story
# record"). Six gaps ⇒ each label appears at least six times.
REQUIRED_GAP_FIELDS = (
    "**Business pattern:**",
    "**Required source objects:**",
    "**Expected evidence:**",
    "**Privacy constraints:**",
    "**Detector criterion:**",
    "**Terminology:**",
    "**Scoring considerations:**",
    "**Seed data:**",
    "**Acceptance criteria:**",
    "**Gap classification:**",
)
NUM_GAPS = 6

# Insurance-clear filename tokens that must NOT appear as a shipped detector /
# pack / scorer / ingest module under D2.
INSURANCE_MODULE_TOKENS = (
    "insurance", "claim", "underwrit", "subrog", "reserve", "leakage",
)


def _repo_root() -> Path:
    marker = Path("backend") / "discovery" / "packs" / "template_registry.py"
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / marker).is_file():
            return candidate
    import discovery.packs.template_registry as tr
    return Path(tr.__file__).resolve().parents[3]


REPO_ROOT = _repo_root()
BACKEND_ROOT = REPO_ROOT / "backend"
DOC_REL = "docs/2.0-D2_INSURANCE_FUTURE_SCOPE.md"
DOC_PATH = REPO_ROOT / "docs" / "2.0-D2_INSURANCE_FUTURE_SCOPE.md"
SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "insurance_estate_seed.json"


def _mod(name: str):
    try:
        return importlib.import_module(f"discovery.{name}")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module(f"backend.discovery.{name}")


def _tr():
    return _mod("packs.template_registry")


def _pack_config():
    return _mod("packs.pack_config")


@pytest.fixture(scope="module")
def template():
    defn = _tr().get_template(TEMPLATE_ID)
    assert defn is not None, "the Insurance template is not registered"
    return defn


@pytest.fixture(scope="module")
def seed():
    import json
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def seeded_findings(seed):
    out = []
    for name in SC_DETECTOR_MODULES:
        out.extend(_mod(f"detectors.{name}").detect(seed, {}, {}))
    return out


@pytest.fixture(scope="module")
def doc_text():
    assert DOC_PATH.is_file(), f"future-scope record missing at {DOC_REL}"
    return DOC_PATH.read_text(encoding="utf-8")


# ── DoD / AC1 — existing detectors produce meaningful findings ──────────────────

class TestExistingDetectorsDemonstrateMeaningfulFindings:

    def test_the_seed_produces_findings(self, seeded_findings):
        assert seeded_findings, "the seeded insurance estate produced no findings"

    def test_all_seven_shipped_detectors_fire(self, seeded_findings):
        fired = {f.detector_id for f in seeded_findings}
        assert fired == EXPECTED_DETECTORS, (
            f"missing={EXPECTED_DETECTORS - fired}, unexpected={fired - EXPECTED_DETECTORS}"
        )

    def test_every_finding_crosses_its_threshold(self, seeded_findings):
        for finding in seeded_findings:
            assert finding.metric_value >= finding.threshold, finding.detector_id

    def test_findings_come_from_the_shipped_salesforce_ingest(self, seeded_findings):
        for finding in seeded_findings:
            assert finding.signal_source == "salesforce", finding.detector_id

    def test_the_findings_are_meaningful_not_a_single_lucky_hit(self, seeded_findings):
        """DoD: "meaningful findings" — multiple distinct patterns, not one."""
        assert len({f.detector_id for f in seeded_findings}) >= 5


# ── T5 future-story record — recorded, not implemented ──────────────────────────

class TestFutureScopeRecordIsComplete:

    def test_the_record_exists(self, doc_text):
        assert doc_text.strip()
        assert "future-scope record" in doc_text.lower()
        assert "D2-FS1" in doc_text, "the future story id is not recorded"

    def test_it_names_every_candidate_pattern(self, doc_text):
        lower = doc_text.lower()
        missing = [p for p in CANDIDATE_PATTERNS if p not in lower]
        assert missing == [], f"future-scope record omits candidate patterns: {missing}"

    def test_every_gap_carries_the_required_fields(self, doc_text):
        """Each documented gap must carry the full estimate-later schema."""
        for label in REQUIRED_GAP_FIELDS:
            count = doc_text.count(label)
            assert count >= NUM_GAPS, (
                f"field {label!r} appears {count}× — expected one per gap "
                f"(>= {NUM_GAPS})"
            )

    def test_it_distinguishes_a_detector_gap_from_other_problem_kinds(self, doc_text):
        """T5: the record must distinguish a detector gap from a connector,
        focus, terminology, configuration, or seed-quality problem."""
        lower = doc_text.lower()
        assert "detector gap" in lower
        assert "connector" in lower
        # The non-detector problem kinds are named as explicitly NOT detector gaps.
        for kind in ("focus", "terminology", "configuration", "seed-quality"):
            assert kind in lower, f"gap-type vocabulary missing: {kind}"
        assert "not a detector gap" in lower, (
            "the record must state which concerns are NOT detector gaps"
        )

    def test_it_records_patterns_that_do_not_need_new_detection(self, doc_text):
        assert "not requiring additional detection" in doc_text.lower() or (
            "not require" in doc_text.lower()
        )

    def test_it_states_the_implementation_boundary(self, doc_text):
        lower = doc_text.lower()
        assert "implementation boundary" in lower
        for token in ("pack", "scorer", "detector", "ingest"):
            assert token in lower


class TestTemplateLinksTheRecord:
    """The template links the future-story record — config only, no code added."""

    def test_metadata_links_the_record(self, template):
        assert template.metadata.get("future_scope_ref") == DOC_REL
        assert template.metadata.get("future_story") == "D2-FS1"

    def test_the_referenced_doc_exists(self, template):
        ref = template.metadata["future_scope_ref"]
        assert (REPO_ROOT / ref).is_file(), f"future_scope_ref points at a missing file: {ref}"

    def test_future_scope_prose_still_names_the_uncovered_patterns(self, template):
        note = template.metadata["future_scope"].lower()
        for pattern in ("claim leakage", "subrogation", "reserve", "policy-renewal"):
            assert pattern in note, f"future_scope prose no longer names {pattern!r}"


# ── Implementation boundary — structural review (DoD) ──────────────────────────

class TestImplementationBoundaryStructural:

    def test_no_insurance_pack_is_registered(self):
        packs = _pack_config().list_packs()
        offenders = [p for p in packs if any(t in p.lower() for t in INSURANCE_MODULE_TOKENS)]
        assert offenders == [], f"D2 must add no Insurance pack; found {offenders}"

    def test_no_insurance_detector_module_exists(self):
        detectors_dir = BACKEND_ROOT / "discovery" / "detectors"
        offenders = [
            p.name for p in detectors_dir.glob("*.py")
            if any(t in p.name.lower() for t in INSURANCE_MODULE_TOKENS)
            or "policy" in p.name.lower()
        ]
        assert offenders == [], f"D2 must add no Insurance detector module; found {offenders}"

    def test_no_insurance_scorer_module_exists(self):
        packs_dir = BACKEND_ROOT / "discovery" / "packs"
        offenders = [
            p.name for p in packs_dir.glob("*.py")
            if any(t in p.name.lower() for t in INSURANCE_MODULE_TOKENS)
        ]
        assert offenders == [], f"D2 must add no Insurance scorer/pack module; found {offenders}"

    def test_no_insurance_ingestion_surface_exists(self):
        ingest_dir = BACKEND_ROOT / "discovery" / "ingest"
        offenders = [
            p.name for p in ingest_dir.rglob("*.py")
            if any(t in p.name.lower() for t in INSURANCE_MODULE_TOKENS)
        ]
        assert offenders == [], f"D2 must add no Insurance ingestion surface; found {offenders}"

    def test_runner_has_no_insurance_dispatch_branch(self):
        runner = (BACKEND_ROOT / "discovery" / "runner.py").read_text(encoding="utf-8").lower()
        for token in ("insurance", "subrogation", "claim leakage"):
            assert token not in runner, (
                f"runner.py references {token!r} — D2 adds no Insurance runner branch"
            )

    def test_scoring_engine_has_no_insurance_logic(self):
        scorer = (BACKEND_ROOT / "discovery" / "scorer.py").read_text(encoding="utf-8").lower()
        assert "insurance" not in scorer, "scorer.py references insurance — D2 adds no scoring logic"

    def test_the_template_still_reuses_the_service_cloud_pack(self, template):
        assert template.pack_id == PACK_ID

    def test_the_reused_pack_is_the_shipped_seven_detectors(self, seeded_findings):
        """The reused pack fires exactly its seven shipped detectors — nothing
        Insurance-specific was smuggled in."""
        pack = _pack_config().PACK_REGISTRY[PACK_ID]
        assert len(pack["detectors"]) == 7
        assert {f.detector_id for f in seeded_findings} == EXPECTED_DETECTORS

    def test_the_future_pack_story_is_not_secretly_registered(self):
        """D2-FS1 is RECORDED, not built — no pack claims to be the insurance pack."""
        registry = _pack_config().PACK_REGISTRY
        assert not any("insurance" in k.lower() for k in registry)
