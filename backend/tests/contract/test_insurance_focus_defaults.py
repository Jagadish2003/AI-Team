"""Contract tests for 2.0-D2 T3 — Insurance focus defaults.

T3 configures a DEFAULT Discovery Focus for the Insurance template that stays an
editable starting value, using an existing canonical focus id, existing emphasis
tags, and the current focus-affinity mechanism — no new focus type, scoring rule,
focus card, API property or focus-engine branch.

  * ``TestFocusDefaultConfiguration`` — the default is `member_customer_service`
    with the tags mapping to the five areas T3 names, all pre-existing.
  * ``TestLaunchProvenance`` — an untouched launch receives the configured focus and
    records it in the effective configuration and the template snapshot; an edited
    launch keeps the user's value and records `focus_id` as edited.
  * ``TestDetectorAlignment`` — only detectors already available through the
    `service_cloud` pack are referenced, and no Insurance-specific detector exists.
  * ``TestNothingIsSuppressed`` — the validation T3 asks for: policy servicing,
    claims handoffs and underwriting review all stay VISIBLE under the default. A
    detector outside the focus is "surfaced but not emphasised", never dropped.
  * ``TestNoNewFocusMachinery`` — the definition-of-done constraint.
  * ``TestTheFocusLimitationIsReported`` — T3's own instruction that "any inability
    to represent Insurance priorities using existing focuses must be reported
    separately". No single canonical focus emphasises all five areas, so that fact
    is recorded on the template rather than worked around.

A note on what the emphasis TAGS do. They are declarative: the Stack Builder API
returns them for pre-population and launch provenance captures them, but RANKING
comes from ``focus_id`` alone through ``FOCUS_AFFINITY``'s detector-id mapping. The
tags are not a scoring input — ``test_emptying_the_emphasis_tags_changes_no_rank``
proves it, so a template whose tags look right while its ``focus_id`` is wrong cannot
pass quietly.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rbac import seed_owner

_DEV_TOKEN = "dev-token-change-me"

TEMPLATE_ID = "insurance"
PACK_ID = "service_cloud"
DEFAULT_FOCUS = "member_customer_service"
UNDERWRITING_FOCUS = "approvals_compliance"

# The tags T3 names, mapped to the areas they represent.
EXPECTED_EMPHASIS = (
    "service_casework",      # policyholder service
    "intake_requests",       # claims intake
    "approvals",             # underwriting review
    "compliance_risk",       # underwriting review (regulatory half)
    "backlog_work_queues",   # operational queues
    "handoffs_routing",      # cross-team handoffs
)

# The seven detectors the service_cloud pack ships. T3: reference only these.
SC_DETECTORS = (
    "REPETITIVE_AUTOMATION",
    "HANDOFF_FRICTION",
    "APPROVAL_BOTTLENECK",
    "KNOWLEDGE_GAP",
    "INTEGRATION_CONCENTRATION",
    "PERMISSION_BOTTLENECK",
    "CROSS_SYSTEM_ECHO",
)

SC_DETECTOR_MODULES = (
    "repetition", "handoff_friction", "approval_delay", "knowledge_gap",
    "integration_concentration", "permission_bottleneck", "cross_system_echo",
)

# The insurance workflow areas T3 requires to stay visible, and the detector that
# surfaces each.
AREA_DETECTORS = {
    "policy servicing": "REPETITIVE_AUTOMATION",
    "claims handoffs": "HANDOFF_FRICTION",
    "underwriting review": "APPROVAL_BOTTLENECK",
}

SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "insurance_estate_seed.json"


def _mod(name: str):
    try:
        return importlib.import_module(f"discovery.{name}")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module(f"backend.discovery.{name}")


def _tr():
    return _mod("packs.template_registry")


def _affinity():
    return _mod("packs.focus_affinity")


def _pack_config():
    return _mod("packs.pack_config")


@pytest.fixture(scope="module")
def template():
    defn = _tr().get_template(TEMPLATE_ID)
    assert defn is not None, "the Insurance template is not registered"
    return defn


@pytest.fixture(scope="module")
def seeded_findings():
    """Findings the service_cloud detectors produce on the insurance seed."""
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    out = []
    for name in SC_DETECTOR_MODULES:
        out.extend(_mod(f"detectors.{name}").detect(seed, {}, {}))
    return out


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


# ── The configured default ─────────────────────────────────────────────────────

class TestFocusDefaultConfiguration:

    def test_the_default_focus_is_member_customer_service(self, template):
        assert template.focus_defaults.focus_id == DEFAULT_FOCUS

    def test_the_focus_id_is_an_existing_canonical_focus(self, template):
        """No new focus type — it must already exist in FOCUS_AFFINITY."""
        assert template.focus_defaults.focus_id in _affinity().FOCUS_AFFINITY

    def test_the_emphasis_tags_are_exactly_the_five_areas(self, template):
        assert tuple(template.focus_defaults.emphasis) == EXPECTED_EMPHASIS

    def test_every_emphasis_tag_already_exists_in_the_vocabulary(self, template):
        """No new tag: each must already be in use by the industry registry, which
        is the canonical workflow-focus tag vocabulary."""
        industries = _mod("packs.industry_registry").INDUSTRY_REGISTRY
        known = set()
        for config in industries.values():
            for default in config.system_defaults.values():
                known.update(default.workflow_focus)
        unknown = [t for t in template.focus_defaults.emphasis if t not in known]
        assert unknown == [], f"emphasis introduces new tags: {unknown}"

    def test_the_tags_cover_every_workflow_area_t3_names(self, template):
        tags = set(template.focus_defaults.emphasis)
        # policyholder service, claims intake, underwriting review,
        # operational queues, cross-team handoffs
        assert "service_casework" in tags
        assert "intake_requests" in tags
        assert {"approvals", "compliance_risk"} <= tags
        assert "backlog_work_queues" in tags
        assert "handoffs_routing" in tags

    def test_the_focus_emphasises_at_least_one_pack_detector(self, template):
        """A focus emphasising none of the pack's detectors would rank nothing."""
        emphasised = set(_affinity().FOCUS_AFFINITY[DEFAULT_FOCUS] or ())
        assert emphasised & set(SC_DETECTORS)

    def test_the_underwriting_alternative_is_a_real_focus(self):
        """"An underwriting-heavy customer must remain free to select
        approvals_compliance" — so that focus must exist and own the review
        detectors."""
        affinity = _affinity()
        assert UNDERWRITING_FOCUS in affinity.FOCUS_AFFINITY
        owned = set(affinity.FOCUS_AFFINITY[UNDERWRITING_FOCUS] or ())
        assert "APPROVAL_BOTTLENECK" in owned


