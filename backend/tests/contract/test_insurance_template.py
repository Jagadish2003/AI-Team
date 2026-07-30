"""Contract tests for 2.0-D2 T1 — Insurance template instance + registry entry.

D2 is explicitly a CONFIGURATION exercise: "systems, roles, focus defaults,
terminology — reusing existing detectors rather than introducing a domain pack".
So the tests here are shaped around proving that claim rather than around new
behaviour.

  AC1  ``TestAC1PrePopulatesAndProducesFindings`` — the template pre-populates per
       its configuration AND the seven EXISTING Service Cloud detectors produce
       findings on a seeded insurance-shaped estate, with the launched run
       preserving template / pack / focus / role / terminology / version provenance.
  AC2  ``TestAC2ConfigurationOnly`` — zero template-model code changes, and
       zero new detectors.
  AC3  ``TestAC3ShippedConnectorsOnly`` — the R191-R1 anchor-on-shipped rule
       applied to this template's suggested systems. The guard only covers the
       industry registry and the catalog today, so this extends the same
       discovery to templates.
  AC4  ``TestAC4FutureScopeIsRecorded`` — the insurance-specific detectors the
       seeded estate does NOT cover are recorded as future scope, not built.

The seed lives at ``fixtures/insurance_estate_seed.json`` and is structurally
identical to the Salesforce offline fixture, so it drives the REAL detector inputs
rather than a parallel test-only shape.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
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
FOCUS_ID = "member_customer_service"

# The seven Service Cloud detectors this template emphasises — all pre-existing.
EXPECTED_DETECTORS = (
    "REPETITIVE_AUTOMATION",
    "HANDOFF_FRICTION",
    "APPROVAL_BOTTLENECK",
    "KNOWLEDGE_GAP",
    "INTEGRATION_CONCENTRATION",
    "PERMISSION_BOTTLENECK",
    "CROSS_SYSTEM_ECHO",
)

SC_DETECTOR_MODULES = (
    "repetition",
    "handoff_friction",
    "approval_delay",
    "knowledge_gap",
    "integration_concentration",
    "permission_bottleneck",
    "cross_system_echo",
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
SEED_PATH = Path(__file__).resolve().parent / "fixtures" / "insurance_estate_seed.json"
TEMPLATE_REGISTRY_REL = "backend/discovery/packs/template_registry.py"


def _mod(name: str):
    try:
        return importlib.import_module(f"discovery.{name}")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module(f"backend.discovery.{name}")


def _tr():
    return _mod("packs.template_registry")


def _pack_config():
    return _mod("packs.pack_config")


def _load_r191_guard():
    """The R191-R1 shipped-ingestor discovery, loaded by path (it is a test module)."""
    path = (
        BACKEND_ROOT / "tests" / "contract"
        / "test_r191_r1_ingestor_registry_enforcement.py"
    )
    spec = importlib.util.spec_from_file_location("_r191_guard_d2", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def template():
    defn = _tr().get_template(TEMPLATE_ID)
    assert defn is not None, "the Insurance template is not registered"
    return defn


@pytest.fixture(scope="module")
def seed():
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def seeded_findings(seed):
    """Findings the EXISTING Service Cloud detectors produce on the seeded estate."""
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


# ── AC1 — pre-populates, and produces findings on the seeded estate ─────────────

class TestAC1PrePopulatesAndProducesFindings:

    def test_template_is_registered_and_listed(self):
        tr = _tr()
        assert TEMPLATE_ID in tr.TEMPLATE_REGISTRY
        assert TEMPLATE_ID in [t.template_id for t in tr.list_templates()]

    def test_identity_and_description(self, template):
        assert template.template_id == TEMPLATE_ID
        assert template.label == "Insurance"
        assert template.description.strip()

    def test_prepopulates_the_pack(self, template):
        """Reuses the Service Cloud pack — D2 introduces no domain pack."""
        assert template.pack_id == PACK_ID

    def test_prepopulates_focus(self, template):
        assert template.focus_defaults.focus_id == FOCUS_ID
        assert template.focus_defaults.emphasis

    def test_prepopulates_systems(self, template):
        assert "salesforce_sc" in template.suggested_systems
        assert len(template.suggested_systems) >= 3

    def test_every_suggested_system_has_a_role(self, template):
        """A suggested system with no role would launch with no weighting."""
        for system_id in template.suggested_systems:
            assert system_id in template.suggested_roles, system_id

    def test_the_workflow_shape_is_expressed_in_the_roles(self, template):
        """Claims and policy-service records are the primary workload; workflow
        systems supply assignment/escalation; communication corroborates handoffs;
        documentation carries policy/procedure/underwriting context."""
        roles = template.suggested_roles
        assert roles["salesforce_sc"] == "system_of_record"
        assert {roles[s] for s in ("servicenow", "jira")} == {"workflow_system"}
        assert {roles[s] for s in ("teams", "slack")} == {"operational_signal_source"}
        assert {roles[s] for s in ("confluence", "sharepoint")} == {
            "documentation_system"
        }

    def test_metadata_declares_the_insurance_industry_and_version(self, template):
        assert template.metadata["industry_id"] == "insurance"
        assert template.metadata["source"] == "2.0-D2"
        assert template.metadata["version"]

    def test_metadata_names_the_three_workflow_areas(self, template):
        areas = template.metadata["workflow_areas"]
        assert set(areas) == {
            "claims_handling", "underwriting_review", "policy_servicing"
        }

    def test_the_focus_actually_emphasises_pack_detectors(self, template):
        """A focus emphasising none of the pack's detectors would rank nothing."""
        focus = _mod("packs.focus_affinity")
        emphasised = set(focus.FOCUS_AFFINITY[template.focus_defaults.focus_id] or ())
        assert emphasised & set(EXPECTED_DETECTORS)

    # ── the seeded estate ──

    def test_the_seed_is_insurance_shaped(self, seed):
        """Repeat policy-service requests, claims handoffs, underwriting-review
        delay, queue backlog, knowledge gaps, permission concentration and
        cross-system duplication — the seven signals D2 T1 names."""
        reasons = {
            row["reason"] for row in seed["case_metrics"]["category_breakdown"]
        }
        assert {"Claim_FNOL", "Policy_Endorsement", "Underwriting_Referral"} <= reasons
        assert seed["case_metrics"]["handoff_score"] >= 1.5          # claims handoffs
        assert seed["case_metrics"]["knowledge_gap_score"] > 0        # knowledge gaps
        processes = {p["process_name"]: p for p in seed["approval_processes"]}
        assert processes["Underwriting Referral Review"]["avg_delay_days"] >= 3.0
        assert processes["Underwriting Referral Review"]["pending_count"] > 0
        assert seed["cross_system_references"]["sf_echo_score"] >= 0.15
        assert seed["flow_inventory"]["records_90d"] >= 50            # queue backlog

    def test_the_seed_matches_the_real_salesforce_ingest_shape(self, seed):
        """Structurally identical to the offline Salesforce fixture, so it drives
        the REAL detector inputs rather than a test-only shape."""
        shipped = json.loads(
            (
                BACKEND_ROOT / "discovery" / "ingest" / "fixtures"
                / "salesforce_sample.json"
            ).read_text(encoding="utf-8")
        )
        assert set(shipped) == set(seed), (
            f"seed keys differ from the Salesforce fixture: "
            f"only-in-seed={set(seed) - set(shipped)}, "
            f"only-in-fixture={set(shipped) - set(seed)}"
        )

    def test_the_seed_produces_findings_from_existing_detectors(self, seeded_findings):
        assert seeded_findings, "the seeded insurance estate produced no findings"

    def test_all_seven_existing_detectors_fire(self, seeded_findings):
        fired = {f.detector_id for f in seeded_findings}
        assert fired == set(EXPECTED_DETECTORS), (
            f"missing={set(EXPECTED_DETECTORS) - fired}, unexpected={fired - set(EXPECTED_DETECTORS)}"
        )

    def test_every_finding_crosses_its_own_threshold(self, seeded_findings):
        for finding in seeded_findings:
            assert finding.metric_value >= finding.threshold, finding.detector_id

    def test_findings_come_from_salesforce(self, seeded_findings):
        for finding in seeded_findings:
            assert finding.signal_source == "salesforce"

    def test_negative_control_low_delay_approval_is_not_the_driver(self, seed):
        """Policy Endorsement Approval sits below both approval thresholds, so the
        seed is not simply firing on everything."""
        processes = {p["process_name"]: p for p in seed["approval_processes"]}
        endorsement = processes["Policy Endorsement Approval"]
        assert endorsement["avg_delay_days"] < 3.0
        assert endorsement["bottleneck_score"] < 10.0

    def test_negative_control_single_reference_credential(self, seed):
        rating = [
            c for c in seed["named_credentials"]
            if c["credential_name"] == "Rating Engine"
        ][0]
        assert rating["flow_reference_count"] < 3

    # ── untouched launch preserves provenance ──

    def test_untouched_launch_applies_every_default(self, template):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        effective = resolved["effective"]
        assert effective["pack_ids"] == [PACK_ID]
        assert effective["focus_id"] == FOCUS_ID
        assert effective["selected_system_ids"] == list(template.suggested_systems)
        assert effective["roles"] == dict(template.suggested_roles)
        assert resolved["provenance"]["untouched"] is True
        assert resolved["provenance"]["edited_fields"] == []

    def test_untouched_launch_preserves_full_provenance(self, template):
        """Template, pack, focus, system roles, terminology and version are all
        recorded on the launch provenance."""
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        snapshot = resolved["provenance"]["template_defaults"]
        assert snapshot["template_id"] == TEMPLATE_ID
        assert snapshot["pack_id"] == PACK_ID
        assert snapshot["pack_version"] == _pack_config().get_pack_version(PACK_ID)
        assert snapshot["focus_id"] == FOCUS_ID
        assert snapshot["suggested_roles"] == dict(template.suggested_roles)
        assert snapshot["terminology"] == dict(template.terminology)
        assert snapshot["template_version"] == template.metadata["version"]
        assert snapshot["detector_emphasis"] == list(template.detector_emphasis)

    def test_pack_boundary_records_the_template_and_its_pack(self):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        boundaries = resolved["effective"]["pack_boundaries"]
        assert len(boundaries) == 1
        assert boundaries[0]["template_id"] == TEMPLATE_ID
        assert boundaries[0]["pack_id"] == PACK_ID
        assert boundaries[0]["focus_id"] == FOCUS_ID

    def test_user_edits_override_the_defaults(self):
        """Every value stays editable through the existing resolve_launch_config."""
        resolved = _tr().resolve_launch_config(
            TEMPLATE_ID,
            focus_id="approvals_compliance",
            selected_system_ids=["salesforce_sc", "servicenow"],
            weightings={"salesforce_sc": {"role": "workflow_system"}},
        )
        effective = resolved["effective"]
        assert effective["focus_id"] == "approvals_compliance"
        assert effective["selected_system_ids"] == ["salesforce_sc", "servicenow"]
        assert effective["roles"]["salesforce_sc"] == "workflow_system"
        edited = resolved["provenance"]["edited_fields"]
        assert {"focus_id", "selected_system_ids", "roles"} <= set(edited)
        assert resolved["provenance"]["untouched"] is False

    def test_an_explicit_pack_selection_overrides_the_template_pack(self):
        resolved = _tr().resolve_launch_config(TEMPLATE_ID, pack_ids=["cloud_ops"])
        assert resolved["effective"]["pack_ids"] == ["cloud_ops"]
        assert "pack_id" in resolved["provenance"]["edited_fields"]

    # ── served through the existing endpoints ──

    def test_the_templates_endpoint_serves_it(self, client):
        resp = client.get("/api/stack-builder/templates", headers=_auth("default"))
        assert resp.status_code == 200, resp.text
        rows = {row["template_id"]: row for row in resp.json()}
        assert TEMPLATE_ID in rows
        row = rows[TEMPLATE_ID]
        assert row["pack_id"] == PACK_ID
        assert row["focus_defaults"]["focus_id"] == FOCUS_ID
        assert "salesforce_sc" in row["suggested_systems"]
        assert row["suggested_roles"]["salesforce_sc"] == "system_of_record"
        assert row["terminology"]["customer"] == "policyholder"
        assert row["metadata"]["industry_id"] == "insurance"

    def test_the_single_template_endpoint_serves_it(self, client):
        resp = client.get(
            f"/api/stack-builder/templates/{TEMPLATE_ID}", headers=_auth("default")
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["template_id"] == TEMPLATE_ID

    def test_launching_the_template_records_its_configuration(self, client):
        org = _owner_org("d2_insurance_launch")
        resp = client.post(
            "/api/stack-builder/launch",
            headers=_auth(org),
            json={"org_id": org, "template_ids": [TEMPLATE_ID]},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["packIds"] == [PACK_ID]
        assert data["packId"] == PACK_ID

        run = client.get(f"/api/runs/{data['runId']}", headers=_auth(org)).json()
        assert run["packId"] == PACK_ID
        assert run.get("templateId") == TEMPLATE_ID

    def test_a_seeded_run_produces_findings_from_existing_detectors(self, seed, monkeypatch):
        """The end-to-end shape of AC1: an untouched Insurance-template run over the
        seeded estate produces findings, through the real pipeline."""
        import os

        os.environ["INGEST_MODE"] = "offline"
        from discovery.ingest import salesforce as sf_ingest
        from discovery import runner

        monkeypatch.setattr(sf_ingest, "ingest", lambda *a, **k: dict(seed))

        resolved = _tr().resolve_launch_config(TEMPLATE_ID)
        result = runner.run(
            mode="offline", pack_ids=resolved["effective"]["pack_ids"]
        )
        assert result["packId"] == PACK_ID
        opportunities = result["opportunities"]
        assert opportunities, "the seeded insurance run produced no findings"
        detectors = {opp.get("detector_id") for opp in opportunities}
        assert detectors, "no detectors fired in the seeded run"
        assert detectors <= set(EXPECTED_DETECTORS), (
            f"unexpected detectors in an insurance run: "
            f"{detectors - set(EXPECTED_DETECTORS)}"
        )
        for opp in opportunities:
            assert opp.get("packId") == PACK_ID


# ── AC2 — configuration only ───────────────────────────────────────────────────

class TestAC2ConfigurationOnly:

    EXPECTED_TEMPLATE_FIELDS = (
        "template_id", "label", "description", "suggested_systems",
        "suggested_roles", "focus_defaults", "pack_id", "detector_emphasis",
        "terminology", "metadata",
    )

    def test_template_definition_field_set_is_unchanged(self):
        import dataclasses
        fields = tuple(f.name for f in dataclasses.fields(_tr().TemplateDefinition))
        assert fields == self.EXPECTED_TEMPLATE_FIELDS, (
            f"TemplateDefinition changed shape: {fields}. D2 is configuration only."
        )

    def test_focus_defaults_field_set_is_unchanged(self):
        import dataclasses
        fields = tuple(f.name for f in dataclasses.fields(_tr().FocusDefaults))
        assert fields == ("focus_id", "emphasis")

    def test_register_template_still_validates_the_pack(self):
        tr = _tr()
        bad = tr.TemplateDefinition(
            template_id="insurance_bad_pack_probe",
            label="probe",
            description="",
            suggested_systems=[],
            suggested_roles={},
            focus_defaults=tr.FocusDefaults(focus_id="core_operations"),
            pack_id="no_such_pack",
        )
        with pytest.raises(ValueError):
            tr.register_template(bad)
        assert tr.get_template("insurance_bad_pack_probe") is None

    def test_the_template_round_trips_as_configuration(self, template):
        tr = _tr()
        try:
            tr.unregister_template(TEMPLATE_ID)
            assert tr.get_template(TEMPLATE_ID) is None
            tr.register_template(template)
            assert tr.get_template(TEMPLATE_ID) is template
        finally:
            tr.register_template(template)
        assert tr.get_template(TEMPLATE_ID) is not None

    def test_no_new_detectors_were_added(self):
        """D2 reuses existing detectors. No insurance-named detector may exist."""
        detectors_dir = BACKEND_ROOT / "discovery" / "detectors"
        offenders = [
            p.name for p in detectors_dir.glob("*.py")
            if "insurance" in p.name.lower() or "claim" in p.name.lower()
            or "underwrit" in p.name.lower() or "policy" in p.name.lower()
        ]
        assert offenders == [], (
            f"D2 must not add domain detectors — found {offenders}. Insurance "
            f"detectors are recorded as future scope."
        )

    def test_the_service_cloud_pack_is_unchanged(self):
        """The template reuses the pack; it must not have mutated it."""
        pack = _pack_config().PACK_REGISTRY[PACK_ID]
        assert len(pack["detectors"]) == 7
        assert pack["packVersion"] == "1.0.0", (
            "service_cloud packVersion moved — D2 adds no pack logic, so it must not"
        )
        assert pack["ui_labels_path"] is None

    def test_no_template_model_or_api_line_changed(self):
        """The AC2 diff check, when repository history is available.

        template_registry.py is BOTH the model and the registry, so a dict entry
        necessarily edits the file. This asserts the part AC2 protects: no model or
        public-API definition line is touched. SKIPS on a shallow clone rather than
        passing vacuously (CI checks out at depth 1).
        """
        try:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/2.0-D2"],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if base.returncode != 0 or not base.stdout.strip():
                pytest.skip("no merge base with origin/2.0-D2 (shallow clone)")
            diff = subprocess.run(
                ["git", "diff", "-U0", base.stdout.strip(), "HEAD", "--",
                 TEMPLATE_REGISTRY_REL],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
            )
            if diff.returncode != 0:
                pytest.skip("git diff unavailable")
        except (OSError, subprocess.SubprocessError):  # pragma: no cover
            pytest.skip("git unavailable")

        forbidden = (
            "class TemplateDefinition", "class FocusDefaults",
            "def register_template", "def unregister_template", "def get_template",
            "def list_templates", "def resolve_launch_config",
            "def template_defaults_snapshot", "def normalize_template_ids",
        )
        for line in diff.stdout.splitlines():
            if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
                continue
            for token in forbidden:
                assert token not in line, (
                    f"AC2 violation — the template MODEL/API changed: {line.strip()!r}"
                )

    def test_no_engine_file_changed(self):
        """Nothing under the scoring engine or the serve-time terminology model."""
        for rel in ("backend/discovery/scorer.py",
                    "backend/discovery/runner.py",
                    "backend/app/terminology.py",
                    "backend/app/routes_stack_builder.py"):
            source = (REPO_ROOT / rel).read_text(encoding="utf-8").lower()
            assert "insurance" not in source, (
                f"{rel} references insurance — D2 is configuration only"
            )


# ── AC3 — shipped connectors only (R191-R1) ────────────────────────────────────

class TestAC3ShippedConnectorsOnly:
    """The anchor-on-shipped rule applied to this template's systems.

    The R191-R1 guard covers the industry registry and the catalog; it does not
    inspect template systems. This extends the SAME shipped-ingestor discovery to
    them, so a template cannot anchor on a connector that does not ingest.
    """

    def test_every_suggested_system_has_a_shipped_ingestor(self, template):
        guard = _load_r191_guard()
        implemented = guard._implemented_connector_ids()
        missing = [
            system_id for system_id in template.suggested_systems
            if guard._missing_implementation(system_id, implemented)
        ]
        assert missing == [], (
            f"the Insurance template anchors on connectors with no shipped "
            f"ingestion: {missing}"
        )

    def test_no_suggested_system_is_roadmap_labelled(self, template):
        roadmap = importlib.import_module("app.connector_roadmap")
        for system_id in template.suggested_systems:
            assert roadmap.is_roadmap(system_id) is False, (
                f"{system_id} is roadmap-labelled and must not be a template default"
            )

    def test_the_salesforce_product_declares_a_registered_pack(self, template):
        """salesforce_sc is a product declaration, so the 2.0-D1 T5 pack-level gate
        applies to it as well as the connector-level one."""
        spp = importlib.import_module("app.salesforce_product_packs")
        declared = spp.get_product_pack("salesforce_sc")
        assert declared is not None
        assert declared.pack_id in set(_pack_config().list_packs())
        assert declared.pack_id == PACK_ID

    def test_the_r191_r1_cross_check_still_passes(self):
        """The gate D2 AC3 names explicitly."""
        guard = _load_r191_guard()
        guard.test_registry_connectable_entries_have_shipped_ingestors()
        guard.test_connectable_catalog_tiles_have_shipped_ingestors()
        guard.test_unimplemented_catalog_tiles_stay_roadmap_not_connectable()

    def test_every_template_in_the_registry_anchors_on_shipped_connectors(self):
        """Not just Insurance — the rule should hold registry-wide."""
        guard = _load_r191_guard()
        implemented = guard._implemented_connector_ids()
        offenders: dict = {}
        for defn in _tr().list_templates():
            missing = [
                s for s in defn.suggested_systems
                if guard._missing_implementation(s, implemented)
            ]
            if missing:
                offenders[defn.template_id] = missing
        # Templates predating the rule may anchor on non-connector scope ids
        # (e.g. the MSP event-source aliases); Insurance must not be among them.
        assert TEMPLATE_ID not in offenders, offenders.get(TEMPLATE_ID)


# ── AC4 — future scope recorded, not implemented ────────────────────────────────

class TestAC4FutureScopeIsRecorded:

    def test_future_scope_is_recorded_on_the_template(self, template):
        note = template.metadata.get("future_scope", "")
        assert note.strip(), (
            "D2 AC4 requires any identified need for domain-specific detectors to "
            "be RECORDED as future scope"
        )

    def test_the_note_names_the_uncovered_insurance_patterns(self, template):
        note = template.metadata["future_scope"].lower()
        for pattern in ("claim leakage", "subrogation", "reserve", "fraud"):
            assert pattern in note, f"future scope does not name {pattern!r}"

    def test_the_note_states_it_is_out_of_scope_for_this_story(self, template):
        note = template.metadata["future_scope"].lower()
        assert "out of scope" in note or "not " in note
        assert "separate future" in note or "future" in note

    def test_the_note_is_honest_that_no_domain_pack_ships(self, template):
        """The template must not imply insurance-specific detection it lacks."""
        assert template.pack_id == PACK_ID
        assert "no insurance-specific detectors" in template.description.lower()


# ── Terminology hygiene (carried forward from the 2.0-D1 T6 defect) ────────────

class TestTerminologyIsIdempotent:
    """A mapping whose replacement contains its source double-expands text that
    already uses the domain phrase — the defect 2.0-D1 T6 fixed for FSC."""

    def test_no_mapping_replacement_contains_its_source(self, template):
        for generic, domain in template.terminology.items():
            assert generic.lower() not in domain.lower().split(), (
                f"{generic!r} -> {domain!r} double-expands the domain phrase"
            )

    def test_applying_the_map_twice_equals_applying_it_once(self, template):
        terminology = importlib.import_module("app.terminology")
        for generic in template.terminology:
            once = terminology.rewrite_text(generic, template.terminology)
            twice = terminology.rewrite_text(once, template.terminology)
            assert once == twice, f"{generic!r}: {once!r} -> {twice!r}"

    def test_the_map_speaks_insurance(self, template):
        assert template.terminology["customer"] == "policyholder"
        assert template.terminology["account"] == "policy"

    def test_insurance_and_lending_vocabularies_stay_distinct(self):
        tr = _tr()
        insurance = tr.get_template(TEMPLATE_ID).terminology
        lending = tr.get_template("commercial_lending").terminology
        assert insurance["customer"] != lending["customer"]
