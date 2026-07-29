"""MSP-B8 / T6 — bridge ⇔ native equivalence harness.

This is the pack's proof of its own foundation (MSP-B8 Architectural Note "The
contract's first proof"): a cloud event ingested through the export **bridge**
must be detector-equivalent to the same event ingested through a **native**
connector — identical normalised output except for the intentional
``source_system='bridge:<provider>'`` prefix. If the two paths ever diverge, the
mapper contract has drifted, and it is far cheaper to catch that here than after
the L-sized native B1/B2 connectors inherit the same assumptions.

Two paths, one corpus
---------------------
Each MSP-B0 golden fixture is executed through BOTH paths:

  * **Direct mapper path** — the reference mapper is invoked on the raw payload
    exactly as a native connector would (``map_cloudwatch(raw, org_id=...)``), and
    the raw payload is stored against the event's evidence pointer.
  * **Staged bridge path** — the raw payload is staged in ``ops_event_staging``
    and drained through :class:`OpsEventBridgeIngestor`, which routes it to the
    SAME mapper by ``(provider, source_format)`` and re-stamps
    ``source_system='bridge:<provider>'``.

The two resulting normalised events are compared field by field. The ONLY
detector-visible field allowed to differ is ``source_system``; every other field —
event type, timestamp, resource identity, severity/state, signal id, normalised
payload, and the recurrence ``event_signature`` — must be identical. Evidence is
verified separately for **stable resolution**: both paths' evidence pointers must
resolve back to the identical raw payload (the "Done" bar).

Useful diffs
------------
When a case fails, :class:`EquivalenceResult` carries the exact
``(field, native_value, bridge_value)`` tuples that differed, and
:func:`format_report` renders them — so mapper-contract drift is diagnosable at a
glance rather than as an opaque assertion failure.

Provider-injectable: pass an ``InMemoryStagingSink`` (as both sink and reader) for
a DB-free run, or ``DbStagingSink()`` + ``DbStagingReader()`` for the on-DB run.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from database.models.ops_event_staging import OpsEventStagingRow
from discovery.signals.evidence_store import (
    InMemoryRawEventStore,
    resolve_raw_event,
    store_raw_event,
)
from discovery.signals.reference_mappers import MAPPERS

from .ops_event_bridge import OpsEventBridgeIngestor, resolve_raw_payload
from .ops_event_staging_store import InMemoryStagingSink

#: Golden-fixture mapper name → the staging ``(provider, source_format)`` that the
#: bridge routes to the SAME mapper. The inverse of the bridge's mapper registry;
#: keeping it here (not importing the private registry) documents the routing the
#: equivalence proof depends on.
MAPPER_TO_STAGING: Dict[str, tuple] = {
    "map_cloudwatch": ("aws", "cloudwatch_alarm_history"),
    "map_eventbridge": ("aws", "eventbridge_archive"),
    "map_cloudtrail": ("aws", "cloudtrail"),
    "map_azure_monitor": ("azure", "azure_monitor"),
    "map_azure_activity_log": ("azure", "azure_activity_log"),
}

#: The one detector-visible field the bridge intentionally changes.
EXPECTED_DIFF_FIELD = "source_system"

#: Evidence metadata is transport-specific (the bridge points at the STAGED
#: payload, the native path at the live artifact), so it is not part of the
#: normalised field-by-field comparison — it is verified via STABLE RESOLUTION
#: instead (both pointers resolve to the identical raw payload).
_PROVENANCE_FIELD = "provenance"

_GOLDEN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "tests", "fixtures",
    "msp_provider_mapping_golden.json",
)


def load_golden_cases(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load the MSP-B0 golden mapping fixtures (the shared corpus)."""
    with open(path or _GOLDEN_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)["cases"]


@dataclass
class FieldDiff:
    field: str
    native_value: Any
    bridge_value: Any


@dataclass
class EquivalenceResult:
    """The outcome of proving one golden case equivalent across both paths."""

    case_name: str
    provider: str
    mapper: str
    passed: bool = False
    # normalised field-by-field comparison
    unexpected_diffs: List[FieldDiff] = field(default_factory=list)
    source_system_native: str = ""
    source_system_bridge: str = ""
    source_system_differs_as_expected: bool = False
    # evidence (stable resolution)
    evidence_resolves_native: bool = False
    evidence_resolves_bridge: bool = False
    evidence_stable: bool = False
    # diagnostics
    bridge_record_found: bool = True
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_name": self.case_name,
            "provider": self.provider,
            "mapper": self.mapper,
            "passed": self.passed,
            "unexpected_diffs": [vars(d) for d in self.unexpected_diffs],
            "source_system_native": self.source_system_native,
            "source_system_bridge": self.source_system_bridge,
            "source_system_differs_as_expected": self.source_system_differs_as_expected,
            "evidence_resolves_native": self.evidence_resolves_native,
            "evidence_resolves_bridge": self.evidence_resolves_bridge,
            "evidence_stable": self.evidence_stable,
            "bridge_record_found": self.bridge_record_found,
            "message": self.message,
        }


def _case_event_id(case: Dict[str, Any]) -> str:
    """A unique, stable staging ``provider_event_id`` for a golden case.

    The case name is unique across the corpus and stable, so it is a clean staging
    dedupe / evidence key — the equivalence proof does not require it to equal the
    mapper's ``signal_id`` (that field is compared directly on the event).
    """
    return f"golden:{case['name']}"


