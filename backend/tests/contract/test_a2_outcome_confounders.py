"""Contract tests for 2.0-A2 T4 — confounder surfacing (AC3).

AC3: *"Seeded confounders (volume shift beyond threshold, changed CI population,
pack-version change) each surface as a labelled caveat on the measurement, and the
measurement still reports."*

One seeded scenario per confounder type, each asserting BOTH halves of AC3: the
caveat is attached, **and** the measurement still reports its delta. Plus the two
rules that govern the subtask — never a silent adjustment, never a blocked
measurement — verified end to end through the real pipeline rather than against the
pure detectors.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.outcome_confounders import (
    CONFOUNDER_CI_POPULATION_CHANGE,
    CONFOUNDER_PACK_VERSION_CHANGE,
    CONFOUNDER_SEASONALITY_MISMATCH,
    CONFOUNDER_VOLUME_SHIFT,
)

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
MOVEMENT_BASE = "/api/opportunity-movement"

TODAY = datetime.now(timezone.utc).date()
ACTION_DATE = (TODAY - timedelta(days=180)).isoformat()
DETECTOR = "HANDOFF_FRICTION"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _tables() -> None:
    from app.opportunity_baseline import ensure_opportunity_baseline_table
    from app.opportunity_instances import ensure_opportunity_instances_table
    from app.opportunity_lifecycle import ensure_opportunity_lifecycle_tables
    from app.opportunity_movement import ensure_opportunity_movement_table
    from app.temporal import ensure_signal_snapshots_table

    ensure_opportunity_lifecycle_tables()
    ensure_opportunity_baseline_table()
    ensure_opportunity_movement_table()
    ensure_opportunity_instances_table()
    ensure_signal_snapshots_table()


def _auth(org_id: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {DEV_TOKEN}", "X-Org-Id": org_id}


def _org() -> str:
    from app.rbac import _ensure_members_table

    org_id = f"org-a2t4-{uuid4().hex[:8]}"
    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, DEV_TOKEN, "owner", datetime.now(timezone.utc).isoformat()),
            )
        con.commit()
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _opp(identity: str, *, population: float = 800.0, pack_version: str = "1.2.0",
         window_days: int = 90, run_days_ago: int = 200):
    completed = datetime.now(timezone.utc) - timedelta(days=run_days_ago)
    return {
        "id": "opp_001",
        "tier": "Quick Win",
        "confidence": "HIGH",
        "opportunity_identity": identity,
        "packId": "service_cloud",
        "packVersion": pack_version,
        "baseline_mean": 201.6,
        "baseline_window_days": window_days,
        "run_count": 5,
        "signal_key": f"service_cloud::{DETECTOR}::metric_value",
        "_debug": {
            "detector_id": DETECTOR,
            "metric_value": 2.4,
            "run_completed_at": completed.isoformat(),
            "raw_evidence": {
                "owner_changes_90d": 240.0,
                "total_cases_90d": population,
            },
        },
    }


def _instance(org: str, identity: str, run_id: str, days_ago: int,
              pack_version: str = "1.2.0"):
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_instances ("
                " opportunity_identity, run_id, org_id, pack_id, pack_version,"
                " detector_id, evidence_count, created_at, is_deleted"
                ") VALUES (%s,%s,%s,%s,%s,%s,0,%s,FALSE) "
                "ON CONFLICT (opportunity_identity, run_id) DO UPDATE "
                "SET created_at = EXCLUDED.created_at, "
                "    pack_version = EXCLUDED.pack_version",
                (identity, run_id, org, "service_cloud", pack_version, DETECTOR, created),
            )
        con.commit()


def _signals(org: str, run_id: str, *, movement: float, population: float,
             days_ago: int, window_days: int = 90):
    """Both signals for one run — the re-measurement source."""
    captured = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            for name, value in (
                ("owner_changes_90d", movement),
                ("total_cases_90d", population),
            ):
                cur.execute(
                    "INSERT INTO signal_snapshots ("
                    " id, org_id, run_id, pack_id, detector_id, signal_key,"
                    " metric_name, metric_value, threshold, fired, signal_source,"
                    " captured_at, baseline_window_days"
                    ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1.5,TRUE,'salesforce',%s,%s)",
                    (str(uuid4()), org, run_id, "service_cloud", DETECTOR,
                     f"service_cloud::{DETECTOR}::{name}", name, value,
                     captured, window_days),
                )
        con.commit()


def _entity(org: str, run_id: str, name: str):
    """An entity first seen in a run — feeds the CI-population comparison."""
    from app.routes_entities import ensure_entities_table

    ensure_entities_table()
    now = datetime.now(timezone.utc).isoformat()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO entities (id, org_id, entity_type, canonical_name,"
                " display_name, source_system, resolution_confidence,"
                " resolution_status, first_seen_run_id, last_seen_run_id,"
                " run_count, created_at, updated_at)"
                " VALUES (%s,%s,'system',%s,%s,'servicenow',1.0,'resolved',"
                "%s,%s,1,%s,%s) ON CONFLICT DO NOTHING",
                (str(uuid4()), org, name, name, run_id, run_id, now, now),
            )
        con.commit()


def _run(run_id: str, org: str, days_ago: int):
    """A run record — entity visibility is ordered by runs.seq."""
    db.run_set(run_id, {"id": run_id, "runId": run_id, "status": "complete",
                        "org_id": org})


def _setup(org: str, identity: str, *, baseline_population=800.0,
           current_population=800.0, baseline_pack="1.2.0", current_pack="1.2.0",
           baseline_window=90, current_window=90, current_movement=150.0):
    """Actioned + frozen baseline + two post-action runs, all parameters seedable."""
    from app.opportunity_baseline import capture_baselines_for_run
    from app.opportunity_lifecycle import ensure_tracked, record_action

    _run("run_baseline", org, 200)
    capture_baselines_for_run(
        [_opp(identity, population=baseline_population, pack_version=baseline_pack,
              window_days=baseline_window)],
        org_id=org, run_id="run_baseline",
    )
    ensure_tracked(org, identity, run_id="run_baseline")
    record_action(org, identity, ACTION_DATE, "analyst@example.com")

    _instance(org, identity, "run_baseline", days_ago=200, pack_version=baseline_pack)
    _instance(org, identity, "run_post_1", days_ago=60, pack_version=current_pack)
    _instance(org, identity, "run_post_2", days_ago=20, pack_version=current_pack)
    _run("run_post_1", org, 60)
    _run("run_post_2", org, 20)
    _signals(org, "run_post_1", movement=180.0, population=baseline_population,
             days_ago=60, window_days=current_window)
    _signals(org, "run_post_2", movement=current_movement,
             population=current_population, days_ago=20, window_days=current_window)
    return "run_post_2"


def _measure(org: str, identity: str, run_id: str):
    from app.opportunity_movement import measure_movement

    return measure_movement(org, identity, run_id)


def _caveats(record, type_code=None):
    found = record.get("confounders") or []
    return [c for c in found if type_code is None or c["type"] == type_code]


def _delta(record, signal="owner_changes_90d"):
    return next(m["delta"] for m in record["movements"] if m["signalName"] == signal)


# ---------------------------------------------------------------------------
# AC3 — one seeded scenario per confounder, each asserting BOTH halves
# ---------------------------------------------------------------------------


class TestSeededPackVersionChange:
    def test_it_surfaces_as_a_labelled_caveat_and_the_measurement_still_reports(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity, baseline_pack="1.2.0", current_pack="1.3.0")
        record = _measure(org, identity, run)

        caveats = _caveats(record, CONFOUNDER_PACK_VERSION_CHANGE)
        assert caveats, "a pack-version change must surface as a caveat"
        caveat = caveats[0]
        assert caveat["severity"] == "material"
        assert caveat["label"]
        assert caveat["detail"]["baselinePackVersion"] == "1.2.0"
        assert caveat["detail"]["currentPackVersion"] == "1.3.0"
        assert caveat["detectedAt"]

        # ...and the measurement still reports.
        assert _delta(record) == -90
        assert record["comparability"]["verdict"]

    def test_no_caveat_when_the_pack_version_is_unchanged(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity, baseline_pack="1.2.0", current_pack="1.2.0")
        record = _measure(org, identity, run)
        assert not _caveats(record, CONFOUNDER_PACK_VERSION_CHANGE)


class TestSeededVolumeShift:
    def test_it_surfaces_as_a_labelled_caveat_and_the_measurement_still_reports(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity, baseline_population=800.0,
                     current_population=1600.0)
        record = _measure(org, identity, run)

        caveats = _caveats(record, CONFOUNDER_VOLUME_SHIFT)
        assert caveats, "a volume shift beyond the threshold must surface as a caveat"
        detail = caveats[0]["detail"]
        assert detail["baselineValue"] == 800
        assert detail["currentValue"] == 1600
        assert detail["direction"] == "increased"
        assert detail["materialThreshold"]

        assert _delta(record) == -90

    def test_no_caveat_for_ordinary_drift(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity, baseline_population=800.0,
                     current_population=820.0)
        record = _measure(org, identity, run)
        assert not _caveats(record, CONFOUNDER_VOLUME_SHIFT)


class TestSeededCiPopulationChange:
    def test_it_surfaces_as_a_labelled_caveat_and_the_measurement_still_reports(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity)

        # Baseline run sees five services; the post-action runs see five more.
        for i in range(5):
            _entity(org, "run_baseline", f"service-{i}")
        for i in range(5, 12):
            _entity(org, "run_post_2", f"service-{i}")

        record = _measure(org, identity, run)

        caveats = _caveats(record, CONFOUNDER_CI_POPULATION_CHANGE)
        assert caveats, "a changed CI population must surface as a caveat"
        detail = caveats[0]["detail"]
        assert detail["addedCount"] >= 1
        assert "different populations" in detail["implication"]

        assert _delta(record) == -90

    def test_no_caveat_when_the_population_is_unchanged(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity)
        for i in range(6):
            _entity(org, "run_baseline", f"stable-service-{i}")
        record = _measure(org, identity, run)
        assert not _caveats(record, CONFOUNDER_CI_POPULATION_CHANGE)


class TestSeededSeasonalityMismatch:
    def test_it_surfaces_as_a_labelled_caveat_and_the_measurement_still_reports(self):
        """A very long baseline window vs a short current one lands in different months."""
        org, identity = _org(), _identity()
        run = _setup(org, identity, baseline_window=30, current_window=30)
        record = _measure(org, identity, run)

        # The baseline window ended ~200 days ago and the current ~20 days ago, so
        # with 30-day windows they cover different calendar months.
        caveats = _caveats(record, CONFOUNDER_SEASONALITY_MISMATCH)
        assert caveats, "windows in different parts of the year must surface a caveat"
        detail = caveats[0]["detail"]
        assert detail["mode"]
        assert detail["overlapFraction"] < detail["minOverlapThreshold"]

        assert _delta(record) == -90


# ---------------------------------------------------------------------------
# Never a silent adjustment
# ---------------------------------------------------------------------------


class TestNeverASilentAdjustment:
    def test_the_delta_is_identical_with_and_without_confounders(self):
        """The decisive test.

        Two setups differing ONLY in confounder-triggering fields must report the
        SAME delta. If any code path adjusted the delta to compensate, they would
        differ.
        """
        org_clean, id_clean = _org(), _identity()
        run_clean = _setup(org_clean, id_clean, current_movement=150.0)
        clean = _measure(org_clean, id_clean, run_clean)

        org_dirty, id_dirty = _org(), _identity()
        run_dirty = _setup(
            org_dirty, id_dirty,
            current_movement=150.0,
            baseline_pack="1.2.0", current_pack="9.9.9",
            baseline_population=800.0, current_population=4000.0,
        )
        dirty = _measure(org_dirty, id_dirty, run_dirty)

        assert len(_caveats(dirty)) >= 2, "the dirty setup should carry caveats"
        assert not _caveats(clean, CONFOUNDER_PACK_VERSION_CHANGE)
        assert _delta(dirty) == _delta(clean) == -90, (
            "the delta must be identical — a confounder never adjusts the number"
        )

    def test_the_raw_baseline_and_current_values_are_reported_unmodified(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity, baseline_population=800.0,
                     current_population=4000.0)
        record = _measure(org, identity, run)

        assert record["baseline"]["values"]["total_cases_90d"] == 800
        assert record["current"]["values"]["total_cases_90d"] == 4000


# ---------------------------------------------------------------------------
# Never a blocked measurement
# ---------------------------------------------------------------------------


class TestNeverABlockedMeasurement:
    def test_a_measurement_with_every_confounder_still_persists_and_serves(self, client):
        org, identity = _org(), _identity()
        run = _setup(
            org, identity,
            baseline_pack="1.0.0", current_pack="2.0.0",
            baseline_population=800.0, current_population=5000.0,
            baseline_window=30, current_window=30,
        )
        for i in range(6):
            _entity(org, "run_baseline", f"svc-{i}")
        for i in range(6, 20):
            _entity(org, "run_post_2", f"svc-{i}")

        record = _measure(org, identity, run)
        assert len(_caveats(record)) >= 3

        served = client.get(f"{MOVEMENT_BASE}/{identity}", headers=_auth(org))
        assert served.status_code == 200, "the measurement must still report"
        measurement = served.json()["measurements"][0]
        assert measurement["movements"]
        assert measurement["confounders"]

    def test_a_confounded_measurement_appears_in_the_org_list(self, client):
        org, identity = _org(), _identity()
        run = _setup(org, identity, current_pack="3.0.0")
        _measure(org, identity, run)

        body = client.get(MOVEMENT_BASE, headers=_auth(org)).json()
        assert body["count"] == 1


# ---------------------------------------------------------------------------
# Storage — counts promoted for T6
# ---------------------------------------------------------------------------


class TestConfoundersArePersisted:
    def test_the_caveats_are_stored_with_the_record(self):
        from app.opportunity_movement import get_movement

        org, identity = _org(), _identity()
        run = _setup(org, identity, current_pack="1.3.0")
        _measure(org, identity, run)

        stored = get_movement(org, identity, run)
        assert _caveats(stored, CONFOUNDER_PACK_VERSION_CHANGE)

    def test_the_counts_are_promoted_to_columns_for_aggregation(self):
        """T6 must count caveated measurements without parsing every record."""
        org, identity = _org(), _identity()
        run = _setup(org, identity, current_pack="1.3.0",
                     baseline_population=800.0, current_population=1600.0)
        _measure(org, identity, run)

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT confounder_count, confounder_material_count, "
                    "confounder_types FROM opportunity_movements "
                    "WHERE org_id = %s AND opportunity_identity = %s",
                    (org, identity),
                )
                count, material, types_json = cur.fetchone()

        assert count >= 2
        assert material >= 2
        stored_types = json.loads(types_json)
        assert CONFOUNDER_PACK_VERSION_CHANGE in stored_types
        assert CONFOUNDER_VOLUME_SHIFT in stored_types

    def test_the_record_carries_a_summary_for_consumers(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity, current_pack="1.3.0")
        record = _measure(org, identity, run)

        summary = record["confounderSummary"]
        assert summary["count"] == len(record["confounders"])
        assert summary["materialCount"] + summary["advisoryCount"] == summary["count"]
        assert CONFOUNDER_PACK_VERSION_CHANGE in summary["byType"]

    def test_an_unconfounded_measurement_stores_zero_not_null(self):
        org, identity = _org(), _identity()
        run = _setup(org, identity)
        _measure(org, identity, run)

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT confounder_count, confounder_material_count "
                    "FROM opportunity_movements WHERE org_id = %s "
                    "AND opportunity_identity = %s",
                    (org, identity),
                )
                count, material = cur.fetchone()
        assert count is not None and material is not None

    def test_re_measuring_refreshes_the_counts_rather_than_accumulating(self):
        from app.opportunity_movement import get_movement_history

        org, identity = _org(), _identity()
        run = _setup(org, identity, current_pack="1.3.0")
        for _ in range(3):
            _measure(org, identity, run)

        assert len(get_movement_history(org, identity)) == 1


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


class TestTenancy:
    def test_the_ci_population_comparison_is_org_scoped(self):
        """One org's entities must never enter another org's population diff."""
        org_a, org_b = _org(), _org()
        identity = _identity()
        run = _setup(org_a, identity)

        for i in range(6):
            _entity(org_a, "run_baseline", f"a-svc-{i}")
        # Noise in another org, at the same run ids.
        for i in range(50):
            _entity(org_b, "run_post_2", f"b-svc-{i}")

        record = _measure(org_a, identity, run)
        caveats = _caveats(record, CONFOUNDER_CI_POPULATION_CHANGE)
        for caveat in caveats:
            for name in caveat["detail"].get("addedSample", []):
                assert "b-svc" not in name, "another org's entities leaked into the diff"