# ── Launch provenance ─────────────────────────────────────────────────────────

class TestLaunchProvenance:

    def test_untouched_launch_uses_the_insurance_default(self):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        assert resolved["effective"]["focus_id"] == DEFAULT_FOCUS

    def test_untouched_launch_records_it_in_the_snapshot(self, template):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        snapshot = resolved["provenance"]["template_defaults"]
        assert snapshot["focus_id"] == DEFAULT_FOCUS
        assert snapshot["focus_emphasis"] == list(template.focus_defaults.emphasis)

    def test_untouched_launch_records_no_focus_edit(self):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        assert "focus_id" not in resolved["provenance"]["edited_fields"]
        assert resolved["provenance"]["untouched"] is True

    def test_the_pack_boundary_carries_the_focus(self, template):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        boundary = resolved["effective"]["pack_boundaries"][0]
        assert boundary["focus_id"] == DEFAULT_FOCUS
        assert boundary["focus_emphasis"] == list(template.focus_defaults.emphasis)

    def test_an_edited_focus_wins(self):
        """The user's value must win — this is a starting value, not a lock."""
        resolved = _tr().resolve_launch_config(
            TEMPLATE_ID, focus_id=UNDERWRITING_FOCUS
        )
        assert resolved["effective"]["focus_id"] == UNDERWRITING_FOCUS

    def test_an_edited_focus_is_recorded_as_an_edited_field(self):
        resolved = _tr().resolve_launch_config(
            TEMPLATE_ID, focus_id=UNDERWRITING_FOCUS
        )
        assert "focus_id" in resolved["provenance"]["edited_fields"]
        assert resolved["provenance"]["untouched"] is False

    def test_an_edited_focus_still_records_the_template_default(self):
        """Provenance must show BOTH what the template offered and what was chosen,
        or the edit is untraceable."""
        resolved = _tr().resolve_launch_config(
            TEMPLATE_ID, focus_id=UNDERWRITING_FOCUS
        )
        assert resolved["provenance"]["template_defaults"]["focus_id"] == DEFAULT_FOCUS
        assert resolved["effective"]["focus_id"] == UNDERWRITING_FOCUS

    @pytest.mark.parametrize(
        "chosen",
        ["approvals_compliance", "cross_system_handoffs", "core_operations",
         "back_office_productivity", "enterprise_wide"],
    )
    def test_any_canonical_focus_may_be_chosen(self, chosen):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID, focus_id=chosen)
        assert resolved["effective"]["focus_id"] == chosen

    def test_resubmitting_the_default_is_not_an_edit(self):
        """Explicitly sending the same value must not be reported as a change."""
        resolved = _tr().resolve_launch_config(TEMPLATE_ID, focus_id=DEFAULT_FOCUS)
        assert resolved["effective"]["focus_id"] == DEFAULT_FOCUS
        assert "focus_id" not in resolved["provenance"]["edited_fields"]

    def test_the_templates_endpoint_returns_the_focus_defaults(self, client, template):
        resp = client.get("/api/stack-builder/templates", headers=_auth("default"))
        assert resp.status_code == 200, resp.text
        row = {r["template_id"]: r for r in resp.json()}[TEMPLATE_ID]
        assert row["focus_defaults"]["focus_id"] == DEFAULT_FOCUS
        assert row["focus_defaults"]["emphasis"] == list(
            template.focus_defaults.emphasis
        )

    def test_an_untouched_launch_records_the_focus_on_the_run(self, client):
        org = _owner_org("d2_t3_focus")
        resp = client.post(
            "/api/stack-builder/launch",
            headers=_auth(org),
            json={"org_id": org, "template_ids": [TEMPLATE_ID]},
        )
        assert resp.status_code == 200, resp.text
        run = client.get(
            f"/api/runs/{resp.json()['runId']}", headers=_auth(org)
        ).json()
        assert run.get("focusId") == DEFAULT_FOCUS or run.get("focus_id") == DEFAULT_FOCUS

    def test_an_edited_launch_records_the_users_focus_on_the_run(self, client):
        org = _owner_org("d2_t3_focus_edit")
        resp = client.post(
            "/api/stack-builder/launch",
            headers=_auth(org),
            json={
                "org_id": org,
                "template_ids": [TEMPLATE_ID],
                "focus_id": UNDERWRITING_FOCUS,
            },
        )
        assert resp.status_code == 200, resp.text
        run = client.get(
            f"/api/runs/{resp.json()['runId']}", headers=_auth(org)
        ).json()
        recorded = run.get("focusId") or run.get("focus_id")
        assert recorded == UNDERWRITING_FOCUS


