"""2.0-D4 T4 — the scale envelope: stated, and tested at both edges (AC5).

AC5: *"Load tests meet the documented envelope; at the edge, budgets and
deferrals are reported and no result is silently truncated."*

The shape of every dimension's coverage here is deliberate and comes straight
from the subtask: **a test at the stated limit that passes, and a test past the
limit that demonstrates loud degradation.** The second is the one that earns its
keep. A test proving the system survives its stated maximum tells a customer very
little; a test proving that at 120% of maximum the run completes with an explicit
deferral report tells them what failure looks like before they meet it.

These are load tests in the sense that matters — they drive real volume through
the real enforcing components (``RunBudget``, ``OpsEventStream``, the noise
floors) rather than asserting against a mock. They are sized to run in seconds:
the budget is injected rather than using the shipped 250,000, because what is
being proven is the BEHAVIOUR at the edge, and a quarter of a million synthetic
events would prove the same thing far more slowly.
"""

from __future__ import annotations

import pytest

from app.run_volume_report import build_run_volume_report, document_volume_block
from app.scale_envelope import (
    BASIS_MEASURED,
    DEGRADATION_MODES,
    DEGRADE_DEFER_AND_COUNT,
    DEGRADE_REPORT_ONLY,
    DIM_DOCUMENTS_PER_RUN,
    DIM_EVENTS_PER_RUN,
    DIM_FINDINGS_PER_RUN,
    DIM_SYSTEMS_PER_DEPLOYMENT,
    RECOGNISED_BASES,
    SOFT_FINDINGS_ENVELOPE,
    build_envelope,
    envelope_summary,
)
from discovery.signals.budget import RunBudget
from discovery.signals.noise_floor import NoiseFloorPolicy
from discovery.signals.operational_event import OperationalEvent, ResourceRef
from discovery.signals.ops_stream import OpsEventStream

ORG = "org_load"


def event(index: int, *, signature_group: int = 0, event_class: str = "error"):
    """One operational event. Distinct ids so nothing folds unless intended."""
    return OperationalEvent.build(
        org_id=ORG,
        source_system="aws",
        signal_id=f"evt_{index:07d}",
        event_type=f"ALARM_STATE_{signature_group}",
        resource=ResourceRef(
            provider="aws",
            resource_type="compute",
            resource_id=f"arn:aws:ec2:eu-west-1:1:instance/i-{signature_group:04d}",
            region="eu-west-1",
        ),
        event_class=event_class,
        severity="error",
        observed_at=f"2026-08-04T10:{index % 60:02d}:00+00:00",
    )


# --------------------------------------------------------------------------
# The envelope itself must be honest before its numbers mean anything.
# --------------------------------------------------------------------------


class TestTheEnvelopeIsStatedHonestly:
    def test_every_dimension_declares_a_recognised_basis(self):
        for key, dim in build_envelope().items():
            assert dim.basis in RECOGNISED_BASES, f"{key} has basis {dim.basis!r}"

    def test_every_dimension_declares_what_happens_at_its_edge(self):
        for key, dim in build_envelope().items():
            assert dim.degradation in DEGRADATION_MODES, key
            assert dim.degradation_detail.strip(), f"{key} does not say what degrading looks like"

    def test_no_dimension_degrades_by_dropping_work(self):
        """The rule the whole story rests on, asserted rather than assumed."""
        for key, dim in build_envelope().items():
            detail = dim.degradation_detail.lower()
            assert "silently" not in detail or "never" in detail or "not" in detail, key
            assert dim.degradation != "drop", key

    def test_the_event_budget_is_the_one_measured_dimension(self):
        """And the summary says so, rather than implying all four are measured."""
        summary = envelope_summary()
        assert summary["measuredCount"] == 1
        assert build_envelope()[DIM_EVENTS_PER_RUN].basis == BASIS_MEASURED
        assert "rest on a reproducible measurement" in summary["honestyNote"]

    def test_the_event_budget_is_not_a_second_copy_of_the_b7_number(self):
        """A number restated here would drift the moment B7 was recalibrated."""
        from discovery.signals.ops_calibration import CALIBRATED_RUN_EVENT_BUDGET

        assert build_envelope()[DIM_EVENTS_PER_RUN].limit == CALIBRATED_RUN_EVENT_BUDGET

    def test_unenforced_dimensions_declare_the_gap_rather_than_implying_a_cap(self):
        """Discovering a dimension has no envelope is a legitimate outcome."""
        dims = build_envelope()
        assert dims[DIM_FINDINGS_PER_RUN].declared_gap, (
            "findings-per-run is not enforced; that must be stated, not implied away"
        )
        assert dims[DIM_SYSTEMS_PER_DEPLOYMENT].declared_gap

    def test_every_dimension_states_the_conditions_its_number_holds_under(self):
        """Volume is not time — a number with no conditions is true only in a lab."""
        for key, dim in build_envelope().items():
            assert dim.conditions.strip(), f"{key} states no conditions"

    def test_the_summary_is_json_serialisable(self):
        import json

        s = envelope_summary()
        assert json.loads(json.dumps(s)) == s


