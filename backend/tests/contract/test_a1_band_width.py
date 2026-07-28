"""Contract tests for 2.0-A1 T4 — deterministic band width, on the wire.

The unit tests in ``discovery/tests/test_projection_band_width.py`` pin the
model. These pin the same guarantees where a customer actually meets them: the
stored projection, the three API surfaces the story names, and the Agent
Roadmap's ordering.

Coverage:
  * band width, its per-axis derivation, and the evidence label reach every
    projection surface (AC2);
  * the same seeded finding produces the same band through the REAL pipeline
    hook, run after run (AC2, AC5);
  * a thinner-evidence variant of the same seeded finding lands a demonstrably
    wider band on the wire (AC2);
  * a capped (single-source) finding is labelled on the wire, its strength is
    clamped, and it never orders above a corroborated equivalent in the Agent
    Roadmap — even with a far larger sample (AC4);
  * nothing the band-width model adds to the payload carries savings or
    guarantee language, swept over the WHOLE served projection (AC3);
  * the band-width block is stored with the opportunity, so 2.0-A2 can tell
    "the evidence moved" from "the model moved" (AC6).
"""

from __future__ import annotations

import copy
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from discovery.projection.band_width import (
    AXIS_CONFIDENCE_CAP,
    AXIS_CORROBORATION,
    AXIS_RECURRENCE_STABILITY,
    AXIS_SAMPLE_SIZE,
    BAND_WIDTH_MODEL_VERSION,
    CAPPED_STRENGTH_CEILING,
    CAPPED_STRENGTH_LABEL,
)


DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")

#: AC1/AC2 — the band-width derivation a projection must carry on the wire.
REQUIRED_BAND_WIDTH_KEYS = {
    "modelVersion",
    "lowPct",
    "highPct",
    "widthPct",
    "evidencePenalty",
    "evidenceQuality",
    "evidenceTier",
    "evidenceLabel",
    "bandTier",
    "bandLabel",
    "thinEvidence",
    "confidenceCapped",
    "rationale",
    "drivers",
    "inputs",
}

REQUIRED_STRENGTH_KEYS = {
    "value",
    "tier",
    "label",
    "capped",
    "cappedLabel",
    "comparableWithCapped",
}

#: The four band-width inputs, and only these four.
EXPECTED_AXES = [
    AXIS_SAMPLE_SIZE,
    AXIS_RECURRENCE_STABILITY,
    AXIS_CORROBORATION,
    AXIS_CONFIDENCE_CAP,
]

#: AC3 — no projection surface may carry these.
FORBIDDEN_PHRASES = (
    "will save",
    "will reduce",
    "will cut",
    "guarantee",
    "guaranteed",
    "savings",
    "roi",
    "eliminates",
    "ensures",
)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _auth(org_id: str = "default") -> Dict[str, str]:
    return {"Authorization": f"Bearer {DEV_TOKEN}", "X-Org-Id": org_id}


def _seed_workspace_member(org_id: str, role: str = "owner") -> None:
    """Give the dev token a role in this test's org so RBAC admits the request.

    ``closing()`` rather than a bare ``with``: the pooled connection proxy's
    ``__exit__`` commits but does not close, so only ``.close()`` recycles it.
    """
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, DEV_TOKEN, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _ids() -> tuple[str, str]:
    org_id = f"org-a1t4-{uuid4().hex[:8]}"
    _seed_workspace_member(org_id)
    return org_id, f"run-a1t4-{uuid4().hex[:6]}"