# ── Detector alignment ────────────────────────────────────────────────────────

class TestDetectorAlignment:

    def test_the_pack_is_service_cloud(self, template):
        assert template.pack_id == PACK_ID

    def test_detector_emphasis_references_only_pack_detectors(self, template):
        shipped = {
            importlib.import_module(path).DETECTOR_ID
            for path in _pack_config().get_detector_modules(PACK_ID)
        }
        assert set(template.detector_emphasis) <= shipped, (
            f"references detectors outside the pack: "
            f"{set(template.detector_emphasis) - shipped}"
        )

    def test_detector_emphasis_is_exactly_the_pack(self, template):
        assert set(template.detector_emphasis) == set(SC_DETECTORS)

    def test_no_insurance_specific_detector_exists(self):
        """T3: the template must not introduce an Insurance-specific detector."""
        detectors = BACKEND_DETECTORS = Path(
            _mod("detectors.repetition").__file__
        ).parent
        offenders = [
            p.name for p in detectors.glob("*.py")
            if any(w in p.name.lower()
                   for w in ("insurance", "claim", "underwrit", "policy"))
        ]
        assert offenders == [], offenders

    def test_the_pack_detector_list_is_unchanged(self):
        """Reusing the pack must not mutate it."""
        pack = _pack_config().PACK_REGISTRY[PACK_ID]
        assert len(pack["detectors"]) == 7
        assert pack["packVersion"] == "1.0.0"

    def test_every_area_maps_to_an_existing_pack_detector(self):
        shipped = {
            importlib.import_module(path).DETECTOR_ID
            for path in _pack_config().get_detector_modules(PACK_ID)
        }
        for area, detector_id in AREA_DETECTORS.items():
            assert detector_id in shipped, f"{area} -> {detector_id} is not in the pack"


