"""
2.0-D4 T3 — the per-run reproducibility record (D4-AC4).

AC4: "Each run records pack/connector/policy versions and AI mode sufficient for
reproducibility explanation."

The bar is **explanation, not byte-identical replay**, and the suite is written to
that bar deliberately. Replay here re-serves persisted artifacts and does not re-run
ingestion or regenerate LLM output; this record does not change that. What it must
support is a support engineer, six months later, pointing at exactly which of pack
version / connector version / assembly policy / AI mode differs and saying "that is
why" — which is what ``TestTwoRunComparison`` exercises.

The tests are weighted towards the two ways this feature fails quietly:

  * a field that never changes (a connector "version" always reading ``1.0.0``),
    which is worse than an absent field because it invites false confidence; and
  * a field that claims to know something it does not (an invented assembly-policy
    version while 2.0-B3 has not landed).
"""
from __future__ import annotations

import ast
import copy
import json
import pathlib
import re

import pytest

from app import run_reproducibility as rr

BACKEND = pathlib.Path(rr.__file__).resolve().parents[1]


def _record(**over):
    run = {
        "packIds": ["service_cloud", "cloud_ops"],
        "packVersions": {"service_cloud": "1.4.0", "cloud_ops": "1.0.0"},
    }
    run.update(over.pop("run", {}))
    return rr.build_reproducibility_record(
        run,
        org_id=over.pop("org_id", "default"),
        connector_ids=over.pop("connector_ids", ["salesforce", "servicenow", "jira"]),
    )


# ── the record shape ──────────────────────────────────────────────────────────


class TestRecordShape:

    def test_all_four_dimensions_are_present(self):
        record = _record()
        assert set(record) == {
            "record_version", "packs", "connectors", "assembly_policy", "ai_mode"
        }

    def test_the_record_is_json_serialisable(self):
        """It is stamped onto the run record, which is stored as JSON."""
        json.dumps(_record())

    def test_the_record_version_is_declared(self):
        assert rr.REPRODUCIBILITY_RECORD_VERSION
        assert _record()["record_version"] == rr.REPRODUCIBILITY_RECORD_VERSION

    def test_building_a_record_never_raises_on_a_degenerate_run(self):
        """A version record exists to explain a run; it must never be the reason one
        fails."""
        for run in (None, {}, {"packIds": None}, {"packVersions": "nonsense"}):
            record = rr.build_reproducibility_record(run, org_id=None, connector_ids=None)
            assert set(record) >= {"packs", "connectors", "assembly_policy", "ai_mode"}


# ── packs: read the existing stamp, do not rebuild it ─────────────────────────


