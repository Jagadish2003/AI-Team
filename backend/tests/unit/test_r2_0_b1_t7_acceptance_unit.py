"""2.0-B1 T7 — QA: acceptance-criteria validation for the whole story.

One suite that walks 2.0-B1's six acceptance criteria end to end at the level
that needs no database, driving the REAL engines (``trace_graph``,
``retrieval_trace``, ``evidence_export``, ``export_audit``, and the MSP-B7
correlation-window layer) over seeded inputs:

  AC1 — a finding expands to a complete chain terminating in source records;
        every hop carries origin, connector, run id, and timestamp.
  AC2 — joined claims display the join type and correlation window used, and a
        claim whose join is outside window cannot appear (MSP-B7 regression).
  AC3 — the trace shows retrieval candidates both used and not used by assembly.
  AC4 — the export bundle verifies against its signature; altering any byte
        fails verification.
  AC5 — exports contain no unredacted secrets and no host x vulnerability
        enumeration (the 1.9 aggregation floor holds in export).
  AC6 — every export generation is an audit event naming user, scope, and time.

Deliberately cross-cutting rather than duplicating the per-subtask suites — the
depth for each criterion lives in its own file and is *not* re-implemented here:

  * AC1/AC2/AC3 route behaviour  → tests/contract/test_trace_graph_contract.py
  * AC3 assembly capture         → tests/unit/test_r2_0_b1_t2_retrieval_trace.py
  * AC4/AC5 export internals     → tests/unit/test_r2_0_b1_t4_evidence_export.py
  * AC6 audit write point        → tests/unit/test_r2_0_b1_t6_export_audit.py
  * All six over a live run      → tests/contract/test_r2_0_b1_t7_acceptance.py

What this file adds is the *acceptance* view: each criterion asserted as a
property of the shipped behaviour, plus the chain-integrity and out-of-window
regressions that no single subtask file owned.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
from typing import Any, Dict, List, Optional

import pytest

from app import evidence_export as ee
from app import export_audit as ea
from app import trace_graph as tg

REPORT_KEY = "rk-t7-acceptance"
RUN_ID = "run_t7"
OPP_ID = "opp_t7_001"
COMPLETED_AT = "2026-07-30T10:05:00+00:00"


# ─────────────────────────────────────────────────────────────────────────────
# Seed data — one realistic cloud_ops finding with a full provenance chain
# ─────────────────────────────────────────────────────────────────────────────


def _evidence(ev_id: str = "ev_sn_1", snippet: str = "42 incidents resolved the same way in 30 days.") -> Dict[str, Any]:
    return {
        "id": ev_id,
        "tsLabel": "30 Jul 2026, 10:00",
        "source": "ServiceNow",
        "evidenceType": "Metric",
        "title": "Recurring resolution loop",
        "snippet": snippet,
        "confidence": "HIGH",
        "provenanceType": "observed",
        "detectorId": "cloud_ops_recurring_resolution_loop",
        "packId": "cloud_ops",
    }


def _correlation_window(*, within: bool, delta: float) -> Dict[str, Any]:
    """One MSP-B7 ``WindowJoin.to_trace()['correlation_window']`` record."""
    return {
        "join_type": "event_incident",
        "window_seconds": 7200,
        "delta_seconds": delta,
        "within_window": within,
        "a_at": "2026-07-30T09:00:00+00:00",
        "b_at": "2026-07-30T09:30:00+00:00" if within else "2026-07-31T09:00:00+00:00",
    }


def _opportunity(
    *,
    evidence_ids: Optional[List[str]] = None,
    correlation_windows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "id": OPP_ID,
        "title": "Recurring resolution loop on the payments queue",
        "aiRationale": "The same manual remediation repeats across 42 incidents.",
        "tier": "Quick Win",
        "impact": 4,
        "effort": 2,
        "confidence": "HIGH",
        "decision": "UNREVIEWED",
        "evidenceIds": evidence_ids if evidence_ids is not None else ["ev_sn_1"],
        "packId": "cloud_ops",
        "packVersion": "1.2.0",
        "findingContract": {
            "corroboration": {
                "status": "corroborated",
                "sources": ["servicenow", "events"],
                "window_gated": True,
                "correlation_windows": correlation_windows or [],
            },
            "source_trace": {
                "systems": ["servicenow", "events"],
                "artifacts": [
                    {"type": "incident", "id": "INC0012345"},
                    {"type": "event_signature", "id": "v1:9ab3"},
                ],
            },
        },
    }


_POINTERS = [
    {
        "source_system": "servicenow",
        "source_artifact": "INC0012345",
        "source_timestamp": "2026-07-30T09:30:00+00:00",
        "origin": "observed",
        "detector_evidence_id": "ev_sn_1",
        "extraction_job_id": None,
        "chunk_id": None,
        "retrieval_result_id": None,
        "confidence": None,
    }
]


def _build_trace(
    *,
    correlation_windows: Optional[List[Dict[str, Any]]] = None,
    retrieval_candidates: Optional[List[Dict[str, Any]]] = None,
) -> tg.FindingTrace:
    return tg.build_finding_trace(
        _opportunity(correlation_windows=correlation_windows),
        RUN_ID,
        evidence_items=[_evidence()],
        pointers=[dict(p) for p in _POINTERS],
        run_completed_at=COMPLETED_AT,
        retrieval_candidates=retrieval_candidates or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — a complete chain terminating in source records
# ─────────────────────────────────────────────────────────────────────────────


def test_ac1_finding_expands_to_a_complete_chain():
    trace = _build_trace()
    assert trace.complete is True
    hop_types = [h.hop_type for h in trace.hops]
    assert hop_types.count(tg.HOP_FINDING) == 1
    assert tg.HOP_EVIDENCE in hop_types
    assert tg.HOP_SOURCE_RECORD in hop_types, "the chain must reach source records"


def test_ac1_every_hop_carries_origin_connector_run_id_and_timestamp():
    """The four fields an auditor needs on every hop. ``connector``/``timestamp``
    may be null where the underlying record genuinely has none, but the FIELD is
    always present and typed — never absent."""
    trace = _build_trace()
    for hop in trace.hops:
        payload = hop.to_dict()
        for key in ("hop_id", "hop_type", "origin", "connector", "run_id",
                    "timestamp", "from_hop_id", "detail"):
            assert key in payload, f"{hop.hop_id} is missing {key}"
        assert payload["origin"] in (tg.ORIGIN_OBSERVED, tg.ORIGIN_INFERRED)
        assert payload["run_id"] == RUN_ID
        assert payload["connector"] is None or isinstance(payload["connector"], str)
        assert payload["timestamp"] is None or isinstance(payload["timestamp"], str)

    # The observed leg of this seeded finding does carry a real connector and a
    # real timestamp — so the assertions above are not passing on all-nulls.
    source_hops = [h for h in trace.hops if h.hop_type == tg.HOP_SOURCE_RECORD]
    assert any(h.connector for h in source_hops)
    assert any(h.timestamp for h in source_hops)
    assert next(h for h in trace.hops if h.hop_type == tg.HOP_FINDING).timestamp == COMPLETED_AT


def test_ac1_the_chain_is_a_connected_tree_rooted_at_the_finding():
    """A chain with orphan hops would not be navigable end to end — every hop
    except the root must name a parent that actually exists in the trace."""
    trace = _build_trace()
    hop_ids = {h.hop_id for h in trace.hops}
    roots = [h for h in trace.hops if h.from_hop_id is None]
    assert len(roots) == 1 and roots[0].hop_type == tg.HOP_FINDING
    for hop in trace.hops:
        if hop is roots[0]:
            continue
        assert hop.from_hop_id in hop_ids, f"{hop.hop_id} is orphaned"


def test_ac1_chain_terminates_in_source_records_that_name_their_artifact():
    """"Terminating in source records" means the leaves ARE source records and
    each identifies the record it resolves to."""
    trace = _build_trace()
    parents = {h.from_hop_id for h in trace.hops}
    leaves = [h for h in trace.hops if h.hop_id not in parents]
    assert leaves, "a chain must have leaves"
    assert all(h.hop_type == tg.HOP_SOURCE_RECORD for h in leaves), (
        f"leaf hop types: {[h.hop_type for h in leaves]}"
    )
    for leaf in leaves:
        assert leaf.detail.get("source_artifact") or leaf.detail.get("artifact_id")


def test_ac1_a_thin_finding_degrades_instead_of_pretending_to_be_complete():
    """An opportunity with no evidence and no source_trace has no chain — the
    trace says so (``complete=False``) rather than fabricating hops."""
    trace = tg.build_finding_trace({"id": "opp_thin"}, RUN_ID)
    assert trace.complete is False
    assert trace.incomplete_reason == tg.REASON_NO_TRACE
    assert trace.has_chain is False
    assert [h.hop_type for h in trace.hops] == [tg.HOP_FINDING]


def test_ac1_a_chain_that_stops_at_evidence_is_not_reported_complete():
    """The case this AC turns on, and the one the original check missed.

    AC1 is "a complete chain TERMINATING IN SOURCE RECORDS". A finding whose
    evidence resolves to no originating record — no stored evidence pointers and
    no source_trace artifacts, which is what a run materialized before pointer
    storage produces — is a two-hop chain. ``complete`` used to be
    ``len(hops) > 1``, so that reported as complete and a reviewer had no way to
    tell missing provenance from a genuinely thin finding.
    """
    opp = {"id": "opp_2hop", "evidenceIds": ["ev_1"]}
    evidence = [{
        "id": "ev_1", "title": "Multiple low-complexity flows on a high-volume object",
        "source": "salesforce", "tsLabel": "29 Jul 2026, 15:44",
        "provenanceType": "observed",
    }]
    trace = tg.build_finding_trace(opp, RUN_ID, evidence_items=evidence, pointers=[])

    assert [h.hop_type for h in trace.hops] == [tg.HOP_FINDING, tg.HOP_EVIDENCE]
    assert trace.complete is False, "a chain that never reaches a source record is not complete"
    assert trace.incomplete_reason == tg.REASON_NO_SOURCE_RECORD


def test_ac1_an_incomplete_chain_is_still_shown_not_hidden():
    """``has_chain`` and ``complete`` answer different questions, and conflating
    them hides the chain a reviewer most needs to interrogate.

    The route serves ``available`` from ``has_chain``, so a two-hop chain still
    renders — with its incompleteness stated — rather than collapsing to
    "No source trace available yet".
    """
    opp = {"id": "opp_2hop", "evidenceIds": ["ev_1"]}
    evidence = [{"id": "ev_1", "title": "x", "source": "salesforce",
                 "tsLabel": "29 Jul 2026, 15:44"}]
    trace = tg.build_finding_trace(opp, RUN_ID, evidence_items=evidence, pointers=[])

    assert trace.has_chain is True
    assert trace.complete is False
    payload = trace.to_dict()
    assert payload["has_chain"] is True
    assert payload["incomplete_reason"] == tg.REASON_NO_SOURCE_RECORD


def test_ac1_a_chain_reaching_a_source_record_is_complete_with_no_reason():
    """The positive case: once provenance is recorded the chain terminates in a
    source record, and nothing is flagged."""
    trace = _build_trace()
    assert trace.complete is True
    assert trace.incomplete_reason is None
    assert trace.to_dict()["incomplete_reason"] is None


# ─────────────────────────────────────────────────────────────────────────────
# AC2 — join type + correlation window shown; out-of-window cannot appear
# ─────────────────────────────────────────────────────────────────────────────


def test_ac2_a_joined_claim_displays_its_join_type_and_window():
    trace = _build_trace(correlation_windows=[_correlation_window(within=True, delta=1800.0)])
    assert len(trace.joins) == 1
    join = trace.joins[0].to_dict()
    assert join["join_type"] == "event_incident"
    assert join["window_seconds"] == 7200
    assert join["delta_seconds"] == 1800.0
    assert join["within_window"] is True
    # The join is anchored to the hop it corroborates, so it is navigable.
    assert join["hop_id"] in {h.hop_id for h in trace.hops}


def test_ac2_an_out_of_window_join_cannot_appear_in_the_trace():
    trace = _build_trace(correlation_windows=[_correlation_window(within=False, delta=86400.0)])
    assert trace.joins == [], "a coincidence outside the window is not a claim"


def test_ac2_only_the_within_window_join_survives_a_mixed_set():
    trace = _build_trace(correlation_windows=[
        _correlation_window(within=True, delta=600.0),
        _correlation_window(within=False, delta=90000.0),
    ])
    assert len(trace.joins) == 1
    assert trace.joins[0].delta_seconds == 600.0
    assert all(j.within_window for j in trace.joins)


@pytest.mark.parametrize(
    "window",
    [
        {"join_type": "event_incident"},          # flag absent entirely
        {"within_window": None},
        {"within_window": False},
        {"within_window": "false"},               # truthy string — fail closed
        {"within_window": "no"},
        {"within_window": 1},                     # not a bool
        "not-a-mapping",
    ],
)
def test_ac2_only_a_literal_true_flag_admits_a_join(window):
    """Fail closed: an entry that does not POSITIVELY assert ``within_window is
    True`` is dropped. Truthiness would admit the string ``"false"`` — which a
    JSON round trip or a hand-written fixture can produce — and this layer exists
    precisely for the case where the caller is wrong."""
    trace = _build_trace(correlation_windows=[window])  # type: ignore[list-item]
    assert trace.joins == [], window


def test_ac2_the_pack_boundary_is_equally_fail_closed():
    from discovery.packs.cloud_ops_finding import build_corroboration

    corroboration = build_corroboration(
        "single_source",
        sources=["servicenow"],
        label="Single-source",
        correlation_windows=[{"join_type": "event_incident", "within_window": "false"}],
    )
    assert corroboration["correlation_windows"] == []


def test_ac2_regression_the_pack_boundary_also_drops_out_of_window_joins():
    """Two independent layers hold the guarantee: the producing pack filters
    before persistence, and trace_graph filters again on the way out."""
    from discovery.packs.cloud_ops_finding import build_corroboration

    corroboration = build_corroboration(
        "corroborated",
        sources=["servicenow", "events"],
        label="Corroborated by ServiceNow and cloud events",
        window_gated=True,
        correlation_windows=[
            _correlation_window(within=True, delta=600.0),
            _correlation_window(within=False, delta=90000.0),
        ],
    )
    kept = corroboration["correlation_windows"]
    assert len(kept) == 1
    assert kept[0]["delta_seconds"] == 600.0
    assert all(w["within_window"] for w in kept)


def test_ac2_regression_msp_b7_records_the_window_on_a_rejected_join():
    """The window layer itself must record a rejected join (auditable), while
    contributing zero confidence — coincidence never inflates confidence."""
    from discovery.correlation.windows import (
        JOIN_EVENT_INCIDENT,
        gate_operational_corroboration,
        join_within_window,
    )

    inside = join_within_window(
        {"occurred_at": "2026-07-30T09:00:00+00:00"},
        {"occurred_at": "2026-07-30T09:30:00+00:00"},
        JOIN_EVENT_INCIDENT,
    )
    outside = join_within_window(
        {"occurred_at": "2026-07-30T09:00:00+00:00"},
        {"occurred_at": "2026-08-30T09:00:00+00:00"},
        JOIN_EVENT_INCIDENT,
    )
    assert inside.within is True
    assert outside.within is False
    # Both are recorded with their window + delta — the rejection is auditable.
    for join in (inside, outside):
        fragment = join.to_trace()["correlation_window"]
        assert fragment["join_type"] == JOIN_EVENT_INCIDENT
        assert fragment["window_seconds"] > 0
        assert fragment["delta_seconds"] is not None

    elevated = gate_operational_corroboration(
        {"occurred_at": "2026-07-30T09:00:00+00:00"},
        {"occurred_at": "2026-07-30T09:30:00+00:00"},
    )
    not_elevated = gate_operational_corroboration(
        {"occurred_at": "2026-07-30T09:00:00+00:00"},
        {"occurred_at": "2026-08-30T09:00:00+00:00"},
    )
    assert elevated.elevates is True
    assert not_elevated.elevates is False


# ─────────────────────────────────────────────────────────────────────────────
# AC3 — retrieval candidates used AND not used are both visible
# ─────────────────────────────────────────────────────────────────────────────


def _retrieval_candidate(chunk_id: str, *, confidence: float, is_stale: bool = False) -> Dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "origin": "observed",
        "confidence": confidence,
        "similarity": confidence,
        "source_system": "confluence",
        "source_artifact": f"page-{chunk_id}",
        "content": f"retrieved content for {chunk_id}",
        "is_stale": is_stale,
    }


def test_ac3_assembly_records_every_proposed_candidate_used_or_not():
    """Retrieval proposes, assembly decides — and BOTH sides are recorded, each
    with the decision and the reason for it."""
    from app.context_assembly import AssemblyPolicy
    from app.retrieval_trace import assemble_evidence_candidates_for_opportunity

    proposed = [
        _retrieval_candidate(f"chunk_{i:02d}", confidence=1.0 - (i * 0.04))
        for i in range(14)
    ]

    def factory(_org_id: str):
        def source(_opportunity, _policy=None):
            return list(proposed)
        return source

    records = assemble_evidence_candidates_for_opportunity(
        "org_a", _opportunity(), evidence_source_factory=factory
    )

    assert len(records) == len(proposed), "every proposal gets exactly one record"
    used = [r for r in records if r["used"]]
    unused = [r for r in records if not r["used"]]
    assert used and unused, "the seeded set must exercise both sides"
    assert len(used) <= AssemblyPolicy().max_evidence_chunks
    for record in records:
        assert record["decision"] in ("included", "excluded")
        assert record["reason"], "an unexplained decision is not transparency"
        # Enough provenance to identify WHICH chunk was (not) used.
        assert record["source_system"] == "confluence"
        assert record["source_artifact"]


def test_ac3_the_trace_surfaces_used_and_unused_with_matching_counts():
    stored = [
        {"chunk_id": "c1", "used": True, "decision": "included",
         "reason": "included@position_1", "confidence": 0.93, "origin": "observed",
         "source_system": "confluence", "source_artifact": "page-42",
         "content_snippet": "runbook step 3", "is_stale": False},
        {"chunk_id": "c2", "used": False, "decision": "excluded",
         "reason": "below_confidence_floor", "confidence": 0.01, "origin": "observed",
         "source_system": "git", "source_artifact": "README.md",
         "content_snippet": "unrelated", "is_stale": False},
        {"chunk_id": "c3", "used": False, "decision": "excluded",
         "reason": "stale", "confidence": 0.88, "origin": "observed",
         "source_system": "sharepoint", "source_artifact": "policy.docx",
         "content_snippet": "outdated", "is_stale": True},
    ]
    payload = _build_trace(retrieval_candidates=stored).to_dict()

    assert payload["retrieval_candidates_used_count"] == 1
    assert payload["retrieval_candidates_unused_count"] == 2
    assert (
        payload["retrieval_candidates_used_count"]
        + payload["retrieval_candidates_unused_count"]
        == len(payload["retrieval_candidates"])
    )
    by_id = {c["chunk_id"]: c for c in payload["retrieval_candidates"]}
    assert by_id["c1"]["used"] is True
    assert by_id["c2"]["used"] is False and by_id["c2"]["reason"] == "below_confidence_floor"
    assert by_id["c3"]["is_stale"] is True, "a freshness exclusion stays visible"


def test_ac3_an_unused_candidate_is_never_silently_dropped_from_the_trace():
    """The failure mode this AC exists to prevent: showing only what was used."""
    stored = [
        {"chunk_id": f"c{i}", "used": False, "decision": "excluded",
         "reason": "ranked_out", "confidence": 0.4, "origin": "observed",
         "source_system": "slack", "source_artifact": f"thread-{i}",
         "content_snippet": "chatter", "is_stale": False}
        for i in range(5)
    ]
    payload = _build_trace(retrieval_candidates=stored).to_dict()
    assert len(payload["retrieval_candidates"]) == 5
    assert payload["retrieval_candidates_used_count"] == 0
    assert payload["retrieval_candidates_unused_count"] == 5


# ─────────────────────────────────────────────────────────────────────────────
# Export fixture (AC4 / AC5 / AC6)
# ─────────────────────────────────────────────────────────────────────────────


class _FakeTrace:
    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._payload)


@pytest.fixture
def exportable(monkeypatch):
    """A signable run: one finding, its evidence, its trace and pointers, the
    run-level artifacts, and a license that yields a report_key."""
    kv: Dict[str, Any] = {
        "opps": [_opportunity()],
        "evidence": [_evidence()],
        "executive_report": {"confidence": "HIGH", "topQuickWins": [OPP_ID]},
        "roadmap": {"phases": [{"name": "Phase 1", "opportunityIds": [OPP_ID]}]},
        "audit": [{"id": "a1", "action": "APPROVED", "by": "analyst@example.com"}],
    }
    run = {
        "id": RUN_ID,
        "org_id": "org_a",
        "startedAt": "2026-07-30T10:00:00+00:00",
        "completedAt": COMPLETED_AT,
        "mode": "offline",
        "packId": "cloud_ops",
        "packVersion": "1.2.0",
    }
    monkeypatch.setattr(ee.db, "get_run", lambda rid: run if rid == RUN_ID else None)
    monkeypatch.setattr(
        ee.db, "run_kv_get", lambda key, rid, default=None: kv.get(key, default)
    )
    monkeypatch.setattr(
        "app.trace_graph.load_finding_trace",
        lambda rid, oid: _FakeTrace(_build_trace().to_dict()),
    )
    monkeypatch.setattr(
        "app.evidence_pointers.get_evidence_pointers_for_opportunity",
        lambda rid, oid: [dict(p) for p in _POINTERS],
    )
    monkeypatch.setattr(
        "app.usage_report._resolve_license_signing",
        lambda org_id: (REPORT_KEY, "cf-2026-1", org_id),
    )
    return kv


# ─────────────────────────────────────────────────────────────────────────────
# AC4 — the bundle verifies; any altered byte fails
# ─────────────────────────────────────────────────────────────────────────────


def test_ac4_finding_and_report_bundles_verify(exportable):
    for scope, opp in ((ee.SCOPE_FINDING, OPP_ID), (ee.SCOPE_REPORT, None)):
        envelope = ee.generate_signed_export("org_a", RUN_ID, scope=scope, opp_id=opp)
        assert envelope["algorithm"] == ee.SIGNATURE_ALGORITHM
        verdict = ee.verify_export_envelope(envelope, REPORT_KEY)
        assert verdict["verified"] is True, (scope, verdict)


def test_ac4_the_exported_bundle_carries_the_trace_and_the_run_pack_versions(exportable):
    """The bundle is only auditable if it contains the chain and the versions
    that produced it — AC1's chain, frozen into the signed artifact."""
    envelope = ee.generate_signed_export(
        "org_a", RUN_ID, scope=ee.SCOPE_FINDING, opp_id=OPP_ID
    )
    body = envelope["bundle"]
    section = body["findings"][0]
    assert section["opportunity_id"] == OPP_ID
    assert section["trace"]["hop_count"] >= 2
    assert section["evidence"] and section["evidence_pointers"]
    assert body["run_provenance"]["pack_version"] == "1.2.0"


