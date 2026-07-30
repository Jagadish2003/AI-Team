"""Contract tests for 2.0-A2 T3 — post-action monitoring (AC2, AC5, AC7).

AC2: *"Marking an opportunity actioned with a date starts monitoring; subsequent
runs produce movement records with baseline, current, delta, window comparability,
and both run ids."*
AC5: *"An unactioned opportunity never receives an outcome measurement, however
much its signals move."*
AC7: *"Every outcome number resolves to its evidence and the runs that produced
both measurements."*

The subtask's definition of done, one class each:

* an actioned opportunity with a qualifying subsequent run produces a record with
  baseline, current, delta, comparability and both run ids;
* an opportunity with no post-action run produces NO record — not a zero-delta
  one, because absence and "no change" are different facts;
* the record is idempotent per ``(identity, comparison run)``;
* comparability is always populated, never null;
* the record is a stored artifact, so a later pack change cannot retroactively
  alter a measurement already reported.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.opportunity_movement_record import (
    VERDICT_COMPARABLE,
    VERDICT_NOT_COMPARABLE,
    VERDICT_WEAK,
    missing_movement_fields,
)

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")
BASE = "/api/opportunity-movement"

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


def _auth(org_id: str, token: str = DEV_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _seed_member(org_id: str, user_id: str, role: str = "owner") -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _org() -> str:
    org_id = f"org-a2t3-{uuid4().hex[:8]}"
    _seed_member(org_id, DEV_TOKEN)
    return org_id


def _identity() -> str:
    return f"opp_{uuid4().hex[:24]}"


def _opp(identity: str, *, owner_changes: float = 240.0, run_days_ago: int = 200):
    """An opportunity shaped as the Track A adapter stores it."""
    completed = datetime.now(timezone.utc) - timedelta(days=run_days_ago)
    return {
        "id": "opp_001",
        "tier": "Quick Win",
        "confidence": "HIGH",
        "opportunity_identity": identity,
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "baseline_mean": 201.6,
        "baseline_stddev": 2.7,
        "baseline_window_days": 90,
        "run_count": 5,
        "current_value": 2.4,
        "signal_key": f"service_cloud::{DETECTOR}::metric_value",
        "_debug": {
            "detector_id": DETECTOR,
            "metric_value": 2.4,
            "threshold": 1.5,
            "run_completed_at": completed.isoformat(),
            "raw_evidence": {
                "owner_changes_90d": owner_changes,
                "total_cases_90d": 800.0,
            },
        },
    }


def _instance(org: str, identity: str, run_id: str, days_ago: int, pack_version="1.2.0"):
    """Insert a per-run instance at a controlled point in time."""
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO opportunity_instances ("
                " opportunity_identity, run_id, org_id, pack_id, pack_version,"
                " detector_id, evidence_count, created_at, is_deleted"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE) "
                "ON CONFLICT (opportunity_identity, run_id) DO UPDATE "
                "SET created_at = EXCLUDED.created_at",
                (identity, run_id, org, "service_cloud", pack_version, DETECTOR,
                 0, created),
            )
        con.commit()


def _signal(org: str, run_id: str, metric_value: float, days_ago: int,
            metric_name: str = "owner_changes_90d", window_days: int = 90):
    """Insert a per-run signal snapshot — the re-measurement source."""
    captured = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO signal_snapshots ("
                " id, org_id, run_id, pack_id, detector_id, signal_key, metric_name,"
                " metric_value, threshold, fired, signal_source, captured_at,"
                " baseline_window_days"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s,%s,%s)",
                (str(uuid4()), org, run_id, "service_cloud", DETECTOR,
                 f"service_cloud::{DETECTOR}::{metric_name}", metric_name,
                 metric_value, 1.5, "salesforce", captured, window_days),
            )
        con.commit()


def _freeze_baseline(org: str, identity: str, run_id: str, **kw):
    from app.opportunity_baseline import capture_baselines_for_run

    return capture_baselines_for_run([_opp(identity, **kw)], org_id=org, run_id=run_id)


def _action(org: str, identity: str, action_date: str = ACTION_DATE):
    from app.opportunity_lifecycle import ensure_tracked, record_action

    ensure_tracked(org, identity, run_id="run_baseline")
    return record_action(org, identity, action_date, "analyst@example.com")


def _full_setup(org: str, identity: str, *, current_value: float = 150.0):
    """Actioned + baseline + two post-action runs with re-measured signals."""
    _freeze_baseline(org, identity, "run_baseline")
    _action(org, identity)
    _instance(org, identity, "run_baseline", days_ago=200)
    _instance(org, identity, "run_post_1", days_ago=60)
    _instance(org, identity, "run_post_2", days_ago=20)
    _signal(org, "run_post_1", 180.0, days_ago=60)
    _signal(org, "run_post_2", current_value, days_ago=20)
    return "run_post_2"


def _measure(org: str, identity: str, run_id: str):
    from app.opportunity_movement import measure_movement

    return measure_movement(org, identity, run_id)


def _raw_row(org: str, identity: str, run_id: str):
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT comparability_verdict, primary_delta, record, updated_at "
                "FROM opportunity_movements WHERE org_id = %s "
                "AND opportunity_identity = %s AND current_run_id = %s",
                (org, identity, run_id),
            )
            return cur.fetchone()


# ---------------------------------------------------------------------------
# AC2 — an actioned opportunity produces a movement record
# ---------------------------------------------------------------------------


class TestActionedOpportunityProducesAMovementRecord:
    def test_the_record_has_baseline_current_delta_comparability_and_both_run_ids(self):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        record = _measure(org, identity, run)

        assert missing_movement_fields(record) == []

        movement = next(
            m for m in record["movements"] if m["signalName"] == "owner_changes_90d"
        )
        assert movement["baselineValue"] == 240
        assert movement["currentValue"] == 150
        assert movement["delta"] == -90
        assert movement["direction"] == "improved"

        assert record["comparability"]["verdict"]
        assert record["baselineRunId"] == "run_baseline"
        assert record["currentRunId"] == "run_post_2"

    def test_the_record_is_retrievable_from_the_api(self, client):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)

        response = client.get(f"{BASE}/{identity}", headers=_auth(org))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 1
        assert body["measurements"][0]["currentRunId"] == run

    def test_the_run_scoped_read_returns_the_measurement(self, client):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)

        body = client.get(f"{BASE}/run/{run}", headers=_auth(org)).json()
        assert body["count"] == 1
        assert body["items"][0]["opportunityIdentity"] == identity

    def test_the_action_date_anchors_the_measurement(self):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        record = _measure(org, identity, run)
        assert record["actionDate"] == ACTION_DATE

    def test_only_runs_after_the_action_date_are_post_action(self):
        """No "most recent run" fallback — pre-action runs must not be folded in."""
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        record = _measure(org, identity, run)

        assert "run_baseline" not in record["postActionRunIds"], (
            "the pre-action run must not count as a post-action observation"
        )
        assert set(record["postActionRunIds"]) == {"run_post_1", "run_post_2"}

    def test_the_pipeline_helper_measures_every_eligible_opportunity(self):
        from app.opportunity_movement import measure_movements_for_run

        org, identity = _org(), _identity()
        run = _full_setup(org, identity)

        result = measure_movements_for_run(org, run)
        assert result["measured"] == 1
        assert identity in result["measuredIdentities"]


# ---------------------------------------------------------------------------
# AC5 — no outcome without action; absence is not zero
# ---------------------------------------------------------------------------


class TestNoOutcomeWithoutAction:
    def test_an_unactioned_opportunity_is_never_measured(self):
        """However much its signals move."""
        from app.opportunity_movement import (
            SKIP_NOT_ACTIONED,
            MovementMeasurementSkipped,
        )

        org, identity = _org(), _identity()
        _freeze_baseline(org, identity, "run_baseline")
        _instance(org, identity, "run_post_1", days_ago=20)
        _signal(org, "run_post_1", 1.0, days_ago=20)  # a huge move

        with pytest.raises(MovementMeasurementSkipped) as excinfo:
            _measure(org, identity, "run_post_1")
        assert excinfo.value.reason == SKIP_NOT_ACTIONED

    def test_a_dismissed_opportunity_is_never_measured(self):
        from app.opportunity_lifecycle import dismiss, ensure_tracked
        from app.opportunity_movement import (
            SKIP_NOT_ACTIONED,
            MovementMeasurementSkipped,
        )

        org, identity = _org(), _identity()
        _freeze_baseline(org, identity, "run_baseline")
        ensure_tracked(org, identity)
        dismiss(org, identity, "analyst@example.com")
        _instance(org, identity, "run_post_1", days_ago=20)
        _signal(org, "run_post_1", 10.0, days_ago=20)

        with pytest.raises(MovementMeasurementSkipped) as excinfo:
            _measure(org, identity, "run_post_1")
        assert excinfo.value.reason == SKIP_NOT_ACTIONED

    def test_an_opportunity_without_a_baseline_is_never_measured(self):
        from app.opportunity_movement import SKIP_NO_BASELINE, MovementMeasurementSkipped

        org, identity = _org(), _identity()
        _action(org, identity)
        _instance(org, identity, "run_post_1", days_ago=20)
        _signal(org, "run_post_1", 150.0, days_ago=20)

        with pytest.raises(MovementMeasurementSkipped) as excinfo:
            _measure(org, identity, "run_post_1")
        assert excinfo.value.reason == SKIP_NO_BASELINE

    def test_no_post_action_run_produces_no_record_not_a_zero_delta(self):
        """The distinction the definition of done insists on.

        "We have not measured" and "we measured no change" are different facts.
        """
        from app.opportunity_movement import (
            SKIP_NO_POST_ACTION_RUN,
            MovementMeasurementSkipped,
            get_movement_history,
        )

        org, identity = _org(), _identity()
        _freeze_baseline(org, identity, "run_baseline")
        _action(org, identity, action_date=TODAY.isoformat())
        # The only instance predates the action date.
        _instance(org, identity, "run_baseline", days_ago=200)
        _signal(org, "run_baseline", 240.0, days_ago=200)

        with pytest.raises(MovementMeasurementSkipped) as excinfo:
            _measure(org, identity, "run_baseline")
        assert excinfo.value.reason == SKIP_NO_POST_ACTION_RUN

        assert get_movement_history(org, identity) == [], (
            "no record at all — never a zero-delta record"
        )

    def test_a_run_on_the_action_date_itself_does_not_count(self):
        """Strictly after: a same-day observation may predate the change."""
        from app.opportunity_movement import (
            SKIP_NO_POST_ACTION_RUN,
            MovementMeasurementSkipped,
        )

        org, identity = _org(), _identity()
        action = (TODAY - timedelta(days=30)).isoformat()
        _freeze_baseline(org, identity, "run_baseline")
        _action(org, identity, action_date=action)
        _instance(org, identity, "run_same_day", days_ago=30)
        _signal(org, "run_same_day", 150.0, days_ago=30)

        with pytest.raises(MovementMeasurementSkipped) as excinfo:
            _measure(org, identity, "run_same_day")
        assert excinfo.value.reason == SKIP_NO_POST_ACTION_RUN

    def test_the_api_explains_an_absent_measurement(self, client):
        org, identity = _org(), _identity()
        response = client.get(f"{BASE}/{identity}", headers=_auth(org))
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "not a measurement of no change" in detail

    def test_skip_reasons_are_reported_by_the_pipeline_helper(self):
        """An empty outcome view must be explainable, not mysterious."""
        from app.opportunity_movement import SKIP_NOT_ACTIONED, measure_movements_for_run

        org, identity = _org(), _identity()
        _freeze_baseline(org, identity, "run_baseline")
        _instance(org, identity, "run_post_1", days_ago=20)
        _signal(org, "run_post_1", 150.0, days_ago=20)

        result = measure_movements_for_run(org, "run_post_1")
        assert result["measured"] == 0
        assert result["skipReasons"].get(SKIP_NOT_ACTIONED) == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_re_measuring_the_same_run_does_not_accumulate_duplicates(self):
        from app.opportunity_movement import get_movement_history

        org, identity = _org(), _identity()
        run = _full_setup(org, identity)

        for _ in range(4):
            _measure(org, identity, run)

        history = get_movement_history(org, identity)
        assert len(history) == 1, "one record per (identity, comparison run)"

    def test_two_different_comparison_runs_produce_two_records(self):
        from app.opportunity_movement import get_movement_history

        org, identity = _org(), _identity()
        _full_setup(org, identity)
        _measure(org, identity, "run_post_1")
        _measure(org, identity, "run_post_2")

        history = get_movement_history(org, identity)
        assert len(history) == 2
        assert [h["currentRunId"] for h in history] == ["run_post_1", "run_post_2"]

    def test_re_measuring_corrects_rather_than_duplicates(self):
        """A re-derivation of the SAME run pair should update in place."""
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)
        first = _raw_row(org, identity, run)

        # The run's signal is corrected upstream, then re-measured.
        _signal(org, run, 120.0, days_ago=19)
        _measure(org, identity, run)
        second = _raw_row(org, identity, run)

        assert first is not None and second is not None
        from app.opportunity_movement import get_movement_history

        assert len(get_movement_history(org, identity)) == 1


# ---------------------------------------------------------------------------
# Comparability is always populated
# ---------------------------------------------------------------------------


class TestComparabilityAlwaysPopulated:
    def test_the_stored_verdict_column_is_never_null(self):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)

        verdict, _delta, _record, _updated = _raw_row(org, identity, run)
        assert verdict, "comparability_verdict must never be null"
        assert verdict in (VERDICT_COMPARABLE, VERDICT_WEAK, VERDICT_NOT_COMPARABLE)

    def test_a_poorly_comparable_measurement_still_reports(self):
        """Never a blocked measurement — the caveat rides along instead."""
        org, identity = _org(), _identity()
        # Action only days ago, so the projected horizon has not elapsed.
        recent = (TODAY - timedelta(days=2)).isoformat()
        _freeze_baseline(org, identity, "run_baseline")
        _action(org, identity, action_date=recent)
        _instance(org, identity, "run_post_1", days_ago=1)
        _signal(org, "run_post_1", 150.0, days_ago=1, window_days=5)

        record = _measure(org, identity, "run_post_1")
        assert record["comparability"]["verdict"] != VERDICT_COMPARABLE
        assert record["comparability"]["reasons"]
        movement = next(
            m for m in record["movements"] if m["signalName"] == "owner_changes_90d"
        )
        assert movement["delta"] == -90, "the delta is still reported"

    def test_the_verdict_and_reasons_reach_the_api(self, client):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)

        measurement = client.get(f"{BASE}/{identity}", headers=_auth(org)).json()[
            "measurements"
        ][0]
        assert measurement["comparability"]["verdict"]
        assert "reasons" in measurement["comparability"]

    def test_the_org_list_reports_how_many_measurements_are_caveated(self, client):
        """Feeds T6's rule that an aggregate must carry its caveat count."""
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)

        body = client.get(BASE, headers=_auth(org)).json()
        assert body["count"] == 1
        assert "caveatedCount" in body

    def test_an_unknown_verdict_filter_is_refused(self, client):
        org = _org()
        response = client.get(f"{BASE}?verdict=nonsense", headers=_auth(org))
        assert response.status_code == 400
        assert "nonsense" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Stored, not computed at read time
