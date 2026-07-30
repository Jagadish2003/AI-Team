"""2.0-A1 — the pipeline ordering the stored projection depends on.

Structural, not behavioural, and deliberately so: these are ORDERING invariants
across two long materialization functions, and the failure mode they guard is
silent. A projection stored without a stable identity still looks fine on every
screen — it just cannot be followed across runs, so 2.0-A2's comparison quietly
has nothing to match on. Nothing goes red; the flywheel simply never starts.

That is exactly what happened on one of the two paths and is what these tests
now pin:

  * ``routes_sprint4_t1._run_trackb_and_persist``  (POST /api/runs/{id}/compute)
  * ``materialize_t2.run_trackb_and_persist``      (the run-start path in
                                                    routes_sprint4_t2)

Both are live. Both must stamp the cross-run identity and record opportunity
instances BEFORE the projection is computed and stored, and both must re-store
the roadmap AFTER — because the roadmap artifact is built long before a
projection can exist.

Source-order inspection rather than a full pipeline run: exercising these paths
end to end needs live ingestion and a DB, and the invariant being protected is
literally the order of statements.
"""

from __future__ import annotations

import inspect

import pytest

from app import materialize_t2, routes_sprint4_t1

#: (module, function) for every live materialization path.
MATERIALIZATION_PATHS = (
    pytest.param(
        materialize_t2, "run_trackb_and_persist", id="materialize_t2"
    ),
    pytest.param(
        routes_sprint4_t1, "_run_trackb_and_persist", id="routes_sprint4_t1"
    ),
)


def _source(module, func_name: str) -> str:
    return inspect.getsource(getattr(module, func_name))


def _index_of(source: str, needle: str, label: str) -> int:
    position = source.find(needle)
    assert position != -1, f"{label} ({needle!r}) is missing from this path"
    return position


@pytest.mark.parametrize("module, func_name", MATERIALIZATION_PATHS)
class TestProjectionPipelineOrder:
    def test_path_stamps_the_cross_run_opportunity_identity(self, module, func_name):
        """Without it the stored projection is not comparable across runs.

        2.0-A1 T6 records this identity in the projection's provenance, and it is
        the ONLY key 2.0-A2 can follow one problem's projections by.
        """
        source = _source(module, func_name)
        assert "stamp_opportunity_identities" in source, (
            f"{func_name} never stamps opportunity_identity — every projection it "
            "stores will be marked crossRunComparable=False and 2.0-A2 will have "
            "nothing to match on"
        )

    def test_path_records_opportunity_instances(self, module, func_name):
        """The rows the cross-run projection history is attached to."""
        source = _source(module, func_name)
        assert "record_opportunity_instances" in source, (
            f"{func_name} never records opportunity instances — "
            "record_projections_on_instances will find no row to attach to"
        )

    def test_identity_is_stamped_before_the_projection_is_stored(
        self, module, func_name
    ):
        source = _source(module, func_name)
        identity = _index_of(
            source, "stamp_opportunity_identities", "identity stamping"
        )
        projection = _index_of(
            source, "_apply_intervention_projection", "projection storage"
        )
        assert identity < projection, (
            "the projection is stored before the identity exists, so its "
            "provenance records no identity"
        )

    def test_roadmap_is_rebuilt_after_the_projection_exists(self, module, func_name):
        """The roadmap artifact is built before any projection can exist.

        It is stored early — before temporal enrichment, which the projection
        depends on — so without the rebuild the roadmap API serves
        projection-less opportunities and T4's capped-confidence ordering rule
        has nothing to order on.
        """
        source = _source(module, func_name)
        projection = _index_of(
            source, "_apply_intervention_projection", "projection storage"
        )
        rebuild = _index_of(
            source, "_rebuild_roadmap_with_projections", "roadmap rebuild"
        )
        assert projection < rebuild, (
            "the roadmap is rebuilt before the projections exist — it will store "
            "opportunities without them"
        )


class TestIdentityAgreesAcrossOpportunityShapes:
    """The two writes must derive the SAME identity or the join silently fails.

    ``record_opportunity_instances`` consumes the RAW runner opportunity (org /
    detector / signal at top level); ``stamp_opportunity_identities`` consumes
    the Track A stored shape (detector under ``_debug``). The projection is then
    attached by looking the instance row up by the stamped identity — so if the
    two shapes hashed differently, every lookup would miss and no projection
    would ever reach an instance row.
    """

    def test_raw_and_track_a_shapes_produce_the_same_identity(self):
        from app.opportunity_instances import build_opportunity_instance

        common = {
            "id": "opp_001",
            "packId": "service_cloud",
            "packVersion": "1.2.0",
            "impact": 8,
            "effort": 3,
            "confidence": "HIGH",
            "tier": "Quick Win",
            "evidenceIds": ["ev_1"],
        }
        raw = {
            **common,
            "orgId": "org_1",
            "detector_id": "HANDOFF_FRICTION",
            "signal_source": "salesforce",
        }
        track_a = {
            **common,
            "_debug": {
                "detector_id": "HANDOFF_FRICTION",
                "signal_source": "salesforce",
            },
        }

        raw_identity = build_opportunity_instance(
            raw, "run_1", org_id="org_1"
        ).opportunity_identity
        track_a_identity = build_opportunity_instance(
            track_a, "run_1", org_id="org_1"
        ).opportunity_identity

        assert raw_identity == track_a_identity, (
            "the instance row and the stamped opportunity disagree on identity — "
            "stored projections would never attach to their instance row"
        )
