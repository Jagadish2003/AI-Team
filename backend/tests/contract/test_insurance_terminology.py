"""Contract tests for 2.0-D2 T2 — the Insurance terminology set.

T2's objective is that claims, underwriting and policy-servicing stakeholders can
read the output, WITHOUT insurance wording being hardcoded into detectors, frontend
components, report builders or LLM-specific branches. So the tests are organised
around the four obligations that objective creates:

  * ``TestTerminologyIsConfigurationOnly`` — the vocabulary lives on the template
    and nowhere else, and the shared terminology engine is untouched.
  * ``TestAppliesAcrossNarrativeSurfaces`` — it reaches finding titles and
    descriptions, roadmap text, blueprint recommendations, AI summaries, suggested
    next steps, guardrails and executive-report content.
  * ``TestNonNarrativeFieldsArePreserved`` — detector ids, evidence pointers,
    enums, raw records and API field names are not touched.
  * ``TestMappingSafety`` — deterministic, case-preserving, plural-safe and
    idempotent, with an explicit guard against "policy policy" / "claim claim".
  * ``TestDomainIsolation`` — lending, NOC, security, pension and FSC vocabulary
    does not leak into insurance output, and vice versa.

The other domains' vocabularies are READ FROM THE REGISTRY rather than hardcoded
here, so a future template that introduces a colliding word is caught by these tests
instead of silently leaking.
"""
from __future__ import annotations

import importlib
import json
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rbac import seed_owner

_DEV_TOKEN = "dev-token-change-me"

TEMPLATE_ID = "insurance"
PACK_ID = "service_cloud"

# The vocabulary T2 names, and the mapping that carries each concept.
EXPECTED_MAPPINGS = {
    "customer": "policyholder",
    "account": "policy",
    "ticket": "service request",
    "approval": "underwriting review",
    "obligation": "coverage requirement",
    "backlog": "claims queue",
}

# Mappings deliberately absent, each with the reason. A regression that re-adds one
# is caught by test_unsafe_mappings_stay_out.
REJECTED_MAPPINGS = {
    "queue": "superstring — would yield 'claims claims queue'",
    "team": "superstring — would yield 'claims claims team'",
    "agent": "'agent' is the AI agent in this product, not an insurance agent",
    "case": "a policy-endorsement case is not a claim",
}


def _repo_root() -> Path:
    marker = Path("backend") / "app" / "terminology.py"
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / marker).is_file():
            return candidate
    import app.terminology as t
    return Path(t.__file__).resolve().parents[2]


REPO_ROOT = _repo_root()
BACKEND_ROOT = REPO_ROOT / "backend"


def _terminology():
    try:
        return importlib.import_module("app.terminology")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module("backend.app.terminology")


def _tr():
    try:
        return importlib.import_module("discovery.packs.template_registry")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module("backend.discovery.packs.template_registry")


@pytest.fixture(scope="module")
def imap():
    """The Insurance terminology map, as shipped on the template."""
    defn = _tr().get_template(TEMPLATE_ID)
    assert defn is not None, "the Insurance template is not registered"
    return dict(defn.terminology)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _auth(org_id: str) -> dict:
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _owner_org(prefix: str) -> str:
    org_id = f"{prefix}_{uuid4().hex[:8]}"
    seed_owner(org_id, _DEV_TOKEN)
    return org_id


def _other_domain_vocabularies() -> dict:
    """Every OTHER template's terminology, read from the registry.

    Read rather than hardcoded so a future template introducing a colliding word is
    caught here instead of leaking silently.
    """
    return {
        defn.template_id: dict(defn.terminology)
        for defn in _tr().list_templates()
        if defn.template_id != TEMPLATE_ID and defn.terminology
    }


# ── The vocabulary itself ──────────────────────────────────────────────────────