# --------------------------------------------------------------------------
# Events per run — the calibrated, enforced dimension.
# --------------------------------------------------------------------------


class TestEventsPerRunAtAndPastTheLimit:
    def test_at_the_stated_limit_everything_is_processed(self):
        """The 'meets the envelope' half of AC5."""
        limit = 2_000
        stream = OpsEventStream(budget=limit)
        for i in range(limit):
            admission = stream.admit(event(i, signature_group=i % 50))
            assert not admission.is_deferred

        report = stream.budget_report()
        assert report.processed == limit
        assert report.deferred == 0
        assert report.breached is False

    def test_past_the_limit_the_excess_is_deferred_and_counted(self):
        """The half that matters: what 120% of maximum actually looks like."""
        limit = 2_000
        over = int(limit * 1.2)
        stream = OpsEventStream(budget=limit)
        deferred = 0
        for i in range(over):
            if stream.admit(event(i, signature_group=i % 50)).is_deferred:
                deferred += 1

        report = stream.budget_report()
        assert report.breached is True
        assert report.processed == limit
        assert deferred == over - limit == report.deferred
        assert report.seen == over, "every event was seen and accounted for"

    def test_nothing_is_lost_without_a_record(self):
        """seen == processed + deferred, exactly. No third bucket, no silent drop."""
        limit = 500
        stream = OpsEventStream(budget=limit)
        for i in range(limit * 2):
            stream.admit(event(i, signature_group=i % 25))
        r = stream.budget_report()
        assert r.seen == r.processed + r.deferred

    def test_the_deferral_is_explained_not_merely_counted(self):
        limit = 300
        stream = OpsEventStream(budget=limit)
        for i in range(limit + 40):
            stream.admit(event(i, signature_group=i % 10))
        payload = stream.budget_report().to_dict()
        assert payload["breached"] is True
        assert payload["deferred"] == 40
        assert payload.get("reason"), "a breach with no stated reason is not loud"

    def test_the_report_is_json_serialisable_for_the_run_record(self):
        import json

        stream = OpsEventStream(budget=50)
        for i in range(60):
            stream.admit(event(i))
        payload = stream.budget_report().to_dict()
        assert json.loads(json.dumps(payload)) == payload

    def test_an_unbounded_run_defers_nothing(self):
        stream = OpsEventStream(budget=None)
        for i in range(1_000):
            assert not stream.admit(event(i, signature_group=i % 40)).is_deferred
        assert stream.budget_report().deferred == 0