def test_ac4_altering_any_byte_fails_verification(exportable):
    envelope = ee.generate_signed_export(
        "org_a", RUN_ID, scope=ee.SCOPE_FINDING, opp_id=OPP_ID
    )
    raw = ee.envelope_bytes(envelope)
    assert ee.verify_export_bytes(raw, REPORT_KEY)["verified"] is True

    # Every byte position matters: walk a sample of positions across the whole
    # serialised bundle and flip one byte at each.
    step = max(1, len(raw) // 40)
    for index in range(0, len(raw), step):
        flipped = bytearray(raw)
        flipped[index] = (flipped[index] + 1) % 256
        if bytes(flipped) == raw:
            continue
        verdict = ee.verify_export_bytes(bytes(flipped), REPORT_KEY)
        assert verdict["verified"] is False, f"a byte flip at {index} must not verify"


def test_ac4_semantic_tampering_and_a_wrong_key_both_fail(exportable):
    envelope = ee.generate_signed_export(
        "org_a", RUN_ID, scope=ee.SCOPE_FINDING, opp_id=OPP_ID
    )
    understated = copy.deepcopy(envelope)
    understated["bundle"]["findings"][0]["evidence"][0]["snippet"] = "1 incident."
    assert ee.verify_export_envelope(understated, REPORT_KEY)["verified"] is False
    assert ee.verify_export_envelope(envelope, "rk-not-this-install")["verified"] is False


def test_ac4_a_bundle_is_reproducible_for_a_fixed_generation_time(exportable):
    kwargs = dict(
        scope=ee.SCOPE_FINDING, opp_id=OPP_ID, generated_at="2026-07-30T11:00:00+00:00"
    )
    first = ee.generate_signed_export("org_a", RUN_ID, **kwargs)
    second = ee.generate_signed_export("org_a", RUN_ID, **kwargs)
    assert first["signature"] == second["signature"]
    # A JSON round trip (which loses key ordering) still verifies.
    assert ee.verify_export_envelope(json.loads(json.dumps(first)), REPORT_KEY)["verified"] is True


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — no unredacted secrets, no host x vulnerability enumeration in exports
# ─────────────────────────────────────────────────────────────────────────────


SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def test_ac5_a_secret_in_evidence_is_redacted_before_it_is_signed(exportable):
    exportable["evidence"] = [_evidence(snippet=f"42 incidents; token {SECRET}")]
    body = ee.build_export_bundle("org_a", RUN_ID, scope=ee.SCOPE_FINDING, opp_id=OPP_ID)
    serialised = json.dumps(body)
    assert SECRET not in serialised
    assert "REDACTED" in serialised
    # The redaction is declared on the bundle, not silent.
    assert body["redacted_pattern_types"]


def test_ac5_a_secret_anywhere_in_the_report_bundle_is_redacted(exportable):
    """Report scope carries the narrative artifacts too — the sweep is recursive,
    so a secret in the executive report is redacted just the same."""
    exportable["executive_report"] = {
        "confidence": "HIGH",
        "narrative": f"Integration used token {SECRET} during the run.",
    }
    body = ee.build_export_bundle("org_a", RUN_ID, scope=ee.SCOPE_REPORT)
    assert SECRET not in json.dumps(body)


def test_ac5_what_is_signed_is_the_redacted_form(exportable):
    """A signature over the UNREDACTED form would mean the artifact an auditor
    verifies is not the artifact they were handed."""
    exportable["evidence"] = [_evidence(snippet=f"token {SECRET}")]
    envelope = ee.generate_signed_export(
        "org_a", RUN_ID, scope=ee.SCOPE_FINDING, opp_id=OPP_ID
    )
    assert SECRET not in json.dumps(envelope)
    assert ee.verify_export_envelope(envelope, REPORT_KEY)["verified"] is True


@pytest.mark.parametrize(
    "enumerating_snippet",
    [
        "Host 10.1.2.3 is affected by CVE-2026-1234.",
        "server-prod-01 (192.168.4.4) — CVE-2025-9999 unpatched",
    ],
)
def test_ac5_an_enumerating_bundle_is_refused_not_caveated(exportable, enumerating_snippet):
    """The 1.9 aggregation floor holds in export: a bundle that would double as a
    host x vulnerability target list is refused loudly."""
    exportable["evidence"] = [_evidence(snippet=enumerating_snippet)]
    with pytest.raises(ee.EvidenceExportError, match="aggregation floor"):
        ee.build_export_bundle("org_a", RUN_ID, scope=ee.SCOPE_FINDING, opp_id=OPP_ID)


def test_ac5_the_floor_also_holds_for_report_scope(exportable):
    exportable["executive_report"] = {
        "narrative": "Host 10.1.2.3 is affected by CVE-2026-1234."
    }
    with pytest.raises(ee.EvidenceExportError, match="aggregation floor"):
        ee.build_export_bundle("org_a", RUN_ID, scope=ee.SCOPE_REPORT)


def test_ac5_a_refused_export_produces_no_signed_artifact(exportable):
    exportable["evidence"] = [
        _evidence(snippet="Host 10.1.2.3 is affected by CVE-2026-1234.")
    ]
    with pytest.raises(ee.EvidenceExportError):
        ee.generate_signed_export("org_a", RUN_ID, scope=ee.SCOPE_FINDING, opp_id=OPP_ID)


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — every export generation is an audit event naming user, scope, and time
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def audited(monkeypatch):
    """Capture what the audit + telemetry write points receive."""
    calls: Dict[str, List[Any]] = {"audit": [], "telemetry": []}
    monkeypatch.setattr(
        "app.middleware.audit.log_event",
        lambda et, **kw: calls["audit"].append((et, kw)),
    )
    monkeypatch.setattr(
        "app.telemetry.record_event",
        lambda et, payload=None: calls["telemetry"].append((et, payload)),
    )
    return calls


def _assert_names_user_scope_and_time(kwargs: Dict[str, Any], *, expected_scope: str) -> None:
    assert kwargs["user_id"], "an export must name its user"
    assert kwargs["user_id"] != ea.UNATTRIBUTED_ACTOR
    assert kwargs["scope"] == expected_scope
    parsed = dt.datetime.fromisoformat(kwargs["timestamp"])
    assert parsed.tzinfo is not None and parsed.utcoffset() == dt.timedelta(0)


def test_ac6_finding_export_through_the_route_is_audited(exportable, audited, monkeypatch):
    from app import routes_evidence_export as routes

    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")
    monkeypatch.setattr(routes.db, "run_get", lambda rid: {"id": rid, "org_id": "org_a"})

    envelope = routes.get_finding_evidence_export(
        RUN_ID, OPP_ID, download=False, token="analyst-token"
    )
    assert envelope["signature"]

    assert len(audited["audit"]) == 1
    event_type, kwargs = audited["audit"][0]
    assert event_type == "evidence_export_generated"
    _assert_names_user_scope_and_time(kwargs, expected_scope="finding")
    assert kwargs["user_id"] == "analyst-token"
    assert kwargs["org_id"] == "org_a"
    # Identifies WHICH artifact was issued, without carrying its content.
    assert kwargs["run_id"] == RUN_ID
    assert kwargs["opportunity_id"] == OPP_ID
    assert kwargs["content_root"] == envelope["bundle"]["integrity"]["content_root"]
    assert envelope["signature"] not in str(kwargs)


def test_ac6_report_export_through_the_route_is_audited(exportable, audited, monkeypatch):
    from app import routes_evidence_export as routes

    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")
    monkeypatch.setattr(routes.db, "run_get", lambda rid: {"id": rid, "org_id": "org_a"})

    routes.get_report_evidence_export(RUN_ID, download=False, token="analyst-token")
    event_type, kwargs = audited["audit"][0]
    assert event_type == "evidence_export_generated"
    _assert_names_user_scope_and_time(kwargs, expected_scope="report")


def test_ac6_the_download_form_is_audited_too(exportable, audited, monkeypatch):
    """The attachment is the artifact that actually leaves the deployment — it
    must not be the unaudited path."""
    from app import routes_evidence_export as routes

    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")
    monkeypatch.setattr(routes.db, "run_get", lambda rid: {"id": rid, "org_id": "org_a"})

    response = routes.get_finding_evidence_export(
        RUN_ID, OPP_ID, download=True, token="analyst-token"
    )
    assert "attachment" in response.headers["content-disposition"]
    assert len(audited["audit"]) == 1
    _assert_names_user_scope_and_time(audited["audit"][0][1], expected_scope="finding")


def test_ac6_a_refused_export_records_nothing(exportable, audited, monkeypatch):
    """No artifact left the deployment, so there is nothing to attest to — an
    audit trail of non-events is noise, and a 400 must not look like an export."""
    from fastapi import HTTPException

    from app import routes_evidence_export as routes

    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")
    monkeypatch.setattr(routes.db, "run_get", lambda rid: {"id": rid, "org_id": "org_a"})
    exportable["evidence"] = [
        _evidence(snippet="Host 10.1.2.3 is affected by CVE-2026-1234.")
    ]

    with pytest.raises(HTTPException) as exc:
        routes.get_finding_evidence_export(
            RUN_ID, OPP_ID, download=False, token="analyst-token"
        )
    assert exc.value.status_code == 400
    assert audited["audit"] == []


def test_ac6_the_usage_report_export_is_audited(audited, monkeypatch):
    """Every export generation — the signed usage report is one, on the same
    trust model, and previously recorded nothing at all."""
    from app import routes_usage_report as routes

    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")
    monkeypatch.setattr(
        routes,
        "generate_signed_report",
        lambda org_id, f, t: {
            "report": {
                "period": {"from": f, "to": t},
                "runs": {"total": 3},
                "event_count": 7,
                "generated_at": "2026-07-30T11:00:00+00:00",
            },
            "signature": "b" * 64,
            "algorithm": "HMAC-SHA256",
        },
    )

    routes.get_usage_report(from_="2026-07-01", to="2026-07-31", token="owner-token")

    assert len(audited["audit"]) == 1
    event_type, kwargs = audited["audit"][0]
    assert event_type == "usage_report_exported"
    _assert_names_user_scope_and_time(kwargs, expected_scope="usage_report")
    assert kwargs["period_from"] == "2026-07-01"
    assert kwargs["period_to"] == "2026-07-31"
    assert kwargs["run_count"] == 3
    assert kwargs["signature_prefix"] == "b" * 16
    assert "b" * 64 not in str(kwargs)   # never the whole MAC


def test_ac6_an_unsignable_usage_report_records_nothing(audited, monkeypatch):
    from fastapi import HTTPException

    from app import routes_usage_report as routes
    from app.usage_report import UsageReportError

    monkeypatch.setattr(routes, "get_current_org_id", lambda: "org_a")

    def _no_key(org_id, f, t):
        raise UsageReportError("the installed license carries no report_key")

    monkeypatch.setattr(routes, "generate_signed_report", _no_key)
    with pytest.raises(HTTPException) as exc:
        routes.get_usage_report(from_="2026-07-01", to="2026-07-31", token="owner-token")
    assert exc.value.status_code == 400
    assert audited["audit"] == []


def test_ac6_every_export_surface_records_through_the_one_write_point():
    """A new export endpoint must register as an audited surface — otherwise it
    ships unaudited by omission, which is exactly how AC6 rots."""
    from app.routes_evidence_export import FINDING_EXPORT_PATH, REPORT_EXPORT_PATH
    from app.routes_usage_report import USAGE_REPORT_PATH

    for path in (FINDING_EXPORT_PATH, REPORT_EXPORT_PATH, USAGE_REPORT_PATH):
        assert path in ea.EXPORT_AUDIT_SURFACES, path
    for kind in ea.EXPORT_AUDIT_SURFACES.values():
        assert kind in ea.VALID_EXPORT_KINDS