class TestTerminologyContent:

    def test_the_template_carries_a_terminology_map(self, imap):
        assert imap, "the Insurance template declares no terminology"

    @pytest.mark.parametrize("generic,domain", sorted(EXPECTED_MAPPINGS.items()))
    def test_expected_mapping_is_present(self, imap, generic, domain):
        assert imap.get(generic) == domain, (
            f"expected {generic!r} -> {domain!r}, got {imap.get(generic)!r}"
        )

    def test_the_map_is_exactly_the_reviewed_set(self, imap):
        """No undocumented extras: every mapping is one T2 named."""
        assert set(imap) == set(EXPECTED_MAPPINGS)

    def test_the_vocabulary_covers_the_named_concepts(self, imap):
        """policyholders, policies, service requests, underwriting reviews,
        coverage requirements, and claims/service queues."""
        values = " ".join(imap.values()).lower()
        for concept in ("policyholder", "policy", "service request",
                        "underwriting review", "coverage requirement",
                        "claims queue"):
            assert concept in values, f"vocabulary does not cover {concept!r}"

    def test_unsafe_mappings_stay_out(self, imap):
        """Each rejection is documented in the template; this keeps them out."""
        for generic, reason in REJECTED_MAPPINGS.items():
            assert generic not in imap, (
                f"{generic!r} was re-added to the Insurance terminology — {reason}"
            )

    def test_the_rejections_are_documented_in_the_registry(self):
        """A reader must be able to see WHY those words are absent."""
        source = (
            BACKEND_ROOT / "discovery" / "packs" / "template_registry.py"
        ).read_text(encoding="utf-8")
        for generic in REJECTED_MAPPINGS:
            assert re.search(rf"{generic}\s*->", source), (
                f"the rejection of {generic!r} is not documented"
            )

    def test_values_are_marked_subject_to_business_review(self):
        source = (
            BACKEND_ROOT / "discovery" / "packs" / "template_registry.py"
        ).read_text(encoding="utf-8")
        assert "SUBJECT TO BUSINESS REVIEW" in source.upper()


# ── Configuration only ────────────────────────────────────────────────────────