# ---------------------------------------------------------------------------


class TestStoredNotRecomputed:
    def test_a_later_pack_change_does_not_alter_a_reported_measurement(self):
        """The whole reason this is a stored artifact.

        A number that quietly changes after it was quoted is worse than no number.
        """
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        record = _measure(org, identity, run)
        stored_delta = record["movements"][0]["delta"]

        # A later run lands with a different pack version and different signals.
        _instance(org, identity, "run_post_3", days_ago=5, pack_version="9.9.9")
        _signal(org, "run_post_3", 5.0, days_ago=5)

        from app.opportunity_movement import get_movement

        reread = get_movement(org, identity, run)
        assert reread["movements"][0]["delta"] == stored_delta
        assert reread["current"]["packVersion"] == "1.2.0"

    def test_the_record_survives_a_rewrite_of_the_run_scoped_opps_blob(self):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)
        before = _raw_row(org, identity, run)

        db.run_kv_set("opps", run, [])
        assert _raw_row(org, identity, run) == before

    def test_the_api_exposes_no_write_verb(self, client):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)

        for method in ("post", "put", "patch", "delete"):
            kwargs = {"headers": _auth(org)}
            if method != "delete":
                kwargs["json"] = {}
            response = getattr(client, method)(f"{BASE}/{identity}", **kwargs)
            assert response.status_code in (404, 405)


