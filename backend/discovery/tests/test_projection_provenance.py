"""2.0-A1 T6 — the stored projection's provenance spine.

AC6: *"The projection is stored with the opportunity so 2.0-A2 can later compare
it against measured outcome."*

This task is load-bearing: without a stored projection there is nothing to
compare a measured outcome against. But "stored" is not enough on its own — a
projection that cannot be tied back to the opportunity it describes, or followed
across runs, is no more useful than one never stored. These tests pin the stamp
that makes it findable, and the separation that keeps it reproducible.

Pure unit tests: the provenance module takes its clock reading as an argument
precisely so it can be tested without freezing time.
"""

from __future__ import annotations

import copy

import pytest

from discovery.projection import build_projection
from discovery.projection.provenance import (
    PROVENANCE_KEY,
    PROVENANCE_SCHEMA_VERSION,
    REQUIRED_PROVENANCE_FIELDS,
    build_provenance,
    get_provenance,
    is_storable,
    missing_provenance_fields,
    projection_core,
    stamp_projection,
)

CREATED_AT = "2026-07-28T12:00:00+00:00"


def seeded_finding(**overrides):
    finding = {
        "id": "opp_t6_001",
        "title": "Elevated case reassignment",
        "confidence": "HIGH",
        "tier": "Quick Win",
        "packId": "service_cloud",
        "packVersion": "1.2.0",
        "opportunity_identity": "opp_stable_abc123",
        "corroboration_label": "Corroborated by ServiceNow incidents",
        "corroboration_sources": ["ServiceNow", "Jira"],
        "corroboration_rule_ids": ["COR-01", "COR-02"],
        "triple_corroboration": False,
        "recent_values": [200.0, 205.0, 198.0, 202.0, 203.0],
        "baseline_mean": 201.6,
        "baseline_window_days": 90,
        "run_count": 5,
        "_debug": {
            "detector_id": "HANDOFF_FRICTION",
            "roadmap_stage": "NEXT_30",
            "metric_value": 2.4,
            "raw_evidence": {
                "owner_changes_90d": 240.0,
                "total_cases_90d": 800.0,
            },
        },
    }
    finding.update(overrides)
    return finding


def provenance(**overrides):
    kwargs = {
        "run_id": "run_t6",
        "opp_id": "opp_t6_001",
        "created_at": CREATED_AT,
        "org_id": "org_t6",
        "pack_id": "service_cloud",
        "pack_version": "1.2.0",
        "opportunity_identity": "opp_stable_abc123",
        "projection_schema_version": "1.1.0",
    }
    kwargs.update(overrides)
    return build_provenance(**kwargs)


# --------------------------------------------------------------------------
# The stamp itself.
# --------------------------------------------------------------------------


class TestProvenanceStamp:
    def test_carries_every_required_field(self):
        stamp = provenance()
        for field in REQUIRED_PROVENANCE_FIELDS:
            assert stamp.get(field), f"provenance missing {field}"

    def test_identifies_the_run_and_the_opportunity(self):
        """The minimum 2.0-A2 needs to say WHICH projection this is."""
        stamp = provenance()
        assert stamp["runId"] == "run_t6"
        assert stamp["oppId"] == "opp_t6_001"
        assert stamp["createdAt"] == CREATED_AT

    def test_carries_the_stable_cross_run_identity(self):
        """The key outcome tracking follows a problem forward with."""
        stamp = provenance()
        assert stamp["opportunityIdentity"] == "opp_stable_abc123"
        assert stamp["crossRunComparable"] is True

    def test_records_absent_identity_explicitly_rather_than_silently(self):
        """A reader must not have to infer "not trackable" from a null."""
        stamp = provenance(opportunity_identity=None)
        assert stamp["opportunityIdentity"] is None
        assert stamp["crossRunComparable"] is False

    def test_carries_the_schema_versions_a2_needs(self):
        """A2 must be able to tell a model change from an evidence change."""
        stamp = provenance(
            band_width_model_version="1.0.0", recommendation_schema_version="1.0.0"
        )
        assert stamp["provenanceSchemaVersion"] == PROVENANCE_SCHEMA_VERSION
        assert stamp["projectionSchemaVersion"] == "1.1.0"
        assert stamp["bandWidthModelVersion"] == "1.0.0"
        assert stamp["recommendationSchemaVersion"] == "1.0.0"

    def test_blank_and_whitespace_values_normalise_to_none(self):
        stamp = provenance(org_id="   ", pack_id="")
        assert stamp["orgId"] is None
        assert stamp["packId"] is None

    def test_is_pure_and_reads_no_clock(self):
        """Same inputs, same stamp — the caller owns the clock."""
        assert provenance() == provenance()


# --------------------------------------------------------------------------
# Stamping, and the core/provenance separation that protects AC5.
# --------------------------------------------------------------------------