class TestVolumeIsNotTime:
    """The trap the subtask names: an envelope stated purely in volume is true
    on a fast estate and false on a throttled one."""

    def test_a_throttled_source_stops_on_a_bound_other_than_volume(self):
        """A run can stop early with the volume budget barely touched.

        The native connectors bound a run three ways precisely because of this.
        Here the budget is nowhere near exhausted, yet the run stopped — and the
        report must make that visible rather than implying a clean, complete
        ingest.
        """
        stream = OpsEventStream(budget=100_000)
        for i in range(120):  # a throttled provider yielded very little
            stream.admit(event(i, signature_group=i % 12))
        report = stream.budget_report()
        assert report.breached is False, "volume was never the binding constraint"

        run = {
            "runId": "run_throttled",
            "opportunities": [],
            "succeeded": ["aws"],
            "cloudOpsRuntime": {
                "awsEvents": {
                    "budget": report.to_dict(),
                    # What the connector records when a poll bound stops it early.
                    "poll": {
                        "complete": False,
                        "reason": "wall-clock deadline reached (180s)",
                    },
                }
            },
        }
        volume = build_run_volume_report(run).to_dict()
        events = next(d for d in volume["dimensions"] if d["key"] == DIM_EVENTS_PER_RUN)
        assert any("stopped early" in n for n in events["notes"]), (
            "a run cut short by time rather than volume must say so — otherwise "
            "the envelope reads as met when the ingest was incomplete"
        )

    def test_the_envelope_documents_its_conditions_for_events(self):
        conditions = build_envelope()[DIM_EVENTS_PER_RUN].conditions.lower()
        assert "throttl" in conditions
        assert "deadline" in conditions or "poll" in conditions


# --------------------------------------------------------------------------
# Noise floors — suppression is counted, never silent.
# --------------------------------------------------------------------------


class TestSuppressionIsCountedNotSilent:
    def test_floored_signals_are_reported_with_their_volume(self):
        stream = OpsEventStream(budget=None)
        # One signature fires once (below the audit floor of 5); another fires
        # ten times (above it).
        stream.admit(event(0, signature_group=1, event_class="audit"))
        for i in range(10):
            stream.admit(event(100 + i, signature_group=2, event_class="audit"))

        visible, report = NoiseFloorPolicy().apply(stream.active_signals())
        payload = report.to_dict()
        assert payload["suppressed_signatures"], "suppression happened but was not counted"
        assert payload["floors"], "the report must name the floors it applied"

    def test_an_error_class_signal_is_never_floored_by_default(self):
        stream = OpsEventStream(budget=None)
        stream.admit(event(0, signature_group=3, event_class="error"))
        visible, report = NoiseFloorPolicy().apply(stream.active_signals())
        assert len(visible) == 1, "error events must survive a single occurrence"


# --------------------------------------------------------------------------
# Findings per run — stated, reported, deliberately NOT enforced.
# --------------------------------------------------------------------------


class TestFindingsPerRunReportsButNeverTruncates:
    def test_a_run_within_the_threshold_reports_no_breach(self):
        run = {"runId": "r", "opportunities": [{}] * 100}
        obs = _dim(build_run_volume_report(run), DIM_FINDINGS_PER_RUN)
        assert obs["breached"] is False

    def test_a_run_past_the_threshold_is_reported(self):
        over = SOFT_FINDINGS_ENVELOPE + 250
        run = {"runId": "r", "opportunities": [{}] * over}
        obs = _dim(build_run_volume_report(run), DIM_FINDINGS_PER_RUN)
        assert obs["breached"] is True
        assert obs["exceededBy"] == 250
        assert obs["notes"], "exceeding the threshold must be explained"

    def test_no_finding_is_withheld_when_the_threshold_is_passed(self):
        """The whole reason this dimension does not enforce.

        A finding is the product's output. Silently withholding one to satisfy a
        volume target would mean a customer never learns a real problem exists —
        a far worse failure than a long list.
        """
        over = SOFT_FINDINGS_ENVELOPE + 1_000
        run = {"runId": "r", "opportunities": [{"id": i} for i in range(over)]}
        report = build_run_volume_report(run)
        obs = _dim(report, DIM_FINDINGS_PER_RUN)
        assert obs["observed"] == over, "every finding is still counted and served"
        assert obs["deferred"] is None, "findings are never deferred"
        assert obs["degradation"] == DEGRADE_REPORT_ONLY

    def test_the_note_says_it_is_a_prompt_not_a_truncation(self):
        run = {"runId": "r", "opportunities": [{}] * (SOFT_FINDINGS_ENVELOPE + 1)}
        note = " ".join(_dim(build_run_volume_report(run), DIM_FINDINGS_PER_RUN)["notes"])
        assert "still served" in note
        assert "not a truncation" in note