def run_equivalence(
    cases: List[Dict[str, Any]],
    *,
    org_id: str = "equiv_org",
    sink=None,
    reader=None,
) -> List[EquivalenceResult]:
    """Prove each golden case equivalent across the direct and bridge paths.

    ``sink``/``reader`` default to a single shared :class:`InMemoryStagingSink`
    (DB-free). For an on-DB proof pass ``DbStagingSink()`` + ``DbStagingReader()``.
    """
    if sink is None and reader is None:
        shared = InMemoryStagingSink()
        sink = reader = shared
    if sink is None or reader is None:
        raise ValueError("pass both sink and reader, or neither")

    # ── Stage every golden raw payload for the bridge path ──────────────────
    rows: List[OpsEventStagingRow] = []
    for case in cases:
        provider, source_format = MAPPER_TO_STAGING[case["mapper"]]
        rows.append(OpsEventStagingRow(
            org_id=org_id,
            provider=provider,
            source_format=source_format,
            batch_id=f"golden:{provider}",
            provider_event_id=_case_event_id(case),
            raw=case["raw"],
        ))
    sink.insert_rows(rows)

    # ── Drain the bridge once; index emitted records by provider_event_id ───
    bridge_store = InMemoryRawEventStore()
    ingestor = OpsEventBridgeIngestor(reader, raw_store=bridge_store, batch_size=1000)
    bridge_recs: Dict[str, Dict[str, Any]] = {}
    for batch in ingestor.ingest_changes(org_id, None):
        for rec in batch.records:
            bridge_recs[rec["provider_event_id"]] = rec

    # ── Compare each case across both paths ─────────────────────────────────
    native_store = InMemoryRawEventStore()
    results: List[EquivalenceResult] = []
    for case in cases:
        provider, _ = MAPPER_TO_STAGING[case["mapper"]]
        native_event = MAPPERS[case["mapper"]](case["raw"], org_id=org_id)
        store_raw_event(native_store, org_id, native_event, case["raw"])
        rec = bridge_recs.get(_case_event_id(case))
        results.append(_compare_case(
            case, provider, native_event, native_store, rec, bridge_store, org_id
        ))
    return results


def _compare_case(case, provider, native_event, native_store, rec, bridge_store, org_id) -> EquivalenceResult:
    result = EquivalenceResult(
        case_name=case["name"], provider=provider, mapper=case["mapper"],
    )
    if rec is None:
        result.bridge_record_found = False
        result.message = "bridge emitted no event for this case"
        return result

    native_d = native_event.to_dict()
    bridge_d = rec["event"]

    # Field-by-field over the union of keys, EXCLUDING transport-specific
    # provenance (verified via stable resolution instead).
    for f in sorted(set(native_d) | set(bridge_d)):
        if f == _PROVENANCE_FIELD:
            continue
        if native_d.get(f) != bridge_d.get(f):
            if f == EXPECTED_DIFF_FIELD:
                continue  # the one intentional difference
            result.unexpected_diffs.append(FieldDiff(f, native_d.get(f), bridge_d.get(f)))

    result.source_system_native = native_d.get(EXPECTED_DIFF_FIELD, "")
    result.source_system_bridge = bridge_d.get(EXPECTED_DIFF_FIELD, "")
    result.source_system_differs_as_expected = (
        result.source_system_native != result.source_system_bridge
        and result.source_system_bridge == f"bridge:{provider}"
    )

    # Evidence: both pointers must resolve back to the IDENTICAL raw payload.
    native_raw = resolve_raw_event(native_store, org_id, native_event)
    bridge_raw = resolve_raw_payload(bridge_store, org_id, rec)
    result.evidence_resolves_native = native_raw == case["raw"]
    result.evidence_resolves_bridge = bridge_raw == case["raw"]
    result.evidence_stable = (
        native_raw is not None and native_raw == bridge_raw == case["raw"]
    )

    result.passed = (
        not result.unexpected_diffs
        and result.source_system_differs_as_expected
        and result.evidence_stable
    )
    if result.passed:
        result.message = "equivalent (only source_system differs; evidence stable)"
    else:
        parts = []
        if result.unexpected_diffs:
            parts.append(f"{len(result.unexpected_diffs)} unexpected field diff(s)")
        if not result.source_system_differs_as_expected:
            parts.append("source_system prefix wrong")
        if not result.evidence_stable:
            parts.append("evidence did not resolve identically")
        result.message = "; ".join(parts)
    return result


def all_passed(results: List[EquivalenceResult]) -> bool:
    return bool(results) and all(r.passed for r in results)


def format_report(results: List[EquivalenceResult]) -> str:
    """Render a human-readable equivalence report with useful drift diffs."""
    lines = ["MSP-B8 T6 -- bridge <-> native equivalence", ""]
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        lines.append(
            f"[{status}] {r.case_name} ({r.provider}/{r.mapper}) -- "
            f"{r.source_system_native} vs {r.source_system_bridge}"
        )
        if not r.passed:
            lines.append(f"       reason: {r.message}")
            for d in r.unexpected_diffs:
                lines.append(
                    f"       DIFF {d.field}: native={d.native_value!r} "
                    f"bridge={d.bridge_value!r}"
                )
    passed = sum(1 for r in results if r.passed)
    lines += ["", f"{passed}/{len(results)} cases equivalent"]
    return "\n".join(lines)