def _seeded_opp(
    opp_id: str = "opp_001",
    *,
    total_cases: float = 800.0,
    owner_changes: float = 240.0,
    recent_values: List[float] | None = None,
    corroboration_sources: List[str] | None = None,
    corroboration_rule_ids: List[str] | None = None,
    confidence: str = "HIGH",
    tier: str = "Quick Win",
    decision: str = "UNREVIEWED",
) -> Dict[str, Any]:
    """A seeded opportunity shaped exactly as the Track A adapter stores it.

    Defaults are the STRONG-evidence case; every keyword weakens exactly one
    band-width axis, so a test can isolate the axis it is about.
    """
    return {
        "id": opp_id,
        "title": "Elevated case owner reassignment",
        "category": "Automation Opportunity",
        "tier": tier,
        "decision": decision,
        "impact": 8,
        "effort": 3,
        "confidence": confidence,
        "aiRationale": "Owner changes are running above the handoff threshold.",
        "evidenceIds": ["ev_sf_aaa111"],
        "requiredPermissions": [],
        "override": {
            "isLocked": False,
            "rationaleOverride": "",
            "overrideReason": "",
            "updatedAt": None,
        },
        "corroboration_sources": (
            ["ServiceNow", "Jira"]
            if corroboration_sources is None
            else list(corroboration_sources)
        ),
        "corroboration_label": "Corroborated by ServiceNow incidents",
        "triple_corroboration": False,
        "corroboration_rule_ids": (
            ["COR-01", "COR-02"]
            if corroboration_rule_ids is None
            else list(corroboration_rule_ids)
        ),
        "focus_emphasis": None,
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "recent_values": list(
            [200.0, 205.0, 198.0, 202.0, 203.0]
            if recent_values is None
            else recent_values
        ),
        "baseline_mean": 201.6,
        "baseline_stddev": 2.7,
        "baseline_window_days": 90,
        "run_count": 5,
        "signal_key": "service_cloud::HANDOFF_FRICTION::metric_value",
        "_debug": {
            "detector_id": "HANDOFF_FRICTION",
            "signal_source": "salesforce",
            "metric_value": 2.4,
            "threshold": 1.5,
            "roadmap_stage": "NEXT_30",
            "score_debug": {},
            "raw_evidence": {
                "owner_changes_90d": owner_changes,
                "total_cases_90d": total_cases,
                "handoff_score": 2.4,
            },
        },
    }