# --------------------------------------------------------------------------
# Documents per run.
# --------------------------------------------------------------------------


class TestDocumentsPerRun:
    def test_within_budget_nothing_is_deferred(self):
        run = {"runId": "r", "documentVolume": document_volume_block(400, {})}
        obs = _dim(build_run_volume_report(run), DIM_DOCUMENTS_PER_RUN)
        assert obs["breached"] is False
        assert obs["deferred"] == 0

    def test_budget_exceeded_skips_are_counted_as_deferred(self):
        """Transient: those files are retried next run, so they are deferrals."""
        run = {"runId": "r",
               "documentVolume": document_volume_block(900, {"budget_exceeded": 42})}
        obs = _dim(build_run_volume_report(run), DIM_DOCUMENTS_PER_RUN)
        assert obs["deferred"] == 42
        assert obs["breached"] is True

    def test_size_capped_skips_are_reported_but_not_deferred(self):
        """Deterministic: an oversized file will be oversized next run too, so
        counting it as 'deferred' would promise a retry that never helps."""
        run = {"runId": "r",
               "documentVolume": document_volume_block(900, {"size_capped": 7})}
        obs = _dim(build_run_volume_report(run), DIM_DOCUMENTS_PER_RUN)
        assert obs["deferred"] == 0
        assert any("size_capped" in n for n in obs["notes"]), "still reported"

    def test_a_run_with_no_document_stage_reports_not_observed(self):
        """Not a zero. Document ingestion runs outside the discovery run today,
        and a zero would read as 'nothing was skipped'."""
        obs = _dim(build_run_volume_report({"runId": "r"}), DIM_DOCUMENTS_PER_RUN)
        assert obs["observed"] is None


# --------------------------------------------------------------------------
# The assembled report.
# --------------------------------------------------------------------------


class TestTheAssembledReport:
    def test_a_clean_run_says_so_plainly(self):
        run = {"runId": "r", "opportunities": [{}] * 5, "succeeded": ["jira"]}
        report = build_run_volume_report(run)
        assert report.breached is False
        assert "within the stated envelope" in report.headline

    def test_a_breached_run_names_the_dimensions_and_the_totals(self):
        run = {
            "runId": "r",
            "opportunities": [{}] * 3,
            "succeeded": ["aws"],
            "cloudOpsRuntime": {"awsEvents": {"budget": {
                "processed": 250_000, "deferred": 900, "reason": "budget exhausted"}}},
            "documentVolume": document_volume_block(10, {"budget_exceeded": 5}),
        }
        report = build_run_volume_report(run)
        assert set(report.breached_dimensions) == {DIM_EVENTS_PER_RUN, DIM_DOCUMENTS_PER_RUN}
        assert report.total_deferred == 905
        assert "Nothing was discarded without a record" in report.headline

    def test_the_report_never_raises_on_a_malformed_run(self):
        for bad in ({}, {"cloudOpsRuntime": "nonsense"}, {"opportunities": "x"}, None):
            build_run_volume_report(bad).to_dict()

    def test_the_report_is_json_serialisable(self):
        import json

        payload = build_run_volume_report({"runId": "r", "opportunities": [{}]}).to_dict()
        assert json.loads(json.dumps(payload)) == payload


def _dim(report, key):
    return next(d for d in report.to_dict()["dimensions"] if d["key"] == key)