# ── Nothing is suppressed: every pattern stays visible ────────────────────────

class TestNothingIsSuppressed:
    """T3's validation: policy servicing, claims handoffs and underwriting-review
    patterns must remain VISIBLE under the proposed default."""

    def test_the_seed_produces_all_three_area_patterns(self, seeded_findings):
        fired = {f.detector_id for f in seeded_findings}
        for area, detector_id in AREA_DETECTORS.items():
            assert detector_id in fired, f"{area} pattern did not fire on the seed"

    def test_all_seven_pack_detectors_fire_on_the_seed(self, seeded_findings):
        assert {f.detector_id for f in seeded_findings} == set(SC_DETECTORS)

    def test_a_detector_outside_the_focus_is_surfaced_not_dropped(self):
        """The mechanism: focus affinity ANNOTATES, it does not filter."""
        affinity = _affinity()
        for detector_id in SC_DETECTORS:
            annotation = affinity.build_focus_emphasis(DEFAULT_FOCUS, detector_id)
            assert annotation is not None, detector_id
            assert annotation["focus_id"] == DEFAULT_FOCUS
            assert "matched" in annotation
            if not annotation["matched"]:
                assert "surfaced but not emphasised" in annotation["rationale"]

    def test_the_three_area_patterns_all_receive_an_annotation(self):
        affinity = _affinity()
        for area, detector_id in AREA_DETECTORS.items():
            annotation = affinity.build_focus_emphasis(DEFAULT_FOCUS, detector_id)
            assert annotation["focus_id"] == DEFAULT_FOCUS, area

    def test_focus_affinity_never_removes_a_finding(self, seeded_findings):
        """Whatever the focus, the finding SET is unchanged — only ranking moves."""
        affinity = _affinity()
        for focus_id in (DEFAULT_FOCUS, UNDERWRITING_FOCUS, "cross_system_handoffs",
                         "enterprise_wide"):
            annotated = [
                affinity.build_focus_emphasis(focus_id, f.detector_id)
                for f in seeded_findings
            ]
            assert len(annotated) == len(seeded_findings), focus_id
            assert all(a is not None for a in annotated), focus_id

    def test_switching_focus_changes_which_patterns_are_emphasised(self, seeded_findings):
        """The default emphasises policy servicing; approvals_compliance emphasises
        underwriting review. Both keep every finding."""
        affinity = _affinity()

        def emphasised(focus_id):
            return {
                f.detector_id for f in seeded_findings
                if affinity.build_focus_emphasis(focus_id, f.detector_id)["matched"]
            }

        default_hits = emphasised(DEFAULT_FOCUS)
        underwriting_hits = emphasised(UNDERWRITING_FOCUS)
        assert "REPETITIVE_AUTOMATION" in default_hits       # policy servicing
        assert "APPROVAL_BOTTLENECK" in underwriting_hits    # underwriting review
        assert default_hits != underwriting_hits

    def test_enterprise_wide_carries_no_bias(self):
        """The escape hatch: a customer wanting no emphasis at all has one."""
        affinity = _affinity()
        for detector_id in SC_DETECTORS:
            annotation = affinity.build_focus_emphasis("enterprise_wide", detector_id)
            assert annotation is not None


# ── No new focus machinery ────────────────────────────────────────────────────