def _seed_run(org_id: str, run_id: str, opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Seed a run and project its opps through the REAL pipeline hook."""
    from app.materialize_t2 import _apply_intervention_projection

    db.run_set(
        run_id,
        {"id": run_id, "runId": run_id, "status": "complete", "org_id": org_id},
    )
    db.run_kv_set("opps", run_id, opps)
    db.run_kv_set("evidence", run_id, [])
    projected = _apply_intervention_projection(run_id, opps)
    assert projected == len(opps), "pipeline hook did not project every seeded opp"
    return opps


def _served_projection(client: TestClient, org: str, run: str, index: int = 0):
    response = client.get(f"/api/runs/{run}/opportunities", headers=_auth(org))
    assert response.status_code == 200, response.text
    return response.json()[index]["projection"]


def _width(projection: Dict[str, Any]) -> int:
    band = projection["magnitudeBand"]
    return band["highPct"] - band["lowPct"]


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


# ---------------------------------------------------------------------------
# AC2 — the derivation is on the wire, and it is complete.
# ---------------------------------------------------------------------------


class TestBandWidthOnTheWire:
    def test_band_width_derivation_reaches_every_projection_surface(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])

        surfaces = {
            "opportunities": client.get(
                f"/api/runs/{run}/opportunities", headers=_auth(org)
            ).json()[0]["projection"],
            "enrichment": client.get(
                f"/api/runs/{run}/opportunities/opp_001/enrichment", headers=_auth(org)
            ).json()["projection"],
            "blueprint": client.get(
                f"/api/runs/{run}/opportunities/opp_001/blueprint", headers=_auth(org)
            ).json()["projection"],
        }
        for name, projection in surfaces.items():
            band_width = projection.get("bandWidth")
            assert band_width, f"{name} surface carries no band-width derivation"
            missing = REQUIRED_BAND_WIDTH_KEYS - set(band_width)
            assert not missing, f"{name} band width missing {sorted(missing)}"

            strength = projection.get("projectionStrength")
            assert strength, f"{name} surface carries no projection strength"
            assert not REQUIRED_STRENGTH_KEYS - set(strength)

    def test_the_band_width_names_exactly_the_four_documented_inputs(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        band_width = _served_projection(client, org, run)["bandWidth"]

        assert [d["axis"] for d in band_width["drivers"]] == EXPECTED_AXES, (
            "band width must derive from exactly the four documented inputs"
        )
        assert set(band_width["inputs"]) == {
            "sampleTier",
            "sampleSize",
            "recurrenceStability",
            "corroborationStatus",
            "confidenceCapped",
        }

    def test_the_served_band_agrees_with_its_own_derivation(self, client):
        """A rendered label can never disagree with the band beside it."""
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        projection = _served_projection(client, org, run)

        band = projection["magnitudeBand"]
        band_width = projection["bandWidth"]
        assert band_width["lowPct"] == band["lowPct"]
        assert band_width["highPct"] == band["highPct"]
        assert band_width["widthPct"] == band["highPct"] - band["lowPct"]
        assert projection["basis"]["evidenceLabel"] == band_width["evidenceLabel"]
        assert projection["basis"]["bandLabel"] == band_width["bandLabel"]
        assert projection["basis"]["thinEvidence"] is band_width["thinEvidence"]

    def test_band_carries_the_model_version_for_2_0_a2(self, client):
        """AC6 groundwork: A2 must be able to tell evidence drift from model drift."""
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        projection = _served_projection(client, org, run)
        assert projection["bandWidth"]["modelVersion"] == BAND_WIDTH_MODEL_VERSION
        assert projection["schemaVersion"]


# ---------------------------------------------------------------------------
# AC2/AC5 — deterministic across runs; thinner evidence is visibly wider.
# ---------------------------------------------------------------------------


class TestDeterminismThroughThePipeline:
    def test_the_same_seeded_finding_bands_identically_across_runs(self, client):
        org_a, run_a = _ids()
        org_b, run_b = _ids()
        _seed_run(org_a, run_a, [_seeded_opp()])
        _seed_run(org_b, run_b, [_seeded_opp()])

        assert (
            _served_projection(client, org_a, run_a)["bandWidth"]
            == _served_projection(client, org_b, run_b)["bandWidth"]
        )

    def test_reprojecting_a_stored_opportunity_reproduces_the_band(self):
        """AC5: re-running against unchanged signal reproduces identical bands."""
        from discovery.projection import build_projection

        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored = db.run_kv_get("opps", run, [])[0]

        recomputed = build_projection(copy.deepcopy(stored))
        assert recomputed["bandWidth"] == stored["projection"]["bandWidth"]
        assert (
            recomputed["projectionStrength"] == stored["projection"]["projectionStrength"]
        )

    @pytest.mark.parametrize(
        "label, weaken",
        [
            ("small sample", {"total_cases": 12.0, "owner_changes": 6.0}),
            ("bursty recurrence", {"recent_values": [10.0, 400.0, 25.0, 380.0, 15.0]}),
            (
                "no corroboration",
                {"corroboration_sources": [], "corroboration_rule_ids": ["COR-08"]},
            ),
            ("low confidence", {"confidence": "LOW"}),
        ],
    )
    def test_thinner_evidence_yields_a_demonstrably_wider_band(
        self, client, label, weaken
    ):
        """AC2, per axis, on the wire."""
        org_strong, run_strong = _ids()
        org_thin, run_thin = _ids()
        _seed_run(org_strong, run_strong, [_seeded_opp()])
        _seed_run(org_thin, run_thin, [_seeded_opp(**weaken)])

        strong = _served_projection(client, org_strong, run_strong)
        thin = _served_projection(client, org_thin, run_thin)

        assert _width(thin) > _width(strong), (
            f"weakening {label} must widen the band on the wire"
        )
        assert thin["bandWidth"]["evidenceQuality"] < strong["bandWidth"][
            "evidenceQuality"
        ]
        assert thin["bandWidth"]["thinEvidence"] is True


# ---------------------------------------------------------------------------
# AC4 — capped confidence: labelled, clamped, and never ordered above a peer.
# ---------------------------------------------------------------------------


class TestCappedConfidenceOnTheWire:
    def _capped_kwargs(self) -> Dict[str, Any]:
        return {"corroboration_sources": [], "corroboration_rule_ids": ["COR-08"]}

    def test_capped_projection_is_labelled_on_every_surface(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp(**self._capped_kwargs())])

        for projection in (
            _served_projection(client, org, run),
            client.get(
                f"/api/runs/{run}/opportunities/opp_001/enrichment", headers=_auth(org)
            ).json()["projection"],
            client.get(
                f"/api/runs/{run}/opportunities/opp_001/blueprint", headers=_auth(org)
            ).json()["projection"],
        ):
            assert projection["confidenceCapped"] is True
            strength = projection["projectionStrength"]
            assert strength["capped"] is True
            assert strength["cappedLabel"] == CAPPED_STRENGTH_LABEL
            assert strength["comparableWithCapped"] is False

    def test_capped_strength_is_clamped_below_the_ceiling(self, client):
        """Even a very large sample cannot lift a capped finding's strength."""
        org, run = _ids()
        _seed_run(
            org,
            run,
            [_seeded_opp(total_cases=5000.0, owner_changes=2500.0, **self._capped_kwargs())],
        )
        strength = _served_projection(client, org, run)["projectionStrength"]
        assert strength["value"] <= CAPPED_STRENGTH_CEILING

    def test_capped_band_is_wider_than_a_corroborated_equivalent(self, client):
        org_corr, run_corr = _ids()
        org_capped, run_capped = _ids()
        _seed_run(org_corr, run_corr, [_seeded_opp()])
        _seed_run(org_capped, run_capped, [_seeded_opp(**self._capped_kwargs())])

        assert _width(_served_projection(client, org_capped, run_capped)) > _width(
            _served_projection(client, org_corr, run_corr)
        )

    def test_roadmap_never_orders_a_capped_finding_above_a_corroborated_one(self):
        """AC4 where it bites: the Agent Roadmap's stage ordering.

        The capped finding is deliberately given the LARGER sample and is
        supplied FIRST, so only the AC4 rule can produce the expected order.
        """
        from app.roadmap_engine import build_roadmap

        org, run = _ids()
        capped = _seeded_opp(
            "opp_capped", total_cases=5000.0, owner_changes=2500.0, **self._capped_kwargs()
        )
        corroborated = _seeded_opp("opp_corroborated", total_cases=32.0, owner_changes=16.0)
        opps = _seed_run(org, run, [capped, corroborated])

        stage = next(
            s for s in build_roadmap(opps)["stages"] if s["id"] == "NEXT_30"
        )
        ids = [o["id"] for o in stage["opportunities"]]
        assert ids == ["opp_corroborated", "opp_capped"], (
            "a capped finding must never present above a corroborated equivalent"
        )

    def test_roadmap_keeps_its_own_ordering_decisions_intact(self):
        """Projection strength is used CAREFULLY: it demotes, it does not re-rank.

        Two uncapped findings supplied weakest-first must stay weakest-first —
        the roadmap's incoming order encodes analyst-facing decisions that a
        projection has no business overturning.
        """
        from app.roadmap_engine import build_roadmap

        org, run = _ids()
        weak = _seeded_opp("opp_weak", total_cases=12.0, owner_changes=6.0)
        strong = _seeded_opp("opp_strong")
        opps = _seed_run(org, run, [weak, strong])

        stage = next(
            s for s in build_roadmap(opps)["stages"] if s["id"] == "NEXT_30"
        )
        assert [o["id"] for o in stage["opportunities"]] == ["opp_weak", "opp_strong"]

    def test_roadmap_builds_when_an_opportunity_has_no_projection(self):
        """Ordering is advisory — never a reason a roadmap fails to build."""
        from app.roadmap_engine import build_roadmap

        unprojected = _seeded_opp("opp_none")
        capped = _seeded_opp("opp_capped", **self._capped_kwargs())
        org, run = _ids()
        _seed_run(org, run, [capped])

        stage = next(
            s
            for s in build_roadmap([unprojected, capped])["stages"]
            if s["id"] == "NEXT_30"
        )
        assert [o["id"] for o in stage["opportunities"]] == ["opp_none", "opp_capped"]


# ---------------------------------------------------------------------------
# AC6 — the derivation is STORED, not computed at read time.
# ---------------------------------------------------------------------------


class TestBandWidthIsStored:
    def test_band_width_is_persisted_with_the_opportunity(self):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])

        stored = db.run_kv_get("opps", run, [])[0]["projection"]
        assert stored["bandWidth"], "AC6: the band derivation must be stored"
        assert stored["projectionStrength"]["value"] is not None

    def test_served_band_width_equals_the_stored_one(self, client):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp()])
        stored = db.run_kv_get("opps", run, [])[0]["projection"]

        assert _served_projection(client, org, run)["bandWidth"] == stored["bandWidth"]