class TestPackDimension:

    def test_the_pack_versions_come_from_the_run_record(self):
        packs = _record()["packs"]
        assert packs["pack_ids"] == ["service_cloud", "cloud_ops"]
        assert packs["pack_versions"]["service_cloud"] == "1.4.0"
        assert packs["packs_missing_version"] == []

    def test_a_pack_without_a_version_is_surfaced_not_hidden(self):
        record = rr.build_reproducibility_record(
            {"packIds": ["service_cloud", "mystery"], "packVersions": {"service_cloud": "1.4.0"}},
            org_id="default", connector_ids=[],
        )
        assert record["packs"]["packs_missing_version"] == ["mystery"]

    def test_the_singular_scalars_still_yield_a_record(self):
        """R191-P1 keeps packId/packVersion mirroring the primary pack, so a
        pre-multi-pack run must not produce an empty pack dimension."""
        record = rr.build_reproducibility_record(
            {"packId": "ncino", "packVersion": "2.1.0"}, org_id="default", connector_ids=[]
        )
        assert record["packs"]["pack_ids"] == ["ncino"]
        assert record["packs"]["pack_versions"] == {"ncino": "2.1.0"}

    def test_the_module_does_not_recompute_pack_versions(self):
        """Structural: the pack half must READ the run's stamp. Recomputing would
        create a second source of truth that can disagree with the findings' own
        packId/packVersion stamps."""
        source = pathlib.Path(rr.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "pack_record"
        )
        called = {
            n.func.id for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        } | {
            n.func.attr for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "get_pack_version" not in called
        assert "get_pack" not in called

    def test_every_registered_pack_declares_a_version(self):
        """The 1.9 discipline that makes the stamp meaningful rather than decorative:
        bump packVersion whenever a pack's detector, scorer or corroboration logic
        changes. Verified, not assumed."""
        from discovery.packs.pack_config import PACK_REGISTRY

        missing = [
            pack_id for pack_id, entry in PACK_REGISTRY.items()
            if not str(entry.get("packVersion") or "").strip()
        ]
        assert not missing, f"packs with no declared packVersion: {missing}"

    def test_declared_pack_versions_look_like_versions(self):
        from discovery.packs.pack_config import PACK_REGISTRY

        for pack_id, entry in PACK_REGISTRY.items():
            version = str(entry.get("packVersion"))
            assert re.match(r"^\d+\.\d+\.\d+$", version), f"{pack_id}: {version!r}"


# ── AI mode: BOTH providers, independently ────────────────────────────────────


class TestAiModeDimension:

    def test_both_providers_are_recorded_separately(self):
        """The shipped configuration deliberately mixes them (embeddings via
        customer_tenant, generation via hosted), so one collapsed 'AI mode' field
        would be wrong for the default deployment."""
        ai = _record()["ai_mode"]
        assert "generation_provider" in ai
        assert "embedding_provider" in ai

    def test_there_is_no_single_collapsed_ai_mode_field(self):
        ai = _record()["ai_mode"]
        assert "ai_mode" not in ai
        assert "provider" not in ai

    def test_the_providers_reflect_configuration(self, monkeypatch):
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "in_boundary")
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "customer_tenant")
        ai = rr.ai_mode_record()
        assert ai["generation_provider"] == "in_boundary"
        assert ai["embedding_provider"] == "customer_tenant"

    def test_changing_one_provider_does_not_change_the_other(self, monkeypatch):
        """They resolve independently — the property T2-AC3 established."""
        monkeypatch.setenv("MODEL_GENERATION_PROVIDER", "in_boundary")
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
        ai = rr.ai_mode_record()
        assert (ai["generation_provider"], ai["embedding_provider"]) == (
            "in_boundary", "hosted"
        )

    def test_the_embedding_model_identity_is_recorded(self):
        """R18-B1 stamps (embedding_model, embedding_model_version) per vector so a
        deployment rename does not invalidate an index; the same identity is what
        makes an old run's embeddings comparable with a new one's."""
        ai = _record()["ai_mode"]
        assert "embedding_model" in ai
        assert "embedding_model_version" in ai

    def test_the_provider_names_come_from_the_gateway(self):
        """Structural: this module must not read the provider env vars itself — the
        gateway owns that configuration and a second reader is free to drift.

        Scans CODE, not prose: the module docstring legitimately NAMES both env vars
        while explaining why both providers are recorded, and a guard that flagged
        that would have to be weakened to pass — which would make it useless. Stripping
        docstrings is the same rule the D4 T1 audit sweep applies.
        """
        source = pathlib.Path(rr.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
        code = ast.unparse(tree)

        assert "MODEL_GENERATION_PROVIDER" not in code
        assert "MODEL_EMBEDDING_PROVIDER" not in code
        assert "os.getenv" not in code, "the gateway owns provider configuration"
        assert "resolve_provider_names" in code


# ── connectors: the definition, and the "never changes" failure mode ──────────


class TestConnectorDimension:

    def test_each_connector_records_the_three_things_that_vary(self):
        connectors = _record()["connectors"]["connectors"]
        assert set(connectors) == {"salesforce", "servicenow", "jira"}
        for entry in connectors.values():
            assert set(entry) >= {"ingest_module", "api_version", "code_fingerprint"}

    def test_the_api_version_is_the_real_negotiated_version(self):
        connectors = _record()["connectors"]["connectors"]
        assert connectors["salesforce"]["api_version"] == "v59.0"
        # ServiceNow and Jira name their constant differently; reading only
        # API_VERSION reported them as absent, which is the quiet-wrong-answer this
        # record exists to eliminate.
        assert connectors["servicenow"]["api_version"] == "v1"
        assert connectors["jira"]["api_version"] == "3"

    def test_every_declared_api_version_constant_is_discovered(self):
        """The list of constant names must keep up with the source tree.

        Connectors do not agree on a name (API_VERSION / JIRA_API_VERSION /
        SN_API_VERSION / the Azure per-surface ones). If one introduces a new name
        that is not in _API_VERSION_ATTRS, its version silently reads as absent — so
        this fails the build instead.
        """
        pattern = re.compile(
            r"^([A-Z][A-Z0-9_]*(?:API_VERSION|GRAPH_VERSION)[A-Z0-9_]*)\s*=\s*[\"']",
            re.M,
        )
        declared = set()
        for path in sorted((BACKEND / "discovery" / "ingest").rglob("*.py")):
            declared |= set(pattern.findall(path.read_text(encoding="utf-8")))
        unknown = sorted(declared - set(rr._API_VERSION_ATTRS))
        assert not unknown, (
            "these API-version constants exist in discovery/ingest but are not in "
            f"_API_VERSION_ATTRS, so they would read as absent: {unknown}"
        )

    def test_a_connector_that_pins_no_version_reports_none_not_a_default(self):
        """A fabricated version is worse than a missing one."""
        record = rr.connector_record("default", ["documents", "slack"])
        for entry in record["connectors"].values():
            assert entry["api_version"] is None

    def test_the_code_fingerprint_is_not_a_constant(self):
        """The whole point of the fingerprint: a version that never changes explains
        nothing. Two different ingestors must not share a fingerprint."""
        record = rr.connector_record("default", list(rr.CONNECTOR_INGEST_MODULES))
        fingerprints = {
            cid: e["code_fingerprint"] for cid, e in record["connectors"].items()
        }
        assert all(fingerprints.values()), fingerprints
        # distinct modules => distinct fingerprints (salesforce_fsc/ncino/salesforce
        # are separate modules with separate code)
        assert len(set(fingerprints.values())) == len(fingerprints)

    def test_the_fingerprint_tracks_the_source(self):
        """Change the source, change the fingerprint — the property that makes it
        impossible for this field to go stale."""
        assert rr._fingerprint("abc") != rr._fingerprint("abd")
        assert rr._fingerprint("abc") == rr._fingerprint("abc")

    def test_no_hardcoded_version_string_is_used_as_a_connector_version(self):
        """Structural guard against the failure mode: nothing in this module may
        assign a literal semver as a connector version."""
        source = pathlib.Path(rr.__file__).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not re.match(r"^\d+\.\d+\.\d+$", node.value), (
                    f"a literal version {node.value!r} in the reproducibility record "
                    "is the 'always reads 1.0.0' failure this design avoids"
                )

    def test_an_unmapped_connector_is_visibly_unmapped(self):
        record = rr.connector_record("default", ["salesforce", "not_a_connector"])
        assert record["unmapped"] == ["not_a_connector"]
        assert "not_a_connector" not in record["connectors"]

    def test_every_mapped_module_actually_imports(self):
        """A stale entry in CONNECTOR_INGEST_MODULES would report 'unavailable' for a
        connector that exists, which reads as a platform fault rather than a mapping
        error."""
        record = rr.connector_record("default", list(rr.CONNECTOR_INGEST_MODULES))
        broken = {
            cid: e.get("unavailable")
            for cid, e in record["connectors"].items()
            if e.get("unavailable")
        }
        assert not broken, broken

    def test_the_azure_connector_reports_its_per_surface_versions(self):
        """Reading only azure_events.py reported None even though the platform pins an
        api-version per ARM surface in the sibling client modules."""
        entry = rr.connector_record("default", ["azure_events"])["connectors"]["azure_events"]
        assert entry["api_version"]
        assert "ALERTS_API_VERSION" in entry["api_version"]


# ── assembly policy: honest about the absent version ─────────────────────────


class TestAssemblyPolicyDimension:

    def test_the_version_is_null_with_a_recorded_reason(self):
        """2.0-B3 has not landed. A record that claims to know something it does not
        is the failure this story exists to prevent, so the field is null and the
        reason travels WITH the record."""
        policy = _record()["assembly_policy"]
        assert policy["version"] is None
        assert "2.0-B3" in policy["version_absent_reason"]

    def test_no_placeholder_version_is_invented(self):
        policy = _record()["assembly_policy"]
        assert policy["version"] not in ("1.0.0", "0.0.0", "unknown", "")

    def test_the_effective_policy_values_are_recorded(self):
        """Null alone would be less useful than the truth available: the effective
        values are what actually change behaviour."""
        effective = _record()["assembly_policy"]["effective"]
        assert effective
        assert "confidence_floor" in effective
        assert "max_evidence_chunks" in effective

    def test_a_policy_change_changes_the_fingerprint(self):
        from app.context_assembly import AssemblyPolicy

        base = rr.assembly_policy_record(AssemblyPolicy())
        changed = rr.assembly_policy_record(AssemblyPolicy(confidence_floor=0.3))
        assert base["effective_fingerprint"] != changed["effective_fingerprint"]
        assert changed["effective"]["confidence_floor"] == 0.3

    def test_b3_landing_is_detectable(self):
        """When 2.0-B3 lands, AssemblyPolicy should gain a version and this test's
        premise becomes false — which is the signal to populate the field rather than
        leaving a stale 'not landed' reason in every run record."""
        from app.context_assembly import AssemblyPolicy

        assert not hasattr(AssemblyPolicy(), "policy_version"), (
            "AssemblyPolicy now declares a version — populate "
            "assembly_policy_record()['version'] and drop the absent-reason"
        )


# ── AC4's actual ask: explain what changed between two runs ──────────────────


class TestTwoRunComparison:
    """The deliverable AC4 is really asking for, and the shape a customer will want
    in the UI."""

    def test_identical_runs_report_no_difference(self):
        record = _record()
        result = rr.diff_reproducibility(record, copy.deepcopy(record))
        assert result["identical"] is True
        assert result["changes"] == []
        assert "explained by the DATA" in result["summary"]

    def test_a_pack_bump_is_attributed_to_the_pack_dimension(self):
        older = _record()
        newer = copy.deepcopy(older)
        newer["packs"]["pack_versions"]["service_cloud"] = "1.5.0"
        result = rr.diff_reproducibility(older, newer)
        assert result["identical"] is False
        assert result["dimensions_changed"] == ["pack versions"]
        change = result["changes"][0]
        assert change["before"] == "1.4.0" and change["after"] == "1.5.0"

    def test_an_api_version_change_is_attributed_to_connectors(self):
        older = _record()
        newer = copy.deepcopy(older)
        newer["connectors"]["connectors"]["salesforce"]["api_version"] = "v61.0"
        result = rr.diff_reproducibility(older, newer)
        assert result["dimensions_changed"] == ["connector versions"]

    def test_an_ingestor_code_change_is_detected(self):
        older = _record()
        newer = copy.deepcopy(older)
        newer["connectors"]["connectors"]["jira"]["code_fingerprint"] = "deadbeefdeadbeef"
        result = rr.diff_reproducibility(older, newer)
        assert result["dimensions_changed"] == ["connector versions"]

    def test_a_provider_switch_is_attributed_to_ai_mode(self):
        older = _record()
        newer = copy.deepcopy(older)
        newer["ai_mode"]["generation_provider"] = "customer_tenant"
        result = rr.diff_reproducibility(older, newer)
        assert result["dimensions_changed"] == ["AI mode"]

    def test_a_policy_change_is_attributed_to_the_policy_dimension(self):
        older = _record()
        newer = copy.deepcopy(older)
        newer["assembly_policy"]["effective"]["confidence_floor"] = 0.3
        newer["assembly_policy"]["effective_fingerprint"] = "changedchangedxx"
        result = rr.diff_reproducibility(older, newer)
        assert result["dimensions_changed"] == ["assembly policy"]

    def test_several_dimensions_are_all_reported(self):
        """The support-engineer case: point at everything that differs, not the first
        thing found."""
        older = _record()
        newer = copy.deepcopy(older)
        newer["packs"]["pack_versions"]["cloud_ops"] = "1.1.0"
        newer["connectors"]["connectors"]["salesforce"]["api_version"] = "v61.0"
        newer["ai_mode"]["embedding_provider"] = "in_boundary"
        result = rr.diff_reproducibility(older, newer)
        assert result["dimensions_changed"] == [
            "AI mode", "connector versions", "pack versions"
        ]
        assert len(result["changes"]) == 3

    def test_the_summary_is_a_readable_sentence(self):
        older = _record()
        newer = copy.deepcopy(older)
        newer["packs"]["pack_versions"]["cloud_ops"] = "1.1.0"
        summary = rr.diff_reproducibility(older, newer)["summary"]
        assert "pack versions" in summary
        assert summary.endswith(".")

    def test_a_reason_wording_change_is_not_a_platform_change(self):
        """version_absent_reason is explanatory prose, not a version — rewording it
        must not read as the platform having changed."""
        older = _record()
        newer = copy.deepcopy(older)
        newer["assembly_policy"]["version_absent_reason"] = "reworded entirely"
        assert rr.diff_reproducibility(older, newer)["identical"] is True

    def test_an_added_connector_is_reported(self):
        older = rr.build_reproducibility_record(
            {"packIds": ["service_cloud"], "packVersions": {"service_cloud": "1.4.0"}},
            org_id="default", connector_ids=["salesforce"],
        )
        newer = rr.build_reproducibility_record(
            {"packIds": ["service_cloud"], "packVersions": {"service_cloud": "1.4.0"}},
            org_id="default", connector_ids=["salesforce", "jira"],
        )
        result = rr.diff_reproducibility(older, newer)
        assert result["identical"] is False
        assert "connector versions" in result["dimensions_changed"]

    def test_diffing_against_nothing_is_not_a_crash(self):
        record = _record()
        assert rr.diff_reproducibility(None, record)["identical"] is False
        assert rr.diff_reproducibility(record, None)["identical"] is False
        assert rr.diff_reproducibility(None, None)["identical"] is True


# ── the record reaches the run ────────────────────────────────────────────────


class TestStampedOntoTheRun:

    def test_materialization_stamps_the_record(self):
        """Structural: materialize_t2 must build the record onto the run, and must do
        it inside a guard so a version record can never fail a run."""
        source = (BACKEND / "app" / "materialize_t2.py").read_text(encoding="utf-8")
        assert "build_reproducibility_record" in source
        assert 'run["reproducibility"]' in source

        tree = ast.parse(source)
        guarded = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                body = ast.unparse(node)
                if "build_reproducibility_record" in body:
                    guarded = True
        assert guarded, (
            "the reproducibility record must be built inside a try/except — it "
            "explains a run and must never be the reason one fails"
        )

    def test_the_scope_boundary_is_documented(self):
        """AC4's bar is explanation, not byte-identical replay. Saying so in the
        module prevents the scope drift the task warns about."""
        doc = rr.__doc__ or ""
        assert "not" in doc and "byte-identical replay" in doc
        assert "re-serves persisted artifacts" in doc