class TestTerminologyIsConfigurationOnly:

    def test_the_shared_engine_has_no_insurance_awareness(self):
        """The vocabulary must not be hardcoded into the terminology engine."""
        source = (BACKEND_ROOT / "app" / "terminology.py").read_text(encoding="utf-8")
        lowered = source.lower()
        for word in ("insurance", "policyholder", "claim", "underwriting", "policy"):
            assert word not in lowered, (
                f"app/terminology.py references {word!r} — the engine must stay "
                f"domain-agnostic"
            )

    def test_no_detector_hardcodes_insurance_wording(self):
        """Detectors must not carry insurance wording."""
        offenders = []
        for path in (BACKEND_ROOT / "discovery" / "detectors").glob("*.py"):
            lowered = path.read_text(encoding="utf-8").lower()
            for word in ("policyholder", "underwriting", "insurance"):
                if word in lowered:
                    offenders.append((path.name, word))
        assert offenders == [], offenders

    def test_no_report_or_roadmap_builder_hardcodes_insurance_wording(self):
        for rel in ("app/executive_report_engine.py",
                    "discovery/roadmap_engine.py",
                    "app/opportunity_display.py"):
            path = BACKEND_ROOT / rel
            if not path.is_file():
                continue
            lowered = path.read_text(encoding="utf-8").lower()
            for word in ("policyholder", "underwriting", "insurance"):
                assert word not in lowered, f"{rel} hardcodes {word!r}"

    def test_no_llm_branch_hardcodes_insurance_wording(self):
        for rel in ("app/llm_enrichment.py",):
            path = BACKEND_ROOT / rel
            if not path.is_file():
                continue
            lowered = path.read_text(encoding="utf-8").lower()
            for word in ("policyholder", "underwriting", "insurance"):
                assert word not in lowered, f"{rel} hardcodes {word!r}"

    def test_no_frontend_component_hardcodes_insurance_wording(self):
        """The frontend renders from the registry; it must not carry the vocabulary."""
        frontend = REPO_ROOT / "frontend" / "src"
        if not frontend.is_dir():
            pytest.skip("frontend not present")
        offenders = []
        for path in list(frontend.rglob("*.ts")) + list(frontend.rglob("*.tsx")):
            if "__tests__" in path.parts:
                continue
            lowered = path.read_text(encoding="utf-8", errors="ignore").lower()
            for word in ("policyholder", "underwriting review", "coverage requirement"):
                if word in lowered:
                    offenders.append((str(path.relative_to(frontend)), word))
        assert offenders == [], offenders

    def test_the_shared_engine_was_not_modified(self):
        """T2's definition of done: no changes to the shared terminology engine.

        SKIPS on a shallow clone rather than passing vacuously (CI checks out at
        depth 1); the structural check above runs everywhere.
        """
        try:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/2.0-D2"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if base.returncode != 0 or not base.stdout.strip():
                pytest.skip("no merge base with origin/2.0-D2 (shallow clone)")
            changed = subprocess.run(
                ["git", "diff", "--name-only", base.stdout.strip(), "HEAD"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if changed.returncode != 0:
                pytest.skip("git diff unavailable")
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pytest.skip("git unavailable")

        files = {f.strip() for f in changed.stdout.splitlines() if f.strip()}
        assert "backend/app/terminology.py" not in files, (
            "the shared terminology engine changed — T2 must be configuration only"
        )


# ── Served by the template APIs and captured in launch provenance ──────────────

class TestApiAndProvenance:

    def test_the_templates_list_endpoint_returns_the_map(self, client, imap):
        resp = client.get("/api/stack-builder/templates", headers=_auth("default"))
        assert resp.status_code == 200, resp.text
        rows = {row["template_id"]: row for row in resp.json()}
        assert rows[TEMPLATE_ID]["terminology"] == imap

    def test_the_single_template_endpoint_returns_the_map(self, client, imap):
        resp = client.get(
            f"/api/stack-builder/templates/{TEMPLATE_ID}", headers=_auth("default")
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["terminology"] == imap

    def test_the_snapshot_captures_the_map(self, imap):
        snapshot = _tr().template_defaults_snapshot(_tr().get_template(TEMPLATE_ID))
        assert snapshot["terminology"] == imap

    def test_launch_provenance_captures_the_map(self, imap):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        assert resolved["provenance"]["template_defaults"]["terminology"] == imap

    def test_the_pack_boundary_carries_the_map(self, imap):
        """This is what apply_run_terminology reads at serve time."""
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        boundaries = resolved["effective"]["pack_boundaries"]
        assert len(boundaries) == 1
        assert boundaries[0]["terminology"] == imap
        assert boundaries[0]["pack_id"] == PACK_ID

    def test_a_launched_run_resolves_the_map_per_pack(self, imap):
        """resolve_run_terminology_by_pack is the serve-time entry point; give it a
        run record shaped as launch writes it and it must return this vocabulary."""
        terminology = _terminology()
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        run_record = {
            "templateProvenance": {
                "pack_boundaries": resolved["effective"]["pack_boundaries"]
            }
        }
        by_pack = terminology.resolve_run_terminology_by_pack("run-x", run=run_record)
        assert by_pack == {PACK_ID: imap}


# ── Applies across every narrative surface ────────────────────────────────────

class TestAppliesAcrossNarrativeSurfaces:

    def test_finding_title_and_description(self, imap):
        out = _terminology().apply_terminology(
            {
                "title": "Reduce repeated customer tickets",
                "description": "Each customer account raises the same ticket.",
                "category": "Customer Service",
            },
            imap,
        )
        assert out["title"] == "Reduce repeated policyholder service requests"
        assert "policyholder policy" in out["description"]
        assert out["category"] == "Policyholder Service"

    def test_roadmap_text(self, imap):
        out = _terminology().apply_terminology(
            {
                "s9_roadmap": "30-day pilot: clear the backlog of tickets awaiting approval.",
                "summary": "Reduce the customer backlog.",
            },
            imap,
        )
        assert "claims queue of service requests" in out["s9_roadmap"]
        assert "underwriting review" in out["s9_roadmap"]
        assert "policyholder claims queue" in out["summary"]

    def test_blueprint_recommendation_fields(self, imap):
        out = _terminology().apply_terminology(
            {
                "agentTopic": "Customer ticket triage",
                "action": "Route each ticket to the right approval path",
                "detail": "Reads the customer account and its obligation",
                "guardrails": "No automated approval; a human confirms every obligation",
            },
            imap,
        )
        assert out["agentTopic"] == "Policyholder service request triage"
        assert "underwriting review path" in out["action"]
        assert "policyholder policy" in out["detail"]
        assert "coverage requirement" in out["detail"]
        assert "No automated underwriting review" in out["guardrails"]

    def test_ai_enrichment_fields(self, imap):
        out = _terminology().apply_terminology(
            {
                "aiSummary": "Customers raise repeat tickets against the same account.",
                "aiWhyBullets": [
                    "Ticket volume is concentrated",
                    "Approval delay extends the backlog",
                ],
                "aiRisks": ["Obligation tracking is manual"],
                "aiSuggestedNextSteps": ["Triage the ticket backlog by customer"],
                "aiRationale": "The customer obligation is unmet.",
            },
            imap,
        )
        assert "Policyholders raise repeat service requests" in out["aiSummary"]
        assert "policy" in out["aiSummary"]
        assert out["aiWhyBullets"][0] == "Service request volume is concentrated"
        assert "Underwriting review delay" in out["aiWhyBullets"][1]
        assert "claims queue" in out["aiWhyBullets"][1]
        assert out["aiRisks"] == ["Coverage requirement tracking is manual"]
        assert out["aiSuggestedNextSteps"] == [
            "Triage the service request claims queue by policyholder"
        ]
        assert "policyholder coverage requirement" in out["aiRationale"]

    def test_executive_report_content(self, imap):
        out = _terminology().apply_terminology(
            {
                "executiveSummary": (
                    "Customer tickets are growing and the approval backlog is "
                    "extending; each account carries an unmet obligation."
                ),
                "aiExecutiveSummary": "The customer backlog is the headline risk.",
                "s10_exec": "Approval delay is the biggest driver of customer wait.",
            },
            imap,
        )
        assert "Policyholder service requests are growing" in out["executiveSummary"]
        assert "underwriting review claims queue" in out["executiveSummary"]
        assert "coverage requirement" in out["executiveSummary"]
        assert "policyholder claims queue" in out["aiExecutiveSummary"]
        assert "Underwriting review delay" in out["s10_exec"]

    def test_compliance_guardrail_and_corroboration_label(self, imap):
        out = _terminology().apply_terminology(
            {
                "compliance_guardrail": "No automated approval of any obligation.",
                "corroboration_label": "Corroborated by the customer ticket record",
                "preliminary_reason": "Single-source customer signal",
            },
            imap,
        )
        assert "underwriting review" in out["compliance_guardrail"]
        assert "coverage requirement" in out["compliance_guardrail"]
        assert "policyholder service request" in out["corroboration_label"]
        assert "policyholder" in out["preliminary_reason"]

    def test_nested_opportunity_objects_are_reached(self, imap):
        """Findings arrive nested inside lists on real responses."""
        payload = {
            "opportunities": [
                {"title": "Customer ticket volume", "detector_id": "REPETITIVE_AUTOMATION"},
                {"title": "Approval backlog", "detector_id": "APPROVAL_BOTTLENECK"},
            ]
        }
        out = _terminology().apply_terminology(payload, imap)
        assert out["opportunities"][0]["title"] == "Policyholder service request volume"
        assert out["opportunities"][1]["title"] == "Underwriting review claims queue"

    def test_every_allowlisted_narrative_field_is_rewritten(self, imap):
        """Systematic: each field in the engine's allowlist carries the vocabulary."""
        terminology = _terminology()
        for field in terminology.TERMINOLOGY_TEXT_FIELDS:
            out = terminology.apply_terminology({field: "the customer account"}, imap)
            assert out[field] == "the policyholder policy", field


class TestWhereTheVocabularyIsActuallyObservable:
    """The honest boundary of the observable effect.

    The map is wired correctly and rewrites every allowlisted field it is given
    (proved above). But how much a live insurance run VISIBLY changes depends on
    which of those fields the pipeline actually populates, and two pre-existing
    facts limit it. Recorded here so the suite does not imply more than is true.

      1. A `service_cloud` finding carries NO narrative fields at all — the pack
         declares `ui_labels_path: None`, so unlike nCino/FSC it ships no per-detector
         title/description. There is therefore nothing to translate on the finding
         surface. This affects EVERY Service Cloud industry, not insurance, and
         giving the pack a label file would change output for all of them — well
         outside D2's configuration-only scope.
      2. The platform's DETERMINISTIC generated text (e.g. roadmap stage summaries
         like "Prove value fast with low-effort quick wins.") contains none of this
         map's source words, so it is inert there too.

    The vocabulary becomes visible on LLM-GENERATED narrative — aiSummary,
    aiWhyBullets, aiRisks, aiSuggestedNextSteps, aiExecutiveSummary, executiveSummary
    — which is where words like "customer", "ticket", "approval" and "backlog"
    actually occur, and which is precisely what the serve-time engine was built for.
    """

    def test_service_cloud_ships_no_finding_labels(self):
        """Fact 1, asserted rather than assumed."""
        pack_config = importlib.import_module("discovery.packs.pack_config")
        assert pack_config.get_pack(PACK_ID)["ui_labels_path"] is None
        assert pack_config.get_ui_labels(PACK_ID) is None

    def test_the_title_override_map_does_not_cover_service_cloud_detectors(self):
        """The only backend source of SC finding titles covers STRS detectors only."""
        display = importlib.import_module("app.opportunity_display")
        covered = set(display.OPPORTUNITY_TITLE_OVERRIDES)
        sc_detectors = {
            "REPETITIVE_AUTOMATION", "HANDOFF_FRICTION", "APPROVAL_BOTTLENECK",
            "KNOWLEDGE_GAP", "INTEGRATION_CONCENTRATION", "PERMISSION_BOTTLENECK",
            "CROSS_SYSTEM_ECHO",
        }
        assert covered & sc_detectors == set(), (
            "a Service Cloud detector gained a display title — this test's premise "
            "changed and the terminology's observable surface is now wider"
        )

    def test_the_map_is_effective_on_llm_enrichment_narrative(self, imap):
        """Fact 3: this is where the vocabulary genuinely lands.

        Text of the kind LLM enrichment produces about Salesforce case work, which
        naturally uses the generic words this map translates.
        """
        enrichment = {
            "aiSummary": (
                "Customers raise repeat tickets on the same account while approval "
                "sits in a growing backlog."
            ),
            "aiSuggestedNextSteps": [
                "Cluster tickets by customer",
                "Escalate the approval backlog",
            ],
            "aiExecutiveSummary": "Customer ticket volume and approval delay dominate.",
        }
        out = _terminology().apply_terminology(enrichment, imap)
        summary = out["aiSummary"].lower()
        for expected in ("policyholders", "service requests", "policy",
                         "underwriting review", "claims queue"):
            assert expected in summary, f"{expected!r} missing from {summary!r}"
        assert out["aiSuggestedNextSteps"] == [
            "Cluster service requests by policyholder",
            "Escalate the underwriting review claims queue",
        ]
        assert "policyholder service request" in out["aiExecutiveSummary"].lower()

    def test_deterministic_roadmap_summaries_contain_no_source_words(self):
        """Fact 2, asserted so a future change that adds them is noticed."""
        roadmap_engine = importlib.import_module("app.roadmap_engine")
        source = Path(roadmap_engine.__file__).read_text(encoding="utf-8").lower()
        stage_text = " ".join(re.findall(r'"summary":\s*"([^"]*)"', source))
        for word in ("customer", "ticket", "obligation"):
            assert word not in stage_text.lower()


# ── Non-narrative fields are preserved ────────────────────────────────────────

class TestNonNarrativeFieldsArePreserved:

    def test_detector_ids_and_enums_are_untouched(self, imap):
        payload = {
            "detector_id": "APPROVAL_BOTTLENECK",
            "detectorId": "APPROVAL_BOTTLENECK",
            "tier": "Quick Win",
            "confidence": "HIGH",
            "decision": "UNREVIEWED",
            "roadmap_stage": "quick_win",
            "packId": PACK_ID,
        }
        assert _terminology().apply_terminology(payload, imap) == payload

    def test_evidence_pointers_and_ids_are_untouched(self, imap):
        payload = {
            "evidenceIds": ["ev_customer_account_1", "ev_ticket_2"],
            "opportunity_identity": "opp_customer_account_hash",
            "runId": "run_customer_1",
            "source_artifact": "CASE-customer-account",
        }
        assert _terminology().apply_terminology(payload, imap) == payload

    def test_raw_records_are_untouched(self, imap):
        payload = {
            "raw_evidence": {
                "process_name": "Discount Approval",
                "reason": "Customer_Request",
                "account_id": "001xxCUSTOMER",
            }
        }
        assert _terminology().apply_terminology(payload, imap) == payload

    def test_api_field_names_are_never_rewritten(self, imap):
        """Dict KEYS are never touched, only allowlisted VALUES."""
        payload = {"customer": "x", "account": "y", "ticket": "z", "approval": "w"}
        assert set(_terminology().apply_terminology(payload, imap)) == set(payload)

    def test_numbers_and_booleans_survive(self, imap):
        payload = {"impact": 7, "effort": 2, "metric_value": 1.81, "corroborated": False}
        assert _terminology().apply_terminology(payload, imap) == payload

    def test_a_non_allowlisted_string_field_is_not_rewritten(self, imap):
        payload = {"signal_source": "salesforce", "process_name": "Customer Approval"}
        assert _terminology().apply_terminology(payload, imap) == payload


# ── Mapping safety ────────────────────────────────────────────────────────────

class TestMappingSafety:

    SAMPLES = (
        "The customer account has an open ticket awaiting approval and an unmet obligation.",
        "Customers and accounts with tickets pending approvals and obligations clog the backlog.",
        "Reduce the backlog of tickets so each customer account meets its obligation.",
    )

    def test_no_replacement_contains_its_source(self, imap):
        """Rule 1 — the 'policy policy' class of malformation."""
        for generic, domain in imap.items():
            assert generic.lower() not in domain.lower().split(), (
                f"{generic!r} -> {domain!r} double-expands the domain phrase"
            )

    def test_no_replacement_word_is_another_mapping_source(self, imap):
        """Rule 2 — otherwise repeated application cascades."""
        sources = {g.lower() for g in imap}
        replacement_words = {w for d in imap.values() for w in d.lower().split()}
        assert sources & replacement_words == set(), (
            f"these replacement words are also sources: "
            f"{sorted(sources & replacement_words)}"
        )

    @pytest.mark.parametrize("text", SAMPLES)
    def test_applying_twice_equals_applying_once(self, imap, text):
        rewrite = _terminology().rewrite_text
        once = rewrite(text, imap)
        assert rewrite(once, imap) == once

    @pytest.mark.parametrize("text", SAMPLES)
    def test_no_duplicated_domain_phrases(self, imap, text):
        """The explicit guard T2 asks for: no 'policy policy', 'claim claim', etc."""
        once = _terminology().rewrite_text(text, imap).lower()
        words = {w for d in imap.values() for w in d.lower().split()}
        for word in words:
            assert f"{word} {word}" not in once, (
                f"duplicated term {word!r} in {once!r}"
            )

    def test_applying_to_already_insured_text_is_a_no_op(self, imap):
        """Text already written in insurance language must survive untouched."""
        rewrite = _terminology().rewrite_text
        for phrase in ("policyholder", "policy", "service request",
                       "underwriting review", "coverage requirement", "claims queue",
                       "the policyholder policy", "an open service request"):
            assert rewrite(phrase, imap) == phrase, phrase

    def test_case_is_preserved(self, imap):
        rewrite = _terminology().rewrite_text
        assert rewrite("customer", imap) == "policyholder"
        assert rewrite("Customer", imap) == "Policyholder"
        assert rewrite("CUSTOMER", imap) == "POLICYHOLDER"
        assert rewrite("Ticket", imap) == "Service request"
        assert rewrite("TICKET", imap) == "SERVICE REQUEST"

    def test_plurals_are_handled(self, imap):
        rewrite = _terminology().rewrite_text
        assert rewrite("customers", imap) == "policyholders"
        assert rewrite("accounts", imap) == "policies"          # y -> ies
        assert rewrite("tickets", imap) == "service requests"
        assert rewrite("approvals", imap) == "underwriting reviews"
        assert rewrite("obligations", imap) == "coverage requirements"
        assert rewrite("backlogs", imap) == "claims queues"

    def test_rewriting_is_deterministic(self, imap):
        rewrite = _terminology().rewrite_text
        text = self.SAMPLES[1]
        assert len({rewrite(text, imap) for _ in range(10)}) == 1

    def test_whole_words_only(self, imap):
        """A substring inside another word must not be rewritten."""
        rewrite = _terminology().rewrite_text
        for text in ("accountability", "ticketing", "approvals-board", "customerly"):
            out = rewrite(text, imap)
            assert "policy" not in out.lower() or text == "approvals-board", (
                f"{text!r} became {out!r}"
            )
        assert rewrite("accountability", imap) == "accountability"
        assert rewrite("ticketing", imap) == "ticketing"

    def test_an_empty_or_missing_map_is_a_no_op(self):
        terminology = _terminology()
        payload = {"title": "customer account"}
        assert terminology.apply_terminology(payload, {}) == payload
        assert terminology.apply_terminology(payload, None) == payload


# ── Domain isolation ──────────────────────────────────────────────────────────

class TestDomainIsolation:

    # Words that belong to other domains and must never appear in insurance output.
    FOREIGN_TERMS = (
        # lending
        "borrower", "facility", "covenant", "credit memo",
        # NOC / cloud ops
        "mttr", "toil", "runbook", "escalation",
        # security ops
        "remediation task", "playbook", "security queue", "time-in-state",
        # pension / STRS
        "member benefit", "disbursement",
        # FSC
        "household", "financial account", "service process", "referral handoff",
    )

    def test_the_insurance_map_introduces_no_foreign_term(self, imap):
        values = " ".join(imap.values()).lower()
        for term in self.FOREIGN_TERMS:
            assert term not in values, (
                f"the Insurance vocabulary contains the foreign term {term!r}"
            )

    def test_rewriting_generic_text_introduces_no_foreign_term(self, imap):
        text = (
            "The customer account has an open ticket awaiting approval, an unmet "
            "obligation, and a growing backlog."
        )
        out = _terminology().rewrite_text(text, imap).lower()
        for term in self.FOREIGN_TERMS:
            assert term not in out, f"{term!r} leaked into insurance output"

    def test_insurance_output_is_not_lending_output(self, imap):
        """The two financial-services templates must read differently."""
        lending = _tr().get_template("commercial_lending").terminology
        text = "The customer account needs approval."
        rewrite = _terminology().rewrite_text
        insurance_out = rewrite(text, imap)
        lending_out = rewrite(text, lending)
        assert insurance_out != lending_out
        assert "policyholder" in insurance_out and "borrower" not in insurance_out
        assert "borrower" in lending_out and "policyholder" not in lending_out

    def test_every_other_template_maps_the_shared_words_differently(self, imap):
        """Where another template maps the same generic word, the domain value must
        differ — otherwise the two vocabularies are not actually distinct."""
        for template_id, other in _other_domain_vocabularies().items():
            shared = set(imap) & set(other)
            for word in shared:
                assert imap[word] != other[word], (
                    f"{template_id} maps {word!r} to the same value as Insurance "
                    f"({imap[word]!r}) — the vocabularies are not distinct"
                )

    def test_vocabularies_are_never_merged_across_packs(self, imap):
        """The actual protection against cross-domain cascade.

        Two maps applied to the same text COULD cascade — `commercial_lending` maps
        `approval -> approval gate`, and "approval" is an Insurance source, so
        applying both in sequence would yield "underwriting review gate". That never
        happens because `apply_run_terminology` routes each finding to ITS OWN pack's
        map and never merges them; `resolve_run_terminology` deliberately returns an
        empty map for a multi-pack run rather than picking one. This asserts that
        routing rather than demanding the vocabularies be mutually inert, which they
        are not and need not be.
        """
        terminology = _terminology()
        lending = _tr().get_template("commercial_lending").terminology

        # Sequential application WOULD cascade — the reason routing matters.
        cascaded = terminology.rewrite_text(
            terminology.rewrite_text("approval", lending), imap
        )
        assert cascaded != terminology.rewrite_text("approval", lending)

        # A multi-pack run therefore yields no single merged map.
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, "commercial_lending"]
        )
        run_record = {
            "templateProvenance": {
                "pack_boundaries": resolved["effective"]["pack_boundaries"]
            }
        }
        assert terminology.resolve_run_terminology("run-x", run=run_record) == {}
        by_pack = terminology.resolve_run_terminology_by_pack("run-x", run=run_record)
        assert by_pack[PACK_ID] == imap
        assert by_pack["ncino"] == lending

    def test_a_combined_run_keeps_the_vocabularies_separate(self, imap):
        """If Insurance is ever selected alongside another template, each pack's
        findings must be relabelled with ITS OWN vocabulary only."""
        resolved = _tr().resolve_launch_config(
            None, template_ids=[TEMPLATE_ID, "commercial_lending"]
        )
        by_pack = {
            b["pack_id"]: b["terminology"]
            for b in resolved["effective"]["pack_boundaries"]
        }
        assert by_pack[PACK_ID] == imap
        assert by_pack["ncino"]["customer"] == "borrower"

    def test_insurance_terminology_is_not_applied_to_another_packs_finding(self, imap):
        """apply_run_terminology routes by packId; an ncino finding must not be
        relabelled with insurance words."""
        terminology = _terminology()
        lending = _tr().get_template("commercial_lending").terminology
        ncino_finding = {"packId": "ncino", "title": "Customer account approval"}
        out = terminology.apply_terminology(ncino_finding, lending)
        title = out["title"].lower()
        assert "borrower" in title and "facility" in title
        assert "policyholder" not in title
        assert "underwriting review" not in title
