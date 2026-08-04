"""Dump the Azure ``event_signature`` values the connector produces — read-only.

Why this script exists alongside the API endpoint
-------------------------------------------------
``GET /api/runs/{runId}/cloud-ops/event-signatures`` serves the rows a run
recorded — but only for runs executed after that write existed, and a run whose
Azure checkpoint is already caught up ingests 0 new events and therefore records
0 rows. This script closes that gap: it re-polls Azure from the beginning of each
stream's available window and prints the signatures the mappers derive, WITHOUT
running discovery and WITHOUT touching stored state.

What it does NOT do — the safety contract
-----------------------------------------
* **Never writes a checkpoint.** Only ``change_runner.ingest_with_checkpoint``
  persists one, and this script does not use it. It calls
  ``AzureEventIngestor.ingest_all(checkpoint=None)`` directly, so the stored
  ``(org, "azure_events")`` position is neither read nor advanced — the next real
  discovery run resumes exactly where it would have.
* **Never writes to the database**, never creates a run, never emits telemetry.
* **Never invents a value.** Every signature printed is computed by the real
  ``map_azure_*`` mapper → ``OperationalEvent.build`` →
  ``compute_event_signature`` path the pipeline uses.

Output per DISTINCT signature: the value, how many raw events folded onto it
(``recurring`` = >1), the surface + subscription, ``event_class`` /
``resource_type`` / ``event_type`` / ``resource_id``, the first/last-seen window,
and the exact components ``compute_event_signature`` hashed (via
``signature_components``) so a mismatch is explainable rather than mysterious.

Usage
-----
    cd backend
    # live Azure re-poll (same env/config a real run uses)
    INGEST_MODE=live python scripts/dump_azure_event_signatures.py --org-id <org>

    # with copy-paste ServiceNow incident bodies, plus JSON for scripting
    INGEST_MODE=live python scripts/dump_azure_event_signatures.py --org-id <org> \
        --servicenow-payloads --json azure_sigs.json

    # offline fixture run (no Azure account): AzureEventIngestor.ingest_all always
    # resolves a token even offline, where the fixture clients ignore it, so pass a
    # placeholder when no service principal is vaulted.
    python scripts/dump_azure_event_signatures.py --org-id <org> --token offline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from typing import Any, Dict, List, Optional

# Allow `python scripts/dump_azure_event_signatures.py` from backend/.
_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND)

# Load backend/.env BEFORE any discovery import. `is_live()` is read at two
# different moments — once when the config resolves (which subscriptions to poll)
# and again when each stream client is built (fixture vs live HTTP). Without this,
# INGEST_MODE is absent for the first and present for the second (app/db.py calls
# load_dotenv() on import, pulled in via the vault during token resolution), so the
# script polls the offline FIXTURE's placeholder subscriptions with the LIVE client
# and every request 404s — reporting 0 signatures for a healthy subscription.
try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(os.path.join(_BACKEND, ".env"))
except Exception:  # pragma: no cover - dotenv absent is not fatal
    pass

from discovery.cloud_ops_runtime import build_cloud_ops_runtime  # noqa: E402
from discovery.ingest.azure_events import build_ingestor  # noqa: E402
from discovery.signals.event_signature import (  # noqa: E402
    EVENT_SIGNATURE_VERSION,
    signature_components,
)

#: The ServiceNow column the incident-side link would be read from. Kept as a
#: parameter because no such read exists in the ingest layer yet (see the analysis
#: accompanying this script) — the emitted payloads are therefore a specification
#: of what to stamp, not proof that the backend reads it today.
DEFAULT_SIGNATURE_COLUMN = "u_event_signature"


def _collect(org_id: str, *, token: Optional[str] = None) -> List[Dict[str, Any]]:
    """Re-poll every Azure stream from the beginning; return the delta records.

    ``token`` bypasses ARM token acquisition. ``ingest_all`` calls
    ``_resolve_token`` unconditionally — including offline, where the fixture
    clients ignore the token entirely — so an offline dry run with no vaulted
    service principal must pass a placeholder.
    """
    ingestor = build_ingestor(org_id)
    if ingestor is None:
        raise SystemExit(
            f"Azure event connector is not configured for org {org_id!r} "
            "(no pinned subscriptions / no AZURE_EVENT_CONFIG): "
            "resolve_azure_event_config returned None."
        )
    # checkpoint=None => ignore the STORED per-subscription positions and read the
    # full available window. Nothing is written back.
    result = ingestor.ingest_all(checkpoint=None, token=token)
    for scope, status in sorted((result.subscription_status or {}).items()):
        print(f"# {scope}: {status}", file=sys.stderr)
    return list(result.records or ())


def _row(record: Dict[str, Any]) -> Dict[str, Any]:
    event = record.get("event") or {}
    resource = event.get("resource") or {}
    payload = event.get("payload") or {}
    return {
        "event_signature": record.get("event_signature") or event.get("event_signature"),
        "surface": record.get("surface"),
        "subscription_id": record.get("account_scope"),
        "source_system": event.get("source_system"),
        "event_class": event.get("event_class"),
        "resource_type": event.get("resource_type"),
        "event_type": event.get("event_type"),
        "severity": event.get("severity"),
        "resource_id": resource.get("resource_id") or "",
        "observed_at": event.get("observed_at"),
        "signal_id": event.get("signal_id"),
        "components": signature_components(
            source_system=str(event.get("source_system") or ""),
            event_class=str(event.get("event_class") or ""),
            resource_type=str(event.get("resource_type") or ""),
            event_type=str(event.get("event_type") or ""),
            resource_id=resource.get("resource_id"),
            principal=payload.get("caller") or payload.get("principal"),
        ),
    }


def _fold(records: List[Dict[str, Any]]) -> "OrderedDict[str, Dict[str, Any]]":
    """One entry per DISTINCT signature — the same fold ``OpsEventStream`` performs.

    ``occurrences`` counts the raw provider events that collapsed onto the
    signature; ``occurrences > 1`` is the ``recurring`` flag
    ``cloud_ops_recurring_resolution_loop`` requires.
    """
    folded: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for record in records:
        row = _row(record)
        signature = row["event_signature"]
        if not signature:
            continue
        entry = folded.get(signature)
        if entry is None:
            entry = dict(row)
            entry["occurrences"] = 0
            entry["first_seen"] = row["observed_at"]
            entry["last_seen"] = row["observed_at"]
            folded[signature] = entry
        entry["occurrences"] += 1
        observed = row["observed_at"] or ""
        if observed and (not entry["first_seen"] or observed < entry["first_seen"]):
            entry["first_seen"] = observed
        if observed and (not entry["last_seen"] or observed > entry["last_seen"]):
            entry["last_seen"] = observed
    return folded


def _persisted_rows(org_id: str, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The EXACT ``cloud_ops.event_signatures`` rows a run would persist.

    Runs the real production assembly (``build_cloud_ops_runtime`` →
    ``_aggregate_event_signatures`` → ``OpsEventStream`` fold → noise floors) over the
    polled records, so these are the same dicts ``runner._persist_cloud_ops_event_
    signatures`` writes to run-KV ``cloud_ops_event_signatures`` and the endpoint
    returns — not a re-derivation. ``sn_data`` is empty here, so incident-side fields
    (``incident_ids``, ``window_overlap``, TTR, close codes) are correctly empty:
    those only populate once a ServiceNow incident carries the signature.
    """
    runtime = build_cloud_ops_runtime(org_id, {"org_id": org_id}, bridge_records=records)
    return [
        row
        for row in (runtime.block.get("event_signatures") or [])
        if isinstance(row, dict)
    ]