# ---------------------------------------------------------------------------
# AC7 — provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_both_run_ids_are_stored_as_real_columns(self):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)

        with closing(db.connect()) as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT baseline_run_id, current_run_id FROM opportunity_movements "
                    "WHERE org_id = %s AND opportunity_identity = %s",
                    (org, identity),
                )
                row = cur.fetchone()
        # tuple() because this driver returns rows as lists.
        assert tuple(row) == ("run_baseline", run)

    def test_the_record_resolves_to_the_baseline_artifact_it_compared_against(self):
        from app.opportunity_baseline import get_baseline

        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        record = _measure(org, identity, run)

        baseline = get_baseline(org, identity)
        assert record["baselineRunId"] == baseline["runId"]
        assert record["baseline"]["values"] == baseline["measuredValues"]

    def test_every_post_action_run_is_named(self):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        record = _measure(org, identity, run)
        assert len(record["postActionRunIds"]) == 2


# ---------------------------------------------------------------------------
# RBAC and tenancy
# ---------------------------------------------------------------------------


class TestRbacAndTenancy:
    def test_reads_require_authentication(self, client):
        assert client.get(f"{BASE}/{_identity()}").status_code in (401, 403)

    def test_a_viewer_cannot_read_measurements(self, client):
        org, identity = _org(), _identity()
        run = _full_setup(org, identity)
        _measure(org, identity, run)
        _seed_member(org, VIEWER_TOKEN, role="viewer")

        response = client.get(f"{BASE}/{identity}", headers=_auth(org, VIEWER_TOKEN))
        assert response.status_code == 403

    def test_one_org_cannot_read_anothers_measurements(self, client):
        org_a, org_b = _org(), _org()
        identity = _identity()
        run = _full_setup(org_a, identity)
        _measure(org_a, identity, run)

        assert client.get(f"{BASE}/{identity}", headers=_auth(org_b)).status_code == 404

    def test_the_org_list_never_leaks_another_orgs_measurements(self, client):
        org_a, org_b = _org(), _org()
        run_a = _full_setup(org_a, _identity_a := _identity())
        _measure(org_a, _identity_a, run_a)
        run_b = _full_setup(org_b, _identity_b := _identity())
        _measure(org_b, _identity_b, run_b)

        assert client.get(BASE, headers=_auth(org_a)).json()["count"] == 1

    def test_measurement_is_org_scoped_end_to_end(self):
        """Org A's action must not make org B's identical identity measurable."""
        from app.opportunity_movement import (
            SKIP_NOT_ACTIONED,
            MovementMeasurementSkipped,
        )

        org_a, org_b = _org(), _org()
        identity = _identity()
        run = _full_setup(org_a, identity)

        _freeze_baseline(org_b, identity, "run_baseline")
        _instance(org_b, identity, run, days_ago=20)
        _signal(org_b, run, 150.0, days_ago=20)

        with pytest.raises(MovementMeasurementSkipped) as excinfo:
            _measure(org_b, identity, run)
        assert excinfo.value.reason == SKIP_NOT_ACTIONED