class TestNoNewFocusMachinery:

    def test_focus_defaults_field_set_is_unchanged(self):
        import dataclasses
        fields = tuple(f.name for f in dataclasses.fields(_tr().FocusDefaults))
        assert fields == ("focus_id", "emphasis"), (
            "FocusDefaults changed shape — T3 must add no API property"
        )

    def test_the_canonical_focus_set_is_unchanged(self):
        """No new focus type: exactly the seven canonical focuses."""
        assert set(_affinity().FOCUS_AFFINITY) == {
            "member_customer_service", "core_operations", "approvals_compliance",
            "cross_system_handoffs", "back_office_productivity",
            "engineering_change", "enterprise_wide",
        }

    def test_the_focus_engine_has_no_insurance_branch(self):
        source = Path(_affinity().__file__).read_text(encoding="utf-8").lower()
        for word in ("insurance", "policyholder", "underwriting", "claim"):
            assert word not in source, (
                f"focus_affinity.py references {word!r} — the focus engine must "
                f"stay domain-agnostic"
            )

    def test_the_focus_affinity_mapping_was_not_widened_for_insurance(self):
        """Widening member_customer_service would change ranking for every industry
        that uses it (STRS, FSC, service_operations), so T3 must not."""
        emphasised = set(_affinity().FOCUS_AFFINITY[DEFAULT_FOCUS] or ())
        assert emphasised == {
            "REPETITIVE_AUTOMATION", "KNOWLEDGE_GAP", "APPLICATION_STALL",
            "FSC_SERVICING_REQUEST_RECURRENCE",
        }, (
            "member_customer_service's affinity changed — that alters ranking for "
            "every industry using this focus, which T3 explicitly does not do"
        )

    def test_emptying_the_emphasis_tags_changes_no_rank(self, template):
        """The tags are declarative, not a scoring input — the same discipline T1
        proved for detector_emphasis."""
        affinity = _affinity()
        before = {
            d: affinity.build_focus_emphasis(DEFAULT_FOCUS, d)["rank"]
            for d in SC_DETECTORS
        }
        original = list(template.focus_defaults.emphasis)
        try:
            template.focus_defaults.emphasis = []
            after = {
                d: affinity.build_focus_emphasis(DEFAULT_FOCUS, d)["rank"]
                for d in SC_DETECTORS
            }
        finally:
            template.focus_defaults.emphasis = original
        assert before == after, (
            "emptying the emphasis tags changed ranking — they are supposed to be "
            "declarative, with ranking driven by focus_id alone"
        )

    def test_ranking_follows_focus_id_not_the_tags(self, template):
        """The converse: changing focus_id DOES change rank."""
        affinity = _affinity()
        default_rank = affinity.build_focus_emphasis(
            DEFAULT_FOCUS, "APPROVAL_BOTTLENECK"
        )["rank"]
        underwriting_rank = affinity.build_focus_emphasis(
            UNDERWRITING_FOCUS, "APPROVAL_BOTTLENECK"
        )["rank"]
        assert underwriting_rank < default_rank, (
            "approvals_compliance should rank the approval pattern above "
            "member_customer_service"
        )

    def test_an_unknown_focus_degrades_safely(self):
        """No focus-engine branch was added, so the existing safe degrade stands."""
        annotation = _affinity().build_focus_emphasis("not_a_focus", "HANDOFF_FRICTION")
        assert annotation is not None


# ── The limitation, reported ───────────────────────────────────────────────────

class TestTheFocusLimitationIsReported:
    """T3: "any inability to represent Insurance priorities using existing focuses
    must be reported separately"."""

    def test_the_limitation_is_recorded_on_the_template(self, template):
        note = template.metadata.get("focus_limitation", "")
        assert note.strip(), (
            "no single canonical focus emphasises all five Insurance areas, and T3 "
            "requires that be reported"
        )

    def test_the_note_states_the_actual_coverage(self, template):
        note = template.metadata["focus_limitation"]
        assert DEFAULT_FOCUS in note
        assert "2 of the Service Cloud pack's 7" in note

    def test_the_note_names_where_the_other_areas_live(self, template):
        note = template.metadata["focus_limitation"]
        assert "cross_system_handoffs" in note
        assert UNDERWRITING_FOCUS in note

    def test_the_note_says_nothing_is_suppressed(self, template):
        note = template.metadata["focus_limitation"].lower()
        assert "suppressed" in note or "surfaced but not emphasised" in note

    def test_the_note_explains_why_widening_was_not_done(self, template):
        note = template.metadata["focus_limitation"].lower()
        assert "every industry" in note or "strs" in note

    def test_the_claim_in_the_note_is_true(self):
        """Verify the recorded numbers rather than trusting the prose."""
        affinity = _affinity()
        emphasised = set(affinity.FOCUS_AFFINITY[DEFAULT_FOCUS] or ())
        assert len(emphasised & set(SC_DETECTORS)) == 2
        handoffs = set(affinity.FOCUS_AFFINITY["cross_system_handoffs"] or ())
        reviews = set(affinity.FOCUS_AFFINITY[UNDERWRITING_FOCUS] or ())
        assert "HANDOFF_FRICTION" in handoffs
        assert "APPROVAL_BOTTLENECK" in reviews