def _servicenow_payload(
    entry: Dict[str, Any],
    *,
    column: str,
    assignment_group: str,
    close_code: str,
) -> Dict[str, Any]:
    """A ServiceNow incident body shaped for the cloud-ops join.

    Every key is read by a named backend site (see the accompanying analysis);
    none is decorative. Timestamp fields are placeholders because only the
    operator knows the target instance's clock — they must land inside the 2h
    ``event_incident`` correlation window around ``first_seen``.
    """
    resource = entry.get("resource_id") or "unknown-resource"
    return {
        # The event<->incident link. cloud_ops_runtime._incident_signature_index ->
        # ops_recurrence_joins.extract_event_signatures, which accepts ONLY the exact
        # `{version}:{32 hex}` shape.
        column: entry["event_signature"],
        # Incident side of the MSP-B7 event_incident window (2h,
        # ops_calibration.CALIBRATED_CORRELATION_WINDOWS). Outside it,
        # window_overlap stays False and no event-consuming detector can fire.
        "opened_at": "<within 2h of %s>" % (entry.get("first_seen") or "the event"),
        # _incident_ttr_seconds needs a resolve time: ALERT_TRIAGE_TOIL requires
        # 0 < median_ttr_minutes <= 30.
        "resolved_at": "<opened_at + ~10 minutes>",
        "closed_at": "<opened_at + ~10 minutes>",
        # Must be IDENTICAL across this signature's incidents:
        # cloud_ops_alert_triage_toil._distinct_close_codes must equal 1.
        "close_code": close_code,
        "close_notes": "Restarted per runbook KB0010234",
        # Read as resolution.resolved_by_group / incident.assignment_group — a
        # queue, never a person (MSP-B6 AC2).
        "assignment_group": assignment_group,
        "category": "Infrastructure",
        "short_description": f"Azure alert on {resource}",
        "cmdb_ci": resource,
        "state": "7",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-id", required=True)
    parser.add_argument(
        "--servicenow-payloads",
        action="store_true",
        help="also print a ServiceNow incident body per signature",
    )
    parser.add_argument(
        "--signature-column",
        default=DEFAULT_SIGNATURE_COLUMN,
        help=f"ServiceNow column to stamp (default: {DEFAULT_SIGNATURE_COLUMN})",
    )
    parser.add_argument("--assignment-group", default="Cloud Operations")
    parser.add_argument("--close-code", default="Solved (Permanently)")
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument(
        "--token",
        default=None,
        help=(
            "skip ARM token acquisition and use this value. Offline the fixture "
            "clients ignore it, so any placeholder works with no vaulted SP."
        ),
    )
    args = parser.parse_args()

    records = _collect(args.org_id, token=args.token)
    folded = _fold(records)
    rows = _persisted_rows(args.org_id, records)

    print(f"# org={args.org_id} mode={os.environ.get('INGEST_MODE', 'offline')}")
    print(f"# raw azure records polled : {len(records)}")
    print(f"# distinct event_signatures: {len(folded)}")
    print(f"# persisted rows           : {len(rows)}  (post noise-floor)")
    print(f"# signature version prefix : {EVENT_SIGNATURE_VERSION}")
    print("# stored checkpoint NOT read and NOT advanced by this script")
    print()

    # Every signature, one per line — the copy-paste list.
    print("# --- event_signature values (as persisted) ---")
    for row in rows:
        print(row["signature"])
    print()

    payloads: List[Dict[str, Any]] = []
    for index, (signature, entry) in enumerate(folded.items(), start=1):
        print(f"[{index}] {signature}")
        print(f"     occurrences : {entry['occurrences']}"
              f"  (recurring={entry['occurrences'] > 1})")
        print(f"     surface     : {entry['surface']}"
              f"  subscription={entry['subscription_id']}")
        print(f"     event_class : {entry['event_class']}"
              f"  resource_type={entry['resource_type']}")
        print(f"     event_type  : {entry['event_type']}")
        print(f"     resource_id : {entry['resource_id']}")
        print(f"     window      : {entry['first_seen']} .. {entry['last_seen']}")
        print(f"     hashed      : {entry['components']}")
        if args.servicenow_payloads:
            payload = _servicenow_payload(
                entry,
                column=args.signature_column,
                assignment_group=args.assignment_group,
                close_code=args.close_code,
            )
            payloads.append(payload)
            print("     servicenow  :")
            for line in json.dumps(payload, indent=2).splitlines():
                print(f"       {line}")
        print()

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "org_id": args.org_id,
                    "raw_record_count": len(records),
                    "event_signatures": list(folded.values()),
                    # The verbatim rows a run persists / the endpoint returns.
                    "persisted_rows": rows,
                    "servicenow_payloads": payloads,
                },
                handle,
                indent=2,
            )
        print(f"# wrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