class TestStampingAProjection:
    def test_stamp_attaches_provenance_without_mutating_the_input(self):
        projection = build_projection(seeded_finding())
        before = copy.deepcopy(projection)

        stamped = stamp_projection(projection, provenance())
        assert stamped[PROVENANCE_KEY]["runId"] == "run_t6"
        assert projection == before, "stamping must not mutate the computed payload"

    def test_core_excludes_provenance_and_nothing_else(self):
        projection = build_projection(seeded_finding())
        stamped = stamp_projection(projection, provenance())

        core = projection_core(stamped)
        assert PROVENANCE_KEY not in core
        assert core == projection, "the core must be the computed payload exactly"

    def test_two_stamps_of_the_same_finding_share_a_core(self):
        """AC5 survives T6.

        The whole reason provenance is stamped at STORE time rather than built
        into the payload: a timestamp inside the computed result would make
        every recomputation differ from its stored twin.
        """
        finding = seeded_finding()
        first = stamp_projection(build_projection(finding), provenance())
        second = stamp_projection(
            build_projection(finding),
            provenance(run_id="run_later", created_at="2026-09-01T00:00:00+00:00"),
        )

        assert projection_core(first) == projection_core(second)
        assert first[PROVENANCE_KEY] != second[PROVENANCE_KEY]

    def test_stamping_a_non_projection_returns_none(self):
        assert stamp_projection(None, provenance()) is None


# --------------------------------------------------------------------------
# Readback helpers.
# --------------------------------------------------------------------------


class TestReadback:
    def test_get_provenance_returns_an_empty_dict_for_a_pre_t6_projection(self):
        """A projection stored before T6 simply has no stamp — not an error."""
        projection = build_projection(seeded_finding())
        assert get_provenance(projection) == {}
        assert get_provenance(None) == {}

    def test_missing_fields_are_named(self):
        projection = build_projection(seeded_finding())
        assert set(missing_provenance_fields(projection)) == set(
            REQUIRED_PROVENANCE_FIELDS
        )

        stamped = stamp_projection(projection, provenance())
        assert missing_provenance_fields(stamped) == []

    def test_is_storable_requires_run_opp_and_timestamp(self):
        projection = build_projection(seeded_finding())
        assert is_storable(projection) is False
        assert is_storable(stamp_projection(projection, provenance())) is True

    def test_is_storable_does_not_require_a_cross_run_identity(self):
        """A projection identified by run + opportunity is still worth storing.

        It cannot be followed across runs, but it is comparable within its own —
        so it is stored, and the limitation is recorded rather than the whole
        projection discarded.
        """
        projection = build_projection(seeded_finding())
        stamped = stamp_projection(projection, provenance(opportunity_identity=None))
        assert is_storable(stamped) is True
        assert get_provenance(stamped)["crossRunComparable"] is False


# --------------------------------------------------------------------------
# The stored payload must carry everything the task enumerates.
# --------------------------------------------------------------------------

#: Every part the story requires a STORED projection to include.
REQUIRED_STORED_PARTS = (
    "direction",
    "magnitudeBand",
    "observationHorizonDays",
    "assumptionLedger",
    "basis",
)


class TestStoredProjectionCompleteness:
    def test_stored_projection_carries_every_required_part(self):
        stamped = stamp_projection(build_projection(seeded_finding()), provenance())

        for part in REQUIRED_STORED_PARTS:
            assert stamped.get(part) is not None, f"stored projection missing {part}"

    def test_stored_projection_carries_the_corroboration_label(self):
        """The evidence/corroboration label, stored — not re-derived later.

        2.0-A2 reports what the projection RESTED ON. Re-deriving it from the
        cross-run graph at comparison time would read today's corroboration, not
        the corroboration that produced the projection.
        """
        stamped = stamp_projection(build_projection(seeded_finding()), provenance())
        basis = stamped["basis"]

        assert basis["corroborationLabel"] == "Corroborated by ServiceNow incidents"
        assert basis["corroborationStatus"]
        assert basis["corroborationSources"] == ["ServiceNow", "Jira"]
        assert basis["corroborationRuleIds"] == ["COR-01", "COR-02"]
        assert basis["tripleCorroboration"] is False
        # T4's evidence label rides along too.
        assert basis["evidenceLabel"]

    def test_stored_projection_carries_a_run_reference_and_timestamp(self):
        stamped = stamp_projection(build_projection(seeded_finding()), provenance())
        stamp = get_provenance(stamped)

        assert stamp["runId"]
        assert stamp["createdAt"]
        assert stamp["oppId"]

    def test_a_finding_with_no_band_still_stores_a_complete_record(self):
        """Storing only the projectable findings would leave A2 blind to the rest."""
        finding = seeded_finding()
        finding["_debug"]["raw_evidence"] = {
            "owner_changes_90d": 1.0,
            "total_cases_90d": 2.0,
        }
        stamped = stamp_projection(build_projection(finding), provenance())

        assert stamped["direction"] == "no_material_change"
        assert stamped["magnitudeBand"] is None
        assert stamped["assumptionLedger"], "the ledger is stored regardless"
        assert stamped["basis"]
        assert is_storable(stamped)
