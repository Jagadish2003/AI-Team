"""
R16-B1 (T3) — Stable Opportunity Identity tests.

Covers the acceptance criteria that belong to the identity task:

  AC3 — Running discovery twice on the same unchanged data produces the SAME
        opportunity_identity for the same finding, even though run timestamps
        (and run ids) differ.
  AC4 — Changing only a finding's confidence or score between runs does NOT
        change its opportunity_identity.
  AC5 — A genuinely different finding (different entities or signal type)
        produces a different opportunity_identity.

The pure-function tests pin the deterministic contract; the end-to-end test
proves the same guarantee holds through the real opportunity-assembly path in
``discovery.runner.run`` (offline, no live credentials).
"""
from __future__ import annotations

import os

import pytest

from discovery.opportunity_identity import (
    compute_opportunity_identity,
    primary_entity_keys_for_detector,
    stable_entity_key,
)


# ─────────────────────────── format / determinism ───────────────────────────

class TestIdentityFormat:
    def test_prefix_and_length(self):
        """opp_ prefix + 24 hex chars (sha256 truncation) == 28 chars total."""
        ident = compute_opportunity_identity("org1", "service_cloud", "HANDOFF_FRICTION", [])
        assert ident.startswith("opp_")
        assert len(ident) == 4 + 24
        assert all(c in "0123456789abcdef" for c in ident[4:])

    def test_pure_determinism_same_inputs_same_id(self):
        """Same inputs -> byte-identical id, every call (no randomness, no clock)."""
        args = ("orgA", "ncino", "COVENANT_TRACKING_GAP", ["process:covenant_tracking_gap", "system:salesforce"])
        first = compute_opportunity_identity(*args)
        again = compute_opportunity_identity(*args)
        assert first == again


# ─────────────────────────── AC3: stable across runs ────────────────────────

class TestAC3StableAcrossRuns:
    def test_identity_has_no_run_scoped_input(self):
        """AC3 — identity is a function of run-invariant inputs ONLY; there is
        no run_id / timestamp parameter, so it cannot vary run to run."""
        run1 = compute_opportunity_identity(
            "demo-org", "github_engineering", "GITHUB_PR_REVIEW_BOTTLENECK",
            primary_entity_keys_for_detector("GITHUB_PR_REVIEW_BOTTLENECK", "github"),
        )
        # A "second run" recomputes from the same defining characteristics.
        run2 = compute_opportunity_identity(
            "demo-org", "github_engineering", "GITHUB_PR_REVIEW_BOTTLENECK",
            primary_entity_keys_for_detector("GITHUB_PR_REVIEW_BOTTLENECK", "github"),
        )
        assert run1 == run2

    def test_entity_order_independent(self):
        """AC3 — entities discovered in a different order across runs must not
        change the id (keys are sorted internally)."""
        a = compute_opportunity_identity("o", "p", "SIG", ["process:x", "system:y"])
        b = compute_opportunity_identity("o", "p", "SIG", ["system:y", "process:x"])
        assert a == b

    def test_duplicate_entity_keys_collapse(self):
        """AC3 — a duplicated entity key must not change the id."""
        a = compute_opportunity_identity("o", "p", "SIG", ["process:x", "system:y"])
        b = compute_opportunity_identity("o", "p", "SIG", ["process:x", "system:y", "process:x"])
        assert a == b


# ──────────────── AC4: score / confidence do NOT affect identity ────────────

class TestAC4IdentityIgnoresRunVaryingMeasures:
    def test_confidence_and_score_are_not_identity_inputs(self):
        """AC4 — the signature is computed without any score/confidence input,
        so a finding whose only change is its confidence keeps its identity.

        Modelled exactly as the runner does it: two runs of the SAME finding
        whose scorer happened to emit different confidence/score values. The
        identity is built from (org, pack, signal_key, primary entities) — the
        run-varying measures never enter it."""
        finding = dict(
            org_id="demo-org",
            pack_id="ncino",
            signal_key="COVENANT_TRACKING_GAP",
            primary_entity_ids=["process:covenant_tracking_gap", "system:salesforce"],
        )

        # Run 1: confidence MEDIUM, score 42. Run 2: confidence HIGH, score 88.
        # Those values are NOT passed to compute_opportunity_identity at all —
        # which is the point of AC4.
        id_low_confidence = compute_opportunity_identity(**finding)
        id_high_confidence = compute_opportunity_identity(**finding)

        assert id_low_confidence == id_high_confidence

    def test_signature_excludes_extra_kwargs_by_construction(self):
        """AC4 — there is no parameter through which score/confidence/timestamp
        could leak into the id; passing them is a TypeError, proving they are
        structurally excluded from the identity basis."""
        with pytest.raises(TypeError):
            compute_opportunity_identity(  # type: ignore[call-arg]
                org_id="o", pack_id="p", signal_key="s",
                primary_entity_ids=[], confidence="HIGH",
            )


# ─────────────── AC5: genuinely different findings -> different ids ──────────