# ---------------------------------------------------------------------------
# AC3 — vocabulary, swept over the WHOLE served projection.
# ---------------------------------------------------------------------------


class TestVocabularyOverTheWholePayload:
    @pytest.mark.parametrize(
        "variant",
        [
            {},
            {"confidence": "LOW"},
            {"corroboration_sources": [], "corroboration_rule_ids": ["COR-08"]},
            {"total_cases": 12.0, "owner_changes": 6.0},
        ],
    )
    def test_no_savings_or_guarantee_language_anywhere_in_the_payload(
        self, client, variant
    ):
        org, run = _ids()
        _seed_run(org, run, [_seeded_opp(**variant)])
        projection = _served_projection(client, org, run)

        for text in _all_strings(projection):
            lowered = text.lower()
            for phrase in FORBIDDEN_PHRASES:
                assert phrase not in lowered, (
                    f"projection text {text!r} contains forbidden phrase {phrase!r} "
                    "— a band is an evidence statement, not a savings claim"
                )

    def test_the_band_is_a_range_at_every_evidence_level(self, client):
        for variant in (
            {},
            {"total_cases": 12.0, "owner_changes": 6.0},
            {"corroboration_sources": [], "corroboration_rule_ids": ["COR-08"]},
        ):
            org, run = _ids()
            _seed_run(org, run, [_seeded_opp(**variant)])
            band = _served_projection(client, org, run)["magnitudeBand"]
            assert band["lowPct"] < band["highPct"], variant