class TestAC5DistinctFindings:
    def test_different_signal_key_differs(self):
        """AC5 — different finding type (signal_key) -> different id."""
        a = compute_opportunity_identity("org", "github_engineering", "GITHUB_PR_REVIEW_BOTTLENECK", ["system:github"])
        b = compute_opportunity_identity("org", "github_engineering", "GITHUB_STALE_BRANCHES", ["system:github"])
        assert a != b

    def test_different_entities_differ(self):
        """AC5 — same org/pack/signal but different primary entities -> different id."""
        a = compute_opportunity_identity("org", "service_cloud", "HANDOFF_FRICTION", ["process:handoff_friction", "system:salesforce"])
        b = compute_opportunity_identity("org", "service_cloud", "HANDOFF_FRICTION", ["process:handoff_friction", "system:servicenow"])
        assert a != b

    def test_different_org_differs(self):
        """AC5 — the same problem in a different org is a different opportunity."""
        a = compute_opportunity_identity("org-A", "p", "SIG", ["system:x"])
        b = compute_opportunity_identity("org-B", "p", "SIG", ["system:x"])
        assert a != b

    def test_different_pack_differs(self):
        """AC5 — a different pack producing the finding is a different opportunity."""
        a = compute_opportunity_identity("org", "service_cloud", "SIG", ["system:x"])
        b = compute_opportunity_identity("org", "ncino", "SIG", ["system:x"])
        assert a != b

    def test_no_entities_vs_some_entities_differ(self):
        """AC5 — an entity-less finding is distinct from one with entities."""
        a = compute_opportunity_identity("org", "p", "SIG", [])
        b = compute_opportunity_identity("org", "p", "SIG", ["system:x"])
        assert a != b


# ───────────────────── stable entity key derivation ─────────────────────────

class TestStableEntityKeys:
    def test_canonicalises_case_and_whitespace(self):
        """The key uses the canonical_name rule (lower + collapse whitespace),
        so the same entity resolves to the same key regardless of casing — this
        is what makes the key stable across runs rather than the random
        Entity.id UUID."""
        assert stable_entity_key("Process", "PR_REVIEW_BOTTLENECK") == "process:pr_review_bottleneck"
        assert stable_entity_key("system", "  Sales force ") == "system:sales force"

    def test_detector_keys_shape(self):
        """A single-detector opportunity concerns one process + one system key."""
        keys = primary_entity_keys_for_detector("GITHUB_STALE_BRANCHES", "github")
        assert keys == ["process:github_stale_branches", "system:github"]

    def test_detector_keys_skip_blank_signal_source(self):
        keys = primary_entity_keys_for_detector("SOME_DETECTOR", "")
        assert keys == ["process:some_detector"]


# ───────────────────── required-input validation ────────────────────────────

class TestRequiredInputs:
    @pytest.mark.parametrize("field", ["org_id", "pack_id", "signal_key"])
    def test_missing_required_input_raises(self, field):
        args = dict(org_id="o", pack_id="p", signal_key="s", primary_entity_ids=[])
        args[field] = ""
        with pytest.raises(ValueError):
            compute_opportunity_identity(**args)

    def test_none_required_input_raises(self):
        with pytest.raises(ValueError):
            compute_opportunity_identity(None, "p", "s", [])  # type: ignore[arg-type]

    def test_empty_primary_entities_is_allowed(self):
        """Org/pack/signal-level findings concern no specific entity — allowed."""
        ident = compute_opportunity_identity("o", "p", "s", [])
        assert ident.startswith("opp_")

    def test_none_primary_entities_is_allowed(self):
        ident = compute_opportunity_identity("o", "p", "s", None)
        assert ident.startswith("opp_")


# ───────────── AC3 end-to-end: two real runs, identical identities ──────────

class TestAC3EndToEndRunnerStability:
    """Drives the real opportunity-assembly path in discovery.runner.run twice
    over the same unchanged offline GitHub data, with background jobs disabled,
    and asserts the per-detector opportunity_identity is identical across the
    two runs even though their run ids / timestamps differ (AC3)."""

    def _run_once(self):
        from discovery.runner import run
        result = run(mode="offline", pack="github_engineering",
                     org_id="demo-org", systems=["github"])
        return result

    @pytest.fixture(autouse=True)
    def _offline_env(self, monkeypatch):
        monkeypatch.setenv("INGEST_MODE", "offline")
        monkeypatch.setenv("AGENTIQ_DISABLE_BACKGROUND_JOBS", "1")

    def test_same_data_same_identities_across_runs(self):
        r1 = self._run_once()
        r2 = self._run_once()

        o1 = r1.get("opportunities", [])
        o2 = r2.get("opportunities", [])

        # The offline github pack must actually produce findings, or the test
        # would assert nothing.
        assert o1, "expected the offline github_engineering pack to produce opportunities"
        assert r1.get("runId") != r2.get("runId"), "runs must have distinct run ids"

        ids_run1 = {o["detector_id"]: o["opportunity_identity"] for o in o1}
        ids_run2 = {o["detector_id"]: o["opportunity_identity"] for o in o2}

        # AC3: same finding -> same identity across the two runs.
        assert ids_run1 == ids_run2

        # Every opportunity carries a well-formed stable identity.
        for ident in ids_run1.values():
            assert ident.startswith("opp_") and len(ident) == 4 + 24

    def test_identity_is_independent_of_run_scoped_fields(self):
        """AC3/AC4 through the real object: the identity does not encode runId,
        and two opportunities for the same detector across runs match even
        though their runId fields differ."""
        r1 = self._run_once()
        r2 = self._run_once()
        by_det_1 = {o["detector_id"]: o for o in r1.get("opportunities", [])}
        by_det_2 = {o["detector_id"]: o for o in r2.get("opportunities", [])}
        assert by_det_1, "expected opportunities from run 1"

        for det, opp1 in by_det_1.items():
            opp2 = by_det_2[det]
            assert opp1["runId"] != opp2["runId"]
            assert opp1["opportunity_identity"] == opp2["opportunity_identity"]
