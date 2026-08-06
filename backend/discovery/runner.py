"""
SF-2.8 — Runner CLI — ENG-SHARED-1 pack selector added

Full pipeline: ingest → detect → score → build_evidence → OpportunityCandidate[]

Usage:
    python -m backend.discovery.runner --mode offline
    python -m backend.discovery.runner --mode offline --pack ncino
    python -m backend.discovery.runner --mode live --systems salesforce,jira --pack ncino
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dotenv import load_dotenv

from app.telemetry import record_event

try:
    from app.db import update_run_step
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.db import update_run_step

try:
    from app.live_ingest_credentials import (
        clear_connector_auth_failure,
        flag_connector_auth_failure,
    )
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.live_ingest_credentials import (
        clear_connector_auth_failure,
        flag_connector_auth_failure,
    )

# Track A adapter
from .track_a_adapter import export_track_a_seed

try:
    from app.temporal import DetectorEvaluation, snapshot_signals
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.app.temporal import DetectorEvaluation, snapshot_signals

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def _run_detector_phase(
    all_detectors: List[Any],
    sf_data: Dict[str, Any],
    sn_data: Dict[str, Any],
    jira_data: Dict[str, Any],
) -> tuple[List[Any], List[DetectorEvaluation]]:
    detector_results = []
    all_evaluated: List[DetectorEvaluation] = []

    for det in all_detectors:
        name = det.__name__.split(".")[-1]
        try:
            evaluation = det.evaluate(sf_data, sn_data, jira_data)
            all_evaluated.append(evaluation)

            fired = det.detect(sf_data, sn_data, jira_data)
            detector_results.extend(fired)
            if evaluation.fired != bool(fired):
                logger.warning(
                    "  %s: evaluate/detect fired mismatch (evaluation=%s, results=%d)",
                    name,
                    evaluation.fired,
                    len(fired),
                )
            status = f"FIRED ({len(fired)})" if fired else "not fired"
        except Exception as e:
            status = f"ERROR: {e}"
        logger.info(f"  {name}: {status}")

    logger.info(
        "Temporal detector evaluations captured: %d/%d",
        len(all_evaluated),
        len(all_detectors),
    )
    return detector_results, all_evaluated


def _snapshot_detector_evaluations(
    *,
    org_id: str,
    run_id: str,
    pack_id: str,
    detector_results: List[Any],
    all_evaluated: List[DetectorEvaluation],
) -> datetime:
    run_completed_at = datetime.now(timezone.utc)
    try:
        snapshot_signals(
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            detector_results=detector_results,
            all_evaluated=all_evaluated,
            run_completed_at=run_completed_at,
        )
    except Exception as e:
        logger.warning("Signal snapshot failed (non-blocking): %s", e)
    return run_completed_at


def _record_pack_execution(
    *,
    org_id: str,
    run_id: str,
    pack_id: str,
    pack_name: str,
    pack_version: str,
    detectors: List[Any],
    evaluated_count: int,
    executed_at: datetime,
) -> List[str]:
    """Emit and return the immutable detector list used for this run.

    Run Health must describe what actually executed, not whatever the mutable
    pack registry contains when the dashboard is opened later. Every configured
    detector is attempted by ``_run_detector_phase``; identifiers therefore come
    from that exact module list, including a detector whose evaluation raised.
    """
    detector_ids = [
        str(
            getattr(
                detector,
                "DETECTOR_ID",
                getattr(detector, "__name__", type(detector).__name__).split(".")[-1],
            )
        )
        for detector in detectors
    ]
    try:
        record_event(
            "run.pack_executed",
            {
                "org_id": org_id,
                "run_id": run_id,
                "pack_id": pack_id,
                "pack_name": pack_name,
                "pack_version": pack_version,
                "detector_ids": detector_ids,
                "detector_count": len(detector_ids),
                "evaluated_count": evaluated_count,
                "not_evaluated_count": max(0, len(detector_ids) - evaluated_count),
                "executed_at": executed_at.isoformat(),
            },
        )
    except Exception as exc:
        # Telemetry is observability, not a reason to fail discovery. The same
        # snapshot is also returned to materialization and persisted on the run.
        logger.warning("Pack execution telemetry failed (non-blocking): %s", exc)
    return detector_ids


def build_org_context(sf_data: Dict, sn_data: Dict, jira_data: Dict) -> Dict[str, Any]:
    cm = sf_data.get("case_metrics") or {}
    fi = sf_data.get("flow_inventory") or {}
    aps = sf_data.get("approval_processes") or []
    ncs = sf_data.get("named_credentials") or []
    csr_sf = sf_data.get("cross_system_references") or {}
    sn_im = (sn_data or {}).get("incident_metrics") or {}
    csr_sn = (sn_data or {}).get("cross_system_references") or {}
    sn_lc = (sn_data or {}).get("lending_correlation") or {}
    jira_im = (jira_data or {}).get("issue_metrics") or {}
    jira_lc = (jira_data or {}).get("lending_correlation") or {}

    return {
        "sf_total_cases_90d":    cm.get("total_cases_90d", 0),
        "sf_closed_cases_90d":   cm.get("closed_cases_90d", 0),
        "sf_owner_changes_90d":  cm.get("owner_changes_90d", 0),
        "sf_handoff_score":      cm.get("handoff_score", 0.0),
        "sf_active_flows":       fi.get("flow_activity_score", 0.0) if fi else 0,
        "sf_flow_activity_score":fi.get("flow_activity_score", 0.0),
        "sf_pending_approvals":  sum(a.get("pending_count", 0) for a in aps),
        "sf_approval_processes": len(aps),
        "sf_named_credentials":  len(ncs),
        "sf_echo_score":         csr_sf.get("sf_echo_score", 0.0),
        "sn_echo_score":         csr_sn.get("sn_echo_score", 0.0),
        "sn_total_incidents_90d": sn_im.get("total_incidents_90d", csr_sn.get("sn_total_incidents", 0)),
        "sn_lending_signal_count": sn_lc.get("total_matched", 0),
        "jira_echo_score":       jira_im.get("jira_echo_score", 0.0),
        "jira_total_issues_90d":  jira_im.get("total_issues_90d", 0),
        "jira_lending_signal_count": jira_lc.get("total_matched", 0),
        "sources_connected": {
            "salesforce":  bool(sf_data),
            "servicenow":  bool(sn_data),
            "jira":        bool(jira_data),
        },
    }


def _ingest_github(org_id: str, run_id: str) -> Dict[str, Any]:
    """Sync wrapper around the async GitHub connector ingest (T1-S12).

    Returns the Section 2b payload, or {} on any failure (non-blocking — a
    GitHub ingest failure must never abort the run). Tests monkeypatch this
    function to inject mocked GitHub data and exercise the full
    ingest → detect → score → OpportunityCandidate path (AC10).

    Always runs the coroutine in a dedicated thread with its own event loop so
    this function is safe to call from both sync contexts and from within
    FastAPI's running async event loop (e.g. background tasks). Using
    asyncio.run() or loop.run_until_complete() directly on the calling thread
    raises RuntimeError when an event loop is already running there.
    """
    import asyncio
    import concurrent.futures

    try:
        from connectors.saas import github as github_connector
    except ModuleNotFoundError:  # project-root execution uses backend as package
        try:
            from backend.connectors.saas import github as github_connector
        except Exception as e:
            logger.warning("GitHub connector import failed (non-blocking): %s", e)
            return {}
    except Exception as e:
        logger.warning("GitHub connector import failed (non-blocking): %s", e)
        return {}

    try:
        # Run in a fresh thread so asyncio.run() always gets a clean event loop,
        # regardless of whether the caller is sync or inside FastAPI's loop.
        def _run() -> Dict[str, Any]:
            return asyncio.run(github_connector.ingest(org_id, run_id))

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_run).result()
    except Exception as e:
        # Use type name only — the exception str may contain request headers
        # (Authorization: Bearer ...) from urllib3/requests reprs.
        logger.warning(
            "GitHub ingestion failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )
        return {}


def _ingest_slack_corroboration(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive Slack change-based ingestion and build its corroboration block.

    R16-A2 / AT-421 (T6) + AT-419 (T4): Slack is ingested through the shared
    change runner (R16-A1), so this one path satisfies both stories:

      * the runner owns the checkpoint lifecycle — incremental reads against the
        stored ``(org_id, 'slack')`` checkpoint, resumable streamed first load,
        and write-only-on-full-success — so Slack is NOT re-read in full every
        run; and
      * it emits one ``ingestion.artifact_changed`` event per changed Slack
        artifact in every fully-processed batch (AC7), reusing the R16-A1 event
        path — no events are minted here.

    The changed records from each batch are collected via ``process_batch`` and
    aggregated into the ``{escalation_pattern, activity, cross_references}`` block
    the corroboration engine reads, wrapped under the ``'slack'`` key that
    ``_find_corroboration_block('slack', …)`` recognises
    (:func:`slack_signals.build_slack_corroboration_payload`).

    R18-A4 / AT-594 (T1) — deep content BESIDE the signal path: the SAME
    fully-processed batch is also handed to
    :meth:`SlackIngestor.ingest_deep_content`, which assembles the messages into
    threads, scope-checks them against the P5 selection, and hands the conversation
    TEXT to the retrieval substrate (``ingest_content``). One change-runner pass
    therefore drives BOTH the reach signal AND the depth content off the single
    ``(org, 'slack')`` checkpoint — no new connector, no new checkpointing, and the
    reach signal extraction is untouched. Because reach and depth SHARE that one
    checkpoint, the deep-content hand-off is non-blocking here: a substrate failure
    is logged (type name only) and the run continues rather than freezing the shared
    checkpoint or aborting corroboration. Re-handing is idempotent — the substrate
    replaces an artifact's chunks by ``(source_system, source_artifact)`` — so a
    thread re-indexes when it next changes.

    The Slack MEDIUM ceiling (Slack-only stays MEDIUM, never standalone HIGH; it
    elevates only WITH a primary corroborator) is enforced by the engine's
    COR-05/COR-06 rules and the T3 clamp, never here. Non-blocking: any failure
    degrades to an empty block (``{}``) so a Slack read never aborts the run — the
    change runner itself swallows ingestion errors and leaves the checkpoint
    unadvanced (next run re-reads), and the guards here cover import/everything else.
    """
    try:
        from .ingest import change_runner
        from .ingest.slack import SlackIngestor
        from .ingest.slack_signals import build_slack_corroboration_payload
    except Exception as e:  # noqa: BLE001 — Slack corroboration is optional.
        logger.warning("Slack connector import failed (non-blocking): %s", e)
        return {}

    ingestor = SlackIngestor()
    collected: List[Dict[str, Any]] = []

    def _process_batch(batch) -> None:
        # Reach: collect the batch's records for the corroboration block.
        collected.extend(batch.records)
        # Depth (T1): hand this batch's conversation content to the retrieval
        # substrate beside the signal path. Guarded and non-blocking: reach and
        # depth share ONE checkpoint, so a content hand-off failure must not freeze
        # it or abort corroboration — it is logged (type name only, never the
        # exception str, which may carry a Bearer token) and the run continues.
        try:
            ingestor.ingest_deep_content(org_id, batch.records)
        except Exception as e:  # noqa: BLE001 — depth must not break reach/checkpoint
            logger.warning(
                "Slack deep-content hand-off failed (non-blocking) org=%s run=%s: [%s]",
                org_id, run_id, type(e).__name__,
            )

    try:
        # The runner reads the checkpoint, streams the delta, emits an
        # artifact_changed event per changed record, and advances the checkpoint
        # only on full success. process_batch collects each fully-processed
        # batch's records for corroboration AND hands its content to retrieval.
        result = change_runner.ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=_process_batch,
        )
    except Exception as e:  # noqa: BLE001 — belt-and-braces; runner is non-raising.
        # Type name only — the exception str may carry a Bearer token from the
        # Slack client's request reprs.
        logger.warning(
            "Slack ingestion failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )
        return {}

    if result.error is not None:
        # The runner swallows ingestion errors and leaves the checkpoint
        # unadvanced so the next run re-reads; surface it without aborting. Any
        # records already collected from completed batches still feed corroboration.
        logger.warning(
            "Slack change ingest reported an error (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(result.error).__name__,
        )
    else:
        logger.info(
            "Slack change ingest: %s — %d batch(es), %d changed record(s), checkpoint_advanced=%s",
            "first run (streamed full load)" if result.first_run else "incremental",
            result.batches, result.records, result.checkpoint_advanced,
        )

    return build_slack_corroboration_payload(collected)


def _surface_operational_credential_health(
    run_id: str, health_records: List[Dict[str, Any]]
) -> None:
    """Merge fail-closed operational-app credential-miss records into run health (AC1).

    R191-H1 / T1: when an operational-app target is skipped because its vault
    credential is missing, the ingestor records an actionable, credential-free
    connector-health entry (org / target / credential ref). This surfaces those
    entries in the run's ``connector_health`` KV — the same store the connector-
    health API and S1 badges read — so a missing credential is visible with an
    actionable reason rather than a silent no-op. Each entry is keyed
    ``"<System> (<app_id>)"`` so multiple failed targets never collide.

    Non-blocking: any KV read/write problem is logged and swallowed — surfacing
    health must never abort a run. Records carry no secret value.
    """
    if not health_records:
        return
    try:
        try:
            from app.db import run_kv_get, run_kv_set
        except ModuleNotFoundError:  # project-root execution uses backend as package
            from backend.app.db import run_kv_get, run_kv_set  # type: ignore

        existing = run_kv_get("connector_health", run_id, None) or {}
        if not isinstance(existing, dict):
            existing = {}
        for rec in health_records:
            system = str(rec.get("system") or "Operational Application")
            app_id = str(rec.get("appId") or "").strip()
            key = f"{system} ({app_id})" if app_id else system
            existing[key] = rec
        run_kv_set("connector_health", run_id, existing)
    except Exception as e:  # noqa: BLE001 — health surfacing is non-blocking.
        logger.warning(
            "Could not surface operational-app credential health (non-blocking) "
            "run=%s: [%s]",
            run_id, type(e).__name__,
        )


#: Run-scoped KV key holding the assembled cloud-event signature rows.
#: Read-only surface: ``GET /api/runs/{runId}/cloud-ops/event-signatures``.
KV_CLOUD_OPS_EVENT_SIGNATURES = "cloud_ops_event_signatures"


def _persist_cloud_ops_event_signatures(run_id: str, block: Dict[str, Any]) -> None:
    """Persist the assembled ``cloud_ops.event_signatures`` rows for this run.

    The rows are the EXACT detector input ``build_cloud_ops_runtime`` produced —
    stored verbatim, computed nowhere else, so this is a record of what the run
    actually saw rather than a re-derivation. Until now the only trace a run left
    of them was the count in the ``cloudOpsRuntime`` health block and the assembly
    log line: ``build_org_context`` does not carry the cloud_ops block, nothing in
    the app layer persists it, and ``AzureEventIngestor.produces_retrieval_content
    = False`` means no per-event telemetry is emitted either. A signature VALUE
    therefore only survived a run inside a FIRED finding's evidence — which cannot
    exist until a ServiceNow incident already carries that signature. This write
    breaks that circularity so the values can be read and stamped into ServiceNow.

    Write-only, additive, and strictly after assembly: it reads ``block`` and
    never mutates it, so no detector, corroboration, or scoring input changes.
    Non-blocking — any KV problem is logged and swallowed, exactly like
    :func:`_surface_operational_credential_health`.
    """
    rows = block.get("event_signatures")
    if not isinstance(rows, list):
        return
    try:
        try:
            from app.db import run_kv_set
        except ModuleNotFoundError:  # project-root execution uses backend as package
            from backend.app.db import run_kv_set  # type: ignore

        run_kv_set(
            KV_CLOUD_OPS_EVENT_SIGNATURES,
            run_id,
            {
                "runId": run_id,
                "capturedAt": datetime.now(timezone.utc).isoformat(),
                "count": len(rows),
                "rows": rows,
            },
        )
        logger.info(
            "Persisted %d cloud-ops event signature row(s) for run %s "
            "(read via GET /api/runs/%s/cloud-ops/event-signatures)",
            len(rows), run_id, run_id,
        )
    except Exception as e:  # noqa: BLE001 — persisting the record is non-blocking.
        logger.warning(
            "Could not persist cloud-ops event signatures (non-blocking) "
            "run=%s: [%s]",
            run_id, type(e).__name__,
        )


def _ingest_ops_event_bridge(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive MSP-B8 on the shared checkpoint path and validate each batch.

    Validation happens inside ``process_batch`` so a malformed or cross-org
    normalised event cannot advance the staging row checkpoint. Runtime failures
    remain non-blocking for the wider discovery run; the returned health block
    makes the degradation explicit.
    """
    try:
        from .cloud_ops_runtime import operational_event_from_bridge_record
        from .ingest import change_runner
        from .ingest.ops_event_bridge import OpsEventBridgeIngestor
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Ops event bridge import failed (non-blocking): [%s]",
            type(exc).__name__,
        )
        return {
            "records": [],
            "health": {
                "status": "unavailable",
                "reason": type(exc).__name__,
                "records": 0,
            },
        }

    collected: List[Dict[str, Any]] = []

    def _process_batch(batch: Any) -> None:
        validated: List[Dict[str, Any]] = []
        for record in batch.records:
            if not isinstance(record, dict):
                raise TypeError("ops event bridge emitted a non-mapping record")
            operational_event_from_bridge_record(record, org_id=org_id)
            validated.append(dict(record))
        collected.extend(validated)

    try:
        result = change_runner.ingest_with_checkpoint(
            OpsEventBridgeIngestor(),
            org_id,
            process_batch=_process_batch,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Ops event bridge failed (non-blocking) org=%s run=%s: [%s]",
            org_id,
            run_id,
            type(exc).__name__,
        )
        return {
            "records": collected,
            "health": {
                "status": "unavailable",
                "reason": type(exc).__name__,
                "records": len(collected),
            },
        }

    status = "degraded" if result.error is not None else "ok"
    health: Dict[str, Any] = {
        "status": status,
        "records": len(collected),
        "reported_records": int(result.records),
        "batches": int(result.batches),
        "complete": bool(result.complete),
        "first_run": bool(result.first_run),
        "checkpoint_advanced": bool(result.checkpoint_advanced),
    }
    if result.error is not None:
        health["reason"] = type(result.error).__name__
        logger.warning(
            "Ops event bridge degraded org=%s run=%s: [%s]",
            org_id,
            run_id,
            type(result.error).__name__,
        )
    else:
        logger.info(
            "Ops event bridge: %d event(s), %d batch(es), checkpoint_advanced=%s",
            len(collected),
            result.batches,
            result.checkpoint_advanced,
        )
    return {"records": collected, "health": health}


def _cloud_event_step_ok(result: Dict[str, Any]) -> bool:
    """Whether a native cloud-event ingest counts as a SUCCESSFUL discovery step.

    The two native cloud connectors report a health ``status`` rather than raising
    (both are non-blocking), so the Discovery Progress step outcome is derived from
    it: ``ok``/``degraded`` are successes — degraded means partial data was ingested
    and the reason is already carried in the run's ``cloudOpsRuntime`` health block,
    so a red "failed" row would overstate it. ``unavailable`` (import/config/ingest
    failure) and ``not_configured`` (selected for the run but no pinned
    accounts/subscriptions) are failures: the connector delivered nothing, and a
    green check on a source that produced no events is exactly the dishonest
    reporting the progress list exists to prevent.
    """
    status = str((result.get("health") or {}).get("status") or "").strip().lower()
    return status in {"ok", "degraded"}


def _ingest_aws_events(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive the native MSP-B1 AWS Event Connector on the shared checkpoint path.

    The AWS half of the MSP-B1/B2 matched pair, and the live counterpart of the
    MSP-B8 Event-History Bridge for AWS: it polls CloudWatch alarm history, the
    bounded EventBridge rule set, and CloudTrail management events for the pinned
    managed accounts, normalises each through its MSP-B0 mapper, and emits the SAME
    OperationalEvent record shape the bridge emits (AC4 transport equivalence).
    Records are validated inside ``process_batch`` (a malformed or cross-org record
    raises before the ``(org, "aws_events")`` checkpoint can advance), and the
    collected records are merged into the cloud-ops assembly alongside the bridge
    and Azure records — where the OpsEventStream folds duplicate signatures, so a
    native event and its bridged twin never double-count.

    Mirrors :func:`_ingest_azure_events` exactly (same change-runner path, same
    non-blocking posture, same health-block shape), plus the AWS connector's
    per-account health report (AT-646 / AC8), which is also merged into the run's
    ``connector_health`` so a revoked role on one account is visible in run health
    rather than only inside the connector object. Returns ``{"records", "health"}``.
    """
    try:
        from .cloud_ops_runtime import operational_event_from_bridge_record
        from .ingest import change_runner
        from .ingest.aws_event_connector import build_ingestor as build_aws_ingestor
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "AWS event connector import failed (non-blocking): [%s]",
            type(exc).__name__,
        )
        return {
            "records": [],
            "health": {"status": "unavailable", "reason": type(exc).__name__, "records": 0},
        }

    try:
        ingestor = build_aws_ingestor(org_id)
    except Exception as exc:  # noqa: BLE001 — a present-but-invalid config must not crash the run
        logger.warning(
            "AWS event connector config invalid (non-blocking) org=%s: [%s]",
            org_id,
            type(exc).__name__,
        )
        return {
            "records": [],
            "health": {"status": "unavailable", "reason": type(exc).__name__, "records": 0},
        }

    if ingestor is None:
        # Not configured for this org (no pinned accounts / no config) — the
        # connector simply contributes nothing, exactly like an unconfigured source.
        return {"records": [], "health": {"status": "not_configured", "records": 0}}

    collected: List[Dict[str, Any]] = []

    def _process_batch(batch: Any) -> None:
        for record in batch.records:
            if not isinstance(record, dict):
                raise TypeError("aws event connector emitted a non-mapping record")
            # Validate (and org-scope) each event exactly as the bridge does, so a
            # bad record cannot advance the checkpoint.
            operational_event_from_bridge_record(record, org_id=org_id)
            collected.append(dict(record))

    try:
        result = change_runner.ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=_process_batch,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "AWS event connector failed (non-blocking) org=%s run=%s: [%s]",
            org_id,
            run_id,
            type(exc).__name__,
        )
        return {
            "records": collected,
            "health": {
                "status": "unavailable",
                "reason": type(exc).__name__,
                "records": len(collected),
                "accounts": _aws_account_health(ingestor),
            },
        }

    status = "degraded" if result.error is not None else "ok"
    accounts = _aws_account_health(ingestor)
    poll = _cloud_poll_health(ingestor)
    # AC8: a per-account auth/throttle failure is LOUD. Even when the run itself
    # succeeded, an account that failed degrades the connector's reported status —
    # a partial ingest must never read as a clean one.
    if accounts and not accounts.get("all_healthy", True):
        status = "degraded"
    # Same rule for an undrained backlog: a scope that stopped on the per-run poll
    # bound (poll cap / deadline / B7 budget) resumes next run, but this run's ingest
    # was partial and must say so rather than reporting a clean pass.
    if poll and not poll.get("complete", True):
        status = "degraded"
    health: Dict[str, Any] = {
        "status": status,
        "records": len(collected),
        "reported_records": int(result.records),
        "batches": int(result.batches),
        "complete": bool(result.complete),
        "first_run": bool(result.first_run),
        "checkpoint_advanced": bool(result.checkpoint_advanced),
        "accounts": accounts,
        "poll": poll,
    }
    if result.error is not None:
        health["reason"] = type(result.error).__name__
        logger.warning(
            "AWS event connector degraded org=%s run=%s: [%s]",
            org_id,
            run_id,
            type(result.error).__name__,
        )
    else:
        logger.info(
            "AWS event connector: %d event(s), %d batch(es), checkpoint_advanced=%s",
            len(collected),
            result.batches,
            result.checkpoint_advanced,
        )
    _surface_cloud_account_health(org_id, run_id, "aws_events", accounts)
    return {"records": collected, "health": health}


def _cloud_poll_health(ingestor: Any) -> Dict[str, Any]:
    """The native cloud connector's poll-phase report, or ``{}`` when unavailable.

    Names the scopes whose backlog did NOT drain this run and the per-run bound that
    stopped each (MSP-B1: an early stop is resume, not truncation — but it must be
    visible). Never raises: reporting must not be able to fail a good run.
    """
    try:
        report = getattr(ingestor, "poll_report", None)
        return dict(report()) if callable(report) else {}
    except Exception:  # noqa: BLE001 — health is advisory, never fatal
        logger.debug("Could not read cloud connector poll report (non-blocking)", exc_info=True)
        return {}


def _aws_account_health(ingestor: Any) -> Dict[str, Any]:
    """The AWS connector's per-account health report, or ``{}`` when unavailable.

    Offline/static poll sources report no health; a live source reports one entry
    per managed account (AT-646). Never raises — health surfacing must not be able
    to fail an otherwise-good run.
    """
    try:
        report = getattr(ingestor, "health_report", None)
        return dict(report()) if callable(report) else {}
    except Exception:  # noqa: BLE001 — health is advisory, never fatal
        logger.debug("Could not read AWS connector health (non-blocking)", exc_info=True)
        return {}


def _surface_cloud_account_health(
    org_id: str, run_id: str, connector_id: str, report: Dict[str, Any]
) -> None:
    """Merge a cloud connector's per-account health into the run's connector_health.

    Closes the AC8 loop: the connector already records a revoked role / throttled
    account loudly, but that report lived only on the connector object and never
    reached run health, so the R18-C2 connector panel could not show it. Each
    account is surfaced under its own key ("AWS Events (111122223333)") so a
    partial multi-account ingest is visible per account, and the pinned scope on
    the Integration Hub record is updated to the same vocabulary so the card and
    run health read the same word (MSP-B13 AC7).

    Entirely non-blocking: any failure is logged and swallowed.
    """
    accounts = (report or {}).get("accounts") or []
    if not accounts:
        return
    try:
        from app.db import run_kv_get, run_kv_set
        from app.source_keys import source_key_for
    except ModuleNotFoundError:  # project-root execution uses backend as package
        from backend.app.db import run_kv_get, run_kv_set  # type: ignore
        from backend.app.source_keys import source_key_for  # type: ignore

    system = source_key_for(connector_id)
    try:
        existing = run_kv_get("connector_health", run_id, None) or {}
        if not isinstance(existing, dict):
            existing = {}
        for account in accounts:
            account_id = str(account.get("account_id") or "").strip()
            key = f"{system} ({account_id})" if account_id else system
            existing[key] = {
                "system": system,
                "connectorId": connector_id,
                "scopeId": account_id,
                "status": account.get("status"),
                "message": account.get("message") or "",
                "surfacesOk": list(account.get("surfaces_ok") or []),
                "surfacesFailed": dict(account.get("surfaces_failed") or {}),
                "throttleEvents": int(account.get("throttle_events") or 0),
            }
        run_kv_set("connector_health", run_id, existing)
    except Exception as exc:  # noqa: BLE001 — health surfacing is non-blocking.
        logger.warning(
            "Could not surface %s per-account health (non-blocking) run=%s: [%s]",
            connector_id, run_id, type(exc).__name__,
        )

    _update_pinned_scope_health(org_id, connector_id, accounts)


def _update_pinned_scope_health(
    org_id: str, connector_id: str, accounts: List[Dict[str, Any]]
) -> None:
    """Write each account's outcome back onto its pinned Integration Hub scope.

    So the connector card stops showing a freshly-pinned account as ``pending``
    forever, and shows ``auth_failed`` the moment a role is revoked — the same
    vocabulary run health uses (MSP-B13 AC7). Scopes that were not polled are left
    untouched. Non-blocking.
    """
    try:
        from app import db

        record = db.org_connector_get(org_id, connector_id)
        if not isinstance(record, dict):
            return
        scopes = record.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            return
        by_account = {
            str(a.get("account_id") or ""): a for a in accounts if a.get("account_id")
        }
        changed = False
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            account = by_account.get(str(scope.get("scope_id") or ""))
            if account is None:
                continue
            scope["status"] = account.get("status") or scope.get("status")
            scope["health_message"] = account.get("message") or ""
            scope["surfaces_ok"] = list(account.get("surfaces_ok") or [])
            scope["surfaces_failed"] = dict(account.get("surfaces_failed") or {})
            scope["last_checkpoint_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
        if changed:
            record["scopes"] = scopes
            db.org_connector_set(org_id, connector_id, record)
    except Exception as exc:  # noqa: BLE001 — card refresh is advisory
        logger.warning(
            "Could not refresh %s pinned-scope health (non-blocking) org=%s: [%s]",
            connector_id, org_id, type(exc).__name__,
        )


def _ingest_azure_events(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive the native MSP-B2 Azure Event Connector on the shared checkpoint path.

    The live counterpart of the MSP-B8 Event-History Bridge: it polls Azure Monitor
    Alerts, the Activity Log (Administrative only), and Service Health for the pinned
    subscriptions, normalises each through its MSP-B0 mapper, and emits the SAME
    OperationalEvent record shape the bridge emits. Records are validated inside
    ``process_batch`` (a malformed or cross-org record raises before the
    ``(org, "azure_events")`` checkpoint can advance), and the collected records are
    merged into the cloud-ops assembly alongside the bridge records — where the
    OpsEventStream folds duplicate signatures, so a native event and its bridged twin
    never double-count.

    Mirrors :func:`_ingest_ops_event_bridge` exactly (same change-runner path, same
    non-blocking posture, same health-block shape). Returns ``{"records", "health"}``.
    """
    try:
        from .cloud_ops_runtime import operational_event_from_bridge_record
        from .ingest import change_runner
        from .ingest.azure_events import build_ingestor as build_azure_ingestor
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Azure event connector import failed (non-blocking): [%s]",
            type(exc).__name__,
        )
        return {
            "records": [],
            "health": {"status": "unavailable", "reason": type(exc).__name__, "records": 0},
        }

    try:
        ingestor = build_azure_ingestor(org_id)
    except Exception as exc:  # noqa: BLE001 — a present-but-invalid config must not crash the run
        logger.warning(
            "Azure event connector config invalid (non-blocking) org=%s: [%s]",
            org_id,
            type(exc).__name__,
        )
        return {
            "records": [],
            "health": {"status": "unavailable", "reason": type(exc).__name__, "records": 0},
        }

    if ingestor is None:
        # Not configured for this org (no pinned subscriptions / no config) — the
        # connector simply contributes nothing, exactly like an unconfigured source.
        return {"records": [], "health": {"status": "not_configured", "records": 0}}

    collected: List[Dict[str, Any]] = []

    def _process_batch(batch: Any) -> None:
        for record in batch.records:
            if not isinstance(record, dict):
                raise TypeError("azure event connector emitted a non-mapping record")
            # Validate (and org-scope) each event exactly as the bridge does, so a
            # bad record cannot advance the checkpoint.
            operational_event_from_bridge_record(record, org_id=org_id)
            collected.append(dict(record))

    try:
        result = change_runner.ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=_process_batch,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Azure event connector failed (non-blocking) org=%s run=%s: [%s]",
            org_id,
            run_id,
            type(exc).__name__,
        )
        return {
            "records": collected,
            "health": {
                "status": "unavailable",
                "reason": type(exc).__name__,
                "records": len(collected),
            },
        }

    status = "degraded" if result.error is not None else "ok"
    health: Dict[str, Any] = {
        "status": status,
        "records": len(collected),
        "reported_records": int(result.records),
        "batches": int(result.batches),
        "complete": bool(result.complete),
        "first_run": bool(result.first_run),
        "checkpoint_advanced": bool(result.checkpoint_advanced),
    }
    # MSP-B7 T4: the connector admits its own events, so its budget report is the
    # deferral proof for this poll. A breached budget is a partial ingest and must
    # never be reported as a clean one (mirrors the AWS `poll` health block).
    try:
        budget = ingestor.budget_report()
    except Exception:  # noqa: BLE001 — health reporting is never run-critical
        budget = {}
    if budget:
        health["budget"] = dict(budget)
        if budget.get("breached"):
            health["status"] = "degraded"
            health.setdefault("reason", "run_event_budget_exhausted")
    # 2.0-D3 T4: the budget's own counters can only report events it SAW, so a poll
    # the budget stopped before it fetched anything leaves `breached` False — which
    # would report a run that skipped whole subscriptions as a clean one. The
    # connector's deferral report names those polls; mirrors the AWS `poll` block.
    try:
        deferrals = ingestor.deferral_report()
    except Exception:  # noqa: BLE001 — health reporting is never run-critical
        deferrals = {}
    if deferrals and not deferrals.get("complete", True):
        health["deferrals"] = dict(deferrals)
        health["status"] = "degraded"
        health.setdefault("reason", deferrals.get("reason", "run_event_budget_exhausted"))
    # 2.0-D3 T1: the Application Insights picture for this poll. Derived from the
    # emitted records (each in-scope record carries its scope on the WRAPPER), so no
    # extra plumbing is needed and run health states what the bounded App Insights
    # read actually produced rather than leaving it implicit inside the connector.
    # Omitted entirely when the poll met no App Insights signal, so a run with no
    # App Insights estate reports exactly the health block it reported before D3.
    app_insights_records = [r for r in collected if r.get("app_insights")]
    if app_insights_records:
        by_kind: Dict[str, int] = {}
        components: set = set()
        for record in app_insights_records:
            scope = record.get("app_insights") or {}
            # An in-scope record whose kind could not be established is counted as
            # 'unclassified' rather than folded into a real kind — the same honesty
            # the classifier itself applies (see azure_app_insights.py).
            kind = scope.get("signal_kind") or "unclassified"
            by_kind[kind] = by_kind.get(kind, 0) + 1
            if scope.get("component_id"):
                components.add(str(scope["component_id"]))
        health["app_insights"] = {
            "records": len(app_insights_records),
            "components": sorted(components),
            "by_signal_kind": dict(sorted(by_kind.items())),
        }
    if result.error is not None:
        health["reason"] = type(result.error).__name__
        logger.warning(
            "Azure event connector degraded org=%s run=%s: [%s]",
            org_id,
            run_id,
            type(result.error).__name__,
        )
    else:
        logger.info(
            "Azure ingestion: %d event(s), %d batch(es), checkpoint_advanced=%s",
            len(collected),
            result.batches,
            result.checkpoint_advanced,
        )
    return {"records": collected, "health": health}


def _ingest_java_app_corroboration(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive Java application change-based ingestion and build its corroboration block.

    R17-A3 / T1+T5+T6: the Java application source is ingested through the shared
    change runner (R16-A1), so this one path satisfies several tasks at once:

      * the runner owns the checkpoint lifecycle — incremental reads against the
        stored ``(org_id, 'java_app')`` checkpoint, resumable streamed first load,
        and write-only-on-full-success — so a Java app is NOT re-read in full
        every run (T1, AC2); and
      * it emits one ``ingestion.artifact_changed`` event per changed Java
        operational artifact in every fully-processed batch (T6, AC6), reusing the
        R16-A1 event path — no events are minted here.

    The changed records from each batch are collected via ``process_batch`` and
    aggregated into the ``{operational_friction, services}`` block the
    corroboration engine reads, wrapped under the ``'java_app'`` key that
    ``_find_corroboration_block('java_app', …)`` recognises
    (:func:`java_app_signals.build_java_app_corroboration_payload`). A Java-app
    operational signal can then corroborate a finding in another connected system
    (COR-09, T5/AC5).

    Operational surface only (AC8): the ingestor reads framework
    health/diagnostics endpoints + logs, never application source code.
    Non-blocking: any failure degrades to an empty block (``{}``) so a Java read
    never aborts the run — the change runner swallows ingestion errors and leaves
    the checkpoint unadvanced (next run re-reads), and the guards here cover
    import/everything else.
    """
    try:
        from .ingest import change_runner
        from .ingest.java_app import JavaAppIngestor
        from .ingest.java_app_signals import build_java_app_corroboration_payload
    except Exception as e:  # noqa: BLE001 — Java-app corroboration is optional.
        logger.warning("Java app connector import failed (non-blocking): %s", e)
        return {}

    collected: List[Dict[str, Any]] = []
    ingestor = JavaAppIngestor()
    try:
        result = change_runner.ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=lambda batch: collected.extend(batch.records),
        )
    except Exception as e:  # noqa: BLE001 — belt-and-braces; runner is non-raising.
        # Type name only — an exception str could carry a Bearer token from a
        # live HTTP client's request repr.
        logger.warning(
            "Java app ingestion failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )
        # A target skipped fail-closed for a missing vault credential still surfaces
        # in connector health even if a later phase raised (R191-H1 / T1, AC1).
        _surface_operational_credential_health(
            run_id, getattr(ingestor, "credential_health", [])
        )
        return {}

    if result.error is not None:
        logger.warning(
            "Java app change ingest reported an error (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(result.error).__name__,
        )
    else:
        logger.info(
            "Java app change ingest: %s — %d batch(es), %d changed record(s), checkpoint_advanced=%s",
            "first run (streamed full load)" if result.first_run else "incremental",
            result.batches, result.records, result.checkpoint_advanced,
        )

    # Fail-closed credential misses (targets skipped for a missing vault
    # credential) surface in the run's connector_health KV with an actionable
    # reason naming the org, the target, and the credential ref (R191-H1 / T1, AC1).
    _surface_operational_credential_health(run_id, ingestor.credential_health)

    return build_java_app_corroboration_payload(collected)


def _ingest_dotnet_app_corroboration(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive .NET application change-based ingestion and build its corroboration block.

    R17-A4 / T1+T5+T6: the .NET application source is the parallel to the Java
    source (R17-A3) and is ingested through the SAME shared change runner (R16-A1)
    and the SAME shared operational-signal extraction — only the collection edge
    differs. This one path therefore satisfies several tasks at once:

      * the runner owns the checkpoint lifecycle — incremental reads against the
        stored ``(org_id, 'dotnet_app')`` checkpoint, resumable streamed first
        load, and write-only-on-full-success — so a .NET app is NOT re-read in
        full every run (T1, AC2); and
      * it emits one ``ingestion.artifact_changed`` event per changed .NET
        operational artifact in every fully-processed batch (T6, AC7), reusing the
        R16-A1 event path — no events are minted here.

    The changed records from each batch are collected via ``process_batch`` and
    aggregated into the ``{operational_friction, services}`` block the corroboration
    engine reads, wrapped under the ``'dotnet_app'`` key that
    ``_find_corroboration_block('dotnet_app', …)`` recognises
    (:func:`dotnet_app_signals.build_dotnet_app_corroboration_payload`). A .NET-app
    operational signal can then corroborate a finding in another connected system
    (COR-10, T5/AC6).

    Operational surface only (AC8): the ingestor reads the .NET health/diagnostics
    surface + logs — never source code, never an external APM. The whole path is
    wrapped so any import/ingest failure is non-blocking.
    """
    try:
        from .ingest import change_runner
        from .ingest.dotnet_app import DotNetAppIngestor
        from .ingest.dotnet_app_signals import build_dotnet_app_corroboration_payload
    except Exception as e:  # noqa: BLE001 — .NET-app corroboration is optional.
        logger.warning(".NET app connector import failed (non-blocking): %s", e)
        return {}

    collected: List[Dict[str, Any]] = []
    ingestor = DotNetAppIngestor()
    try:
        result = change_runner.ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=lambda batch: collected.extend(batch.records),
        )
    except Exception as e:  # noqa: BLE001 — belt-and-braces; runner is non-raising.
        # Type name only — an exception str could carry a Bearer token from a
        # live HTTP client's request repr.
        logger.warning(
            ".NET app ingestion failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )
        # A target skipped fail-closed for a missing vault credential still surfaces
        # in connector health even if a later phase raised (R191-H1 / T1, AC1).
        _surface_operational_credential_health(
            run_id, getattr(ingestor, "credential_health", [])
        )
        return {}

    if result.error is not None:
        logger.warning(
            ".NET app change ingest reported an error (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(result.error).__name__,
        )
    else:
        logger.info(
            ".NET app change ingest: %s — %d batch(es), %d changed record(s), checkpoint_advanced=%s",
            "first run (streamed full load)" if result.first_run else "incremental",
            result.batches, result.records, result.checkpoint_advanced,
        )

    # Fail-closed credential misses (targets skipped for a missing vault
    # credential) surface in the run's connector_health KV with an actionable
    # reason naming the org, the target, and the credential ref (R191-H1 / T1, AC1).
    _surface_operational_credential_health(run_id, ingestor.credential_health)

    return build_dotnet_app_corroboration_payload(collected)


def _ingest_teams_corroboration(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive Teams change-based ingestion and build its corroboration block.

    R17-A1 / AT-435 (T6) + AT-433 (T4): Teams is ingested through the shared change
    runner (R16-A1), so this one path satisfies both stories:

      * the runner advances the per-(org, 'teams') checkpoint (incremental
        Microsoft Graph delta, not a full re-read) and emits one
        ``ingestion.artifact_changed`` event per changed Teams artifact in each
        fully-processed batch (AC7), using the ``artifact_id`` + ``change_kind``
        every ``TeamsIngestor`` record already carries; and
      * the changed records are aggregated into the ``{escalation_pattern,
        activity, cross_references}`` block the corroboration engine reads, wrapped
        under the ``'teams'`` key (T4 / AT-433).

    R18-A4 / AT-595 (T2) — deep content BESIDE the signal path: the SAME
    fully-processed batch is also handed to
    :meth:`TeamsIngestor.ingest_deep_content`, which assembles the Graph messages
    into threads, scope-checks them against the granted channels, and hands the
    conversation TEXT to the retrieval substrate (``ingest_content``) — via the
    SAME shared conversation model Slack uses (T1). One change-runner pass drives
    BOTH the reach signal AND the depth content off the single ``(org, 'teams')``
    checkpoint — no new connector, no new checkpointing, reach signal untouched.
    The deep hand-off is non-blocking here (reach + depth share the checkpoint, so a
    substrate failure is logged, not fatal — idempotent replace re-indexes on next
    change).

    The Teams MEDIUM ceiling — Teams-only stays MEDIUM, never standalone HIGH; it
    elevates only WITH a primary system-of-record corroborator (COR-06) — is
    enforced by the engine's conversation-source COR-05/COR-06 rules and the T3
    clamp, never here. Non-blocking: any failure degrades to an empty block (`{}`)
    so a Teams read never aborts the run (the runner swallows ingestion errors and
    leaves the checkpoint unadvanced for the next run to re-read).
    """
    try:
        from .ingest import change_runner
        from .ingest.teams import TeamsIngestor
        from .ingest.teams_signals import build_teams_corroboration_payload
    except Exception as e:  # noqa: BLE001 — Teams corroboration is optional.
        logger.warning("Teams connector import failed (non-blocking): %s", e)
        return {}

    ingestor = TeamsIngestor()
    collected: List[Dict[str, Any]] = []

    def _process_batch(batch) -> None:
        # Reach: collect the batch's records for the corroboration block.
        collected.extend(batch.records)
        # Depth (T2): hand this batch's conversation content to the retrieval
        # substrate beside the signal path. Guarded and non-blocking: reach and
        # depth share ONE checkpoint, so a content hand-off failure must not freeze
        # it or abort corroboration — logged (type name only, never the exception
        # str, which may carry a Bearer token) and the run continues.
        try:
            ingestor.ingest_deep_content(org_id, batch.records)
        except Exception as e:  # noqa: BLE001 — depth must not break reach/checkpoint
            logger.warning(
                "Teams deep-content hand-off failed (non-blocking) org=%s run=%s: [%s]",
                org_id, run_id, type(e).__name__,
            )

    try:
        result = change_runner.ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=_process_batch,
        )
    except Exception as e:  # noqa: BLE001 — belt-and-braces; the runner is non-raising.
        # Type name only — the exception str may carry a Bearer token from the
        # Graph client's request reprs.
        logger.warning(
            "Teams ingestion failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )
        return {}

    if result.error is not None:
        logger.warning(
            "Teams change ingest reported an error (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(result.error).__name__,
        )
    else:
        logger.info(
            "Teams change ingest: %s — %d batch(es), %d changed record(s), checkpoint_advanced=%s",
            "first run (streamed full load)" if result.first_run else "incremental",
            result.batches, result.records, result.checkpoint_advanced,
        )

    return build_teams_corroboration_payload(collected)


def _ingest_confluence_corroboration(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive Confluence change-based ingestion and build its corroboration block.

    R17-A2: Confluence is a connected knowledge SOURCE. When connected, drive it
    through the shared change runner (R16-A1) so — in one path —

      * the runner advances the per-(org, 'confluence') checkpoint (incremental
        content-modified delta, not a full re-read) and emits one
        ``ingestion.artifact_changed`` event per changed page/blog post (AC6),
        using the ``artifact_id`` + ``change_kind`` every record carries; and
      * the changed records are aggregated into the ``{activity, cross_references,
        stale_load_bearing}`` block downstream corroboration reads, wrapped under
        the ``'confluence'`` key.

    Non-blocking: any failure degrades to an empty block (`{}`) so a Confluence
    read never aborts the run (the runner swallows ingestion errors and leaves the
    checkpoint unadvanced for the next run to re-read).

    R18-A5 / T1 (AT-600): the SAME changed-record set collected below also drives
    the page/blogpost DEEP CONTENT hand-off to retrieval (``confluence_content.
    ingest_confluence_content``) — page bodies rendered to structure-preserving
    text and indexed with page-level provenance (AC1). This rides this checkpoint
    rather than opening a second one, and is itself non-blocking: a deep-content
    failure never affects the corroboration block returned here.
    """
    try:
        from .ingest import change_runner
        from .ingest.confluence import ConfluenceIngestor
        from .ingest.confluence_content import ingest_confluence_content
        from .ingest.confluence_signals import build_confluence_corroboration_payload
    except Exception as e:  # noqa: BLE001 — Confluence corroboration is optional.
        logger.warning("Confluence connector import failed (non-blocking): %s", e)
        return {}

    collected: List[Dict[str, Any]] = []
    ingestor = ConfluenceIngestor()
    try:
        result = change_runner.ingest_with_checkpoint(
            ingestor,
            org_id,
            process_batch=lambda batch: collected.extend(batch.records),
        )
    except Exception as e:  # noqa: BLE001 — belt-and-braces; the runner is non-raising.
        logger.warning(
            "Confluence ingestion failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )
        return {}

    if result.error is not None:
        logger.warning(
            "Confluence change ingest reported an error (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(result.error).__name__,
        )
    else:
        logger.info(
            "Confluence change ingest: %s — %d batch(es), %d changed record(s), checkpoint_advanced=%s",
            "first run (streamed full load)" if result.first_run else "incremental",
            result.batches, result.records, result.checkpoint_advanced,
        )

    try:
        content_result = ingest_confluence_content(org_id, collected, ingestor=ingestor)
        logger.info(
            "Confluence deep content: org=%s run=%s pages=%d handed_off=%d indexed=%d "
            "empty=%d failed=%d",
            org_id, run_id, content_result.pages_seen, content_result.artifacts_handed_off,
            content_result.artifacts_indexed, content_result.artifacts_empty,
            content_result.artifacts_failed,
        )
    except Exception as e:  # noqa: BLE001 — deep content must never block corroboration.
        logger.warning(
            "Confluence deep content hand-off failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
            exc_info=True,
        )

    return build_confluence_corroboration_payload(collected)


def _ingest_sharepoint_corroboration(org_id: str, run_id: str) -> Dict[str, Any]:
    """Drive SharePoint change-based ingestion and build its corroboration block.

    R17-A2: SharePoint is a connected document SOURCE. Like Teams it authenticates
    via Microsoft Graph and is driven through the shared change runner so — in one
    path — the per-(org, 'sharepoint') Graph delta checkpoint advances
    (incremental, not a full re-read), one ``ingestion.artifact_changed`` event is
    emitted per changed driveItem (AC6), and the changed records are aggregated
    into the ``{activity, cross_references, estates}`` block wrapped under the
    ``'sharepoint'`` key. Non-blocking: any failure degrades to an empty block so a
    SharePoint read never aborts the run.

    R18-A5 / T2 (AT-601): beside the reach signal path, the SharePoint site-page /
    list-text DEEP CONTENT path is driven too (``sharepoint_content.
    ingest_sharepoint_content``) — page/list bodies rendered to structure-preserving
    text and indexed with page-level provenance (AC1). Unlike Confluence (whose
    depth reuses the reach ingestor's records), SharePoint depth is a SEPARATE
    ``ChangeBasedIngestor`` (``connector_id='sharepoint_content'``) with its OWN
    ``(org, 'sharepoint_content')`` checkpoint, so it is invoked independently here
    rather than sharing the reach records. Non-blocking: a deep-content failure never
    affects the corroboration block returned here.
    """
    try:
        from .ingest import change_runner
        from .ingest.sharepoint import SharePointIngestor
        from .ingest.sharepoint_content import ingest_sharepoint_content
        from .ingest.sharepoint_signals import build_sharepoint_corroboration_payload
    except Exception as e:  # noqa: BLE001 — SharePoint corroboration is optional.
        logger.warning("SharePoint connector import failed (non-blocking): %s", e)
        return {}

    collected: List[Dict[str, Any]] = []
    try:
        result = change_runner.ingest_with_checkpoint(
            SharePointIngestor(),
            org_id,
            process_batch=lambda batch: collected.extend(batch.records),
        )
    except Exception as e:  # noqa: BLE001 — belt-and-braces; the runner is non-raising.
        # Type name only — the exception str may carry a Bearer token from the
        # Graph client's request reprs.
        logger.warning(
            "SharePoint ingestion failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )
        return {}

    if result.error is not None:
        logger.warning(
            "SharePoint change ingest reported an error (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(result.error).__name__,
        )
    else:
        logger.info(
            "SharePoint change ingest: %s — %d batch(es), %d changed record(s), checkpoint_advanced=%s",
            "first run (streamed full load)" if result.first_run else "incremental",
            result.batches, result.records, result.checkpoint_advanced,
        )

    try:
        content_result = ingest_sharepoint_content(org_id)
        logger.info(
            "SharePoint deep content: org=%s run=%s records=%d handed_off=%d indexed=%d "
            "empty=%d failed=%d",
            org_id, run_id, content_result.records, content_result.artifacts_handed_off,
            content_result.artifacts_indexed, content_result.artifacts_empty,
            content_result.artifacts_failed,
        )
    except Exception as e:  # noqa: BLE001 — deep content must never block corroboration.
        logger.warning(
            "SharePoint deep content hand-off failed (non-blocking) org=%s run=%s: [%s]",
            org_id, run_id, type(e).__name__,
        )

    return build_sharepoint_corroboration_payload(collected)


_ENTERPRISE_OPS_DEMO_PATH = (
    Path(__file__).parent / "ingest" / "fixtures" / "enterprise_ops_demo.json"
)


def _attach_enterprise_ops_demo(
    sn_data: Optional[Dict[str, Any]],
    jira_data: Optional[Dict[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Seed ENT-5 cross-system blocks from the demo fixture when they are absent.

    The enterprise_ops detectors consume blocks the live ServiceNow/Jira ingest
    does not yet compute: incident_resolution, change_correlation,
    sla_breach_by_team (+ the optional cor06_slack_escalation / team_entity_overlay
    corroboration blocks) on ServiceNow, and issue_resolution / team_backlog on
    Jira. Until that ingestion is built, this fills ONLY the missing blocks so the
    pack produces findings in both offline and live runs. Real data always wins —
    any block already present (e.g. once live computation exists) is left
    untouched. Non-blocking: any load error leaves the payloads unchanged.
    """
    sn = dict(sn_data or {})
    jira = dict(jira_data or {})
    try:
        with _ENTERPRISE_OPS_DEMO_PATH.open("r", encoding="utf-8") as fh:
            seed = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("enterprise_ops demo seed unavailable (non-blocking): %s", exc)
        return sn, jira

    seeded: List[str] = []
    for key, value in (seed.get("servicenow") or {}).items():
        if key.startswith("_"):
            continue
        if key not in sn:
            sn[key] = value
            seeded.append(f"servicenow.{key}")
    for key, value in (seed.get("jira") or {}).items():
        if key.startswith("_"):
            continue
        if key not in jira:
            jira[key] = value
            seeded.append(f"jira.{key}")

    if seeded:
        logger.info(
            "enterprise_ops: seeded demo blocks not produced by live ingest — %s",
            ", ".join(seeded),
        )
    return sn, jira


_DB_CONNECTOR_IDS = frozenset({"oracle_db", "postgresql"})


def _env_or_placeholder(
    env_name: str,
    placeholder: str,
    *,
    connector_id: str,
    mode: str,
) -> str:
    """Return an env value, warning before placeholder use outside offline mode."""
    value = os.environ.get(env_name)
    if value:
        return value

    if mode != "offline" or os.environ.get("REQUIRE_CONNECTOR_SECRETS") == "1":
        logger.warning(
            "%s is not configured for %s; using placeholder %r. "
            "Set %s before running live DB ingestion.",
            env_name,
            connector_id,
            placeholder,
            env_name,
        )
    return placeholder


def _build_db_config(connector_id: str, org_id: str, mode: str):
    """Build a DBConnectorConfig with explicit org_id and documented secret keys."""
    try:
        from connectors.db import DBConnectorConfig
    except ModuleNotFoundError:
        from backend.connectors.db import DBConnectorConfig

    if connector_id == "oracle_db":
        return DBConnectorConfig(
            connector_id="oracle_db",
            org_id=org_id,
            host=_env_or_placeholder(
                "ORACLE_HOST", "oracle.local", connector_id=connector_id, mode=mode
            ),
            port=int(os.environ.get("ORACLE_PORT", "1521")),
            database=os.environ.get("ORACLE_DATABASE", "ORCL"),
            driver="oracledb",
            username_key="ORACLE_DB_USERNAME",
            password_key="ORACLE_DB_PASSWORD",
        )
    if connector_id == "postgresql":
        return DBConnectorConfig(
            connector_id="postgresql",
            org_id=org_id,
            host=_env_or_placeholder(
                "POSTGRESQL_HOST", "postgres.local", connector_id=connector_id, mode=mode
            ),
            port=int(os.environ.get("POSTGRESQL_PORT", "5432")),
            database=os.environ.get("POSTGRESQL_DATABASE", "postgres"),
            driver="psycopg2",
            username_key="POSTGRESQL_USERNAME",
            password_key="POSTGRESQL_PASSWORD",
        )
    raise ValueError(f"Unsupported DB connector for sqlserver_opsignal pack: {connector_id}")


# Connected conversation / knowledge / engineering sources. They ingest once
# inside the runner (via the shared change runner or their pack) and are
# non-blocking, so — like the removed _probe_systems pre-pass did — they pass
# through the per-system summary as "ok" whenever connected.
_PASS_THROUGH_SOURCES = ("slack", "teams", "confluence", "sharepoint", "github")


def _build_ingest_summary(
    systems_set: set,
    systems_of_record: List[tuple],
) -> tuple:
    """Return (per_system, succeeded, errors) for a run — the single source of
    ingest truth that materialization used to compute with a second _probe_systems
    ingest pass.

    `systems_of_record` is a list of ``(name, ok, data, err)`` for Salesforce /
    ServiceNow / Jira. Semantics mirror the old probe: truthy data ⇒ "ok" and
    counted as succeeded; an exception or empty data ⇒ "failed"; a system not in
    the connected set ⇒ "skipped".
    """
    per_system = {s: "skipped" for s in ("salesforce", "servicenow", "jira")}
    succeeded: List[str] = []
    errors: Dict[str, str] = {}
    for name, ok, data, err in systems_of_record:
        if name not in systems_set:
            continue
        if not ok:
            per_system[name] = "failed"
            errors[name] = err or "ingest failed"
        elif data:
            per_system[name] = "ok"
            succeeded.append(name)
        else:
            per_system[name] = "failed"
            errors[name] = "ingest returned no data"
    for s in _PASS_THROUGH_SOURCES:
        if s in systems_set:
            per_system[s] = "ok"
            if s not in succeeded:
                succeeded.append(s)
    return per_system, succeeded, errors


def _resolve_ai_mode_and_provider() -> tuple[str, str]:
    """The active generation AI mode + provider name for billing (R-1.9.1-L2/T1).

    ``ai_mode`` is the configured generation-provider mode (``hosted`` |
    ``in_boundary`` | ``customer_tenant``); ``provider`` is that provider's
    concrete name, which coincides with the mode today but is emitted as its own
    field so the L2 usage report can evolve. Fully defensive: on any resolution
    failure it falls back to the ``MODEL_GENERATION_PROVIDER`` env (default
    ``hosted``) so a metering emit can never break a run.
    """
    try:
        from app.model_gateway import get_generation_provider

        name = get_generation_provider().name
    except Exception:
        name = os.getenv("MODEL_GENERATION_PROVIDER", "hosted") or "hosted"
    return name, name


def _emit_billing_run_completed(
    *,
    org_id: str,
    run_id: str,
    pack_id: Optional[str],
    deployment_type: Optional[str],
    started_at: str,
) -> None:
    """R-1.9.1-L2 / T1 (AC1): emit ``billing.run_completed`` into the immutable
    telemetry store for EVERY run, in every AI mode.

    Billability is DERIVED BY THE L2 USAGE REPORT, never decided here — this event
    is a neutral, complete record of what ran. ``connected_system_count`` is the
    org's connected Integration-Hub entities (the pricing definition), resolved via
    the same ``license_limits`` helper the connect gate uses; ``pack_ids`` is a list
    (forward-compatible with multi-pack runs). Fully fire-and-forget: any failure
    is logged and swallowed so metering never breaks or fails a discovery run.
    """
    try:
        ai_mode, provider = _resolve_ai_mode_and_provider()
        try:
            from app.license_limits import count_connected_systems

            connected_system_count: Optional[int] = count_connected_systems(org_id)
        except Exception:
            connected_system_count = None
        # R-1.9.1-L2 / T4 (AC4): stamp a per-org monotonic sequence number so a
        # billing event deleted from the store before report generation shows up
        # as a gap in the usage report's hash chain. Defensive — a counter hiccup
        # yields seq=None (the event is still emitted, just unsequenced).
        try:
            from app import billing_chain

            _seq: Optional[int] = billing_chain.next_seq(org_id)
        except Exception:
            _seq = None
        record_event(
            "billing.run_completed",
            {
                "run_id": run_id,
                "org_id": org_id,
                "ai_mode": ai_mode,
                "provider": provider,
                "connected_system_count": connected_system_count,
                "pack_ids": [pack_id] if pack_id else [],
                "deployment_type": deployment_type,
                "started_at": started_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "seq": _seq,
                "source": "run_pipeline",
            },
        )
    except Exception:  # pragma: no cover — metering must never break a run
        logger.warning(
            "billing.run_completed emit failed for run %s", run_id, exc_info=True
        )


def run(
    mode: Optional[str] = None,
    run_id: Optional[str] = None,
    org_id: str = "demo-org",
    systems: Optional[List[str]] = None,
    pack: Optional[str] = None,
    pack_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    # ENG-SHARED-1: resolve pack config — replaces temporary is_ncino_pack conditional
    # R191-P1 T2: a run selects one OR MORE packs. `pack_ids` (plural, R191-P1 T1)
    # is the multi-pack selection; the singular `pack` stays accepted as the
    # primary alias (CLI / older callers). Both fold into ONE order-preserving,
    # de-duplicated selection via the shared primitive. Each selected pack runs its
    # own detectors against the ONE shared normalised signal and is scored with its
    # OWN calibration (AC3); a single selection is byte-identical to the former
    # single-pack pipeline (AC2).
    from .packs.pack_config import (
        get_pack,
        get_pack_version,
        normalize_pack_ids,
        is_ncino_pack,
        is_sqlserver_opsignal_pack,
        is_github_engineering_pack,
        is_enterprise_ops_pack,
        is_cloud_ops_pack,
        is_security_ops_pack,
        is_financial_services_cloud_pack,
    )
    _selected_pack_args = normalize_pack_ids(
        list(pack_ids or []) + ([pack] if pack else [])
    )
    if not _selected_pack_args:
        # No selection → the historical default pack (get_pack(None)).
        _selected_pack_args = [None]

    # Resolve each selection to its REGISTERED config, de-duplicated by the
    # resolved packId (two unknown ids both fall back to the default → one pass),
    # order preserved. The first entry is the primary pack — every backward-
    # compatible scalar (packId / packVersion / detectorsExecuted / …) reports it,
    # so a single-pack run is unchanged.
    _pack_configs: List[Tuple[Optional[str], Dict[str, Any]]] = []
    _seen_pack_ids: set = set()
    for _sel in _selected_pack_args:
        _cfg = get_pack(_sel)
        _pid = _cfg["packId"]
        if _pid in _seen_pack_ids:
            continue
        _seen_pack_ids.add(_pid)
        _pack_configs.append((_sel, _cfg))

    primary_pack_arg, pack_config = _pack_configs[0]
    pack_id = pack_config["packId"]
    # R16-B1 §4: stamp the pack VERSION (not just the id) onto every opportunity
    # so governance/debugging can later tell a data change from a pack change.
    pack_version = get_pack_version(primary_pack_arg)
    primary_pack_id = pack_id

    # Union of pack DOMAINS across the whole selection drives the shared, run-once
    # ingestion below: ingest a pack-specific source when ANY selected pack needs
    # it (for a single pack this is identical to the former per-pack gate).
    _selected_domains = {cfg["domain"] for _, cfg in _pack_configs}
    _any_ncino = "ncino" in _selected_domains
    _any_strs = "strs_benefits" in _selected_domains
    _any_github = "github_engineering" in _selected_domains
    _any_enterprise_ops = "enterprise_ops" in _selected_domains
    _any_db_opsignal = "sqlserver_opsignal" in _selected_domains
    _any_security_ops = "security_ops" in _selected_domains
    _any_cloud_ops = "cloud_ops" in _selected_domains
    _any_fsc = "financial_services_cloud" in _selected_domains

    # Default to all systems if None
    if mode is None:
        mode = os.environ.get("INGEST_MODE", "offline").strip().lower()
        if mode not in ("offline", "live"):
            mode = "offline"

    _systems = set(systems) if systems else {"salesforce", "servicenow", "jira"}
    os.environ["INGEST_MODE"] = mode
    if run_id is None:
        run_id = f"run_{uuid.uuid4().hex[:8]}"

    _run_started_dt = datetime.now(timezone.utc)
    started_at = _run_started_dt.isoformat()
    logger.info(f"AgentIQ discovery runner — mode={mode} run_id={run_id} pack={pack_id}")

    # R-1.9.1-L1 / T6 (AC5): stamp the license deployment_type into this run's
    # telemetry context so L2 billing can record which AI deployment topology the
    # run executed under. Resolved once, defensively (lazy import + never raises),
    # so it can never break a run; None when the org has no verifiable license.
    try:
        from app.license_runtime import get_deployment_type
        _deployment_type = get_deployment_type(org_id)
    except Exception:  # pragma: no cover — a context read must never break a run
        _deployment_type = None

    record_event("run.started", {
        "org_id": org_id,
        "run_id": run_id,
        "source": "run_pipeline",
        "deployment_type": _deployment_type,
    })

    # 1. Ingest
    from .ingest import salesforce, servicenow, jira as jira_mod
    from .ingest.operational_config import CredentialRecordError
    from .ingest.salesforce import IngestError as SFError
    from .ingest.servicenow import ServiceNowIngestError as SNError
    from .ingest.jira import JiraIngestError

    sf_data, sn_data, jira_data = {}, {}, {}
    # Single-ingest: capture each system-of-record's ingest error so the runner
    # can return an accurate per-system summary (materialization no longer runs a
    # second _probe_systems ingest pass to build it).
    sf_err = sn_err = jira_err = None
    github_data: Dict[str, Any] = {}
    slack_data: Dict[str, Any] = {}
    java_data: Dict[str, Any] = {}
    dotnet_data: Dict[str, Any] = {}
    teams_data: Dict[str, Any] = {}
    confluence_data: Dict[str, Any] = {}
    sharepoint_data: Dict[str, Any] = {}
    secops_volume_measurements: Optional[Dict[str, Any]] = None
    ops_event_bridge_data: Dict[str, Any] = {
        "records": [],
        "health": {"status": "not_selected", "records": 0},
    }
    # MSP-B2: native Azure Event Connector output (same record shape as the bridge).
    azure_events_data: Dict[str, Any] = {
        "records": [],
        "health": {"status": "not_selected", "records": 0},
    }
    # MSP-B1: native AWS Event Connector output (same record shape as the bridge).
    aws_events_data: Dict[str, Any] = {
        "records": [],
        "health": {"status": "not_selected", "records": 0},
    }
    cloud_ops_runtime_health: Dict[str, Any] = {"status": "not_selected"}
    logger.info(f"Systems: {sorted(list(_systems))}")

    # CS-4 / AT-313: each ingest stage reports success to update_run_step via
    # ok=. A failed stage is recorded in failed_steps (still advancing
    # current_step) so the progress UI shows it as failed rather than as a
    # completed green-check stage. A skipped system (not in _systems) is not a
    # failure, so ok stays True.
    # Progress: mark each source step BEFORE its ingest so the Discovery Progress
    # UI shows it as in-progress (spinner) while the — potentially slow — live
    # fetch runs, then advances to the next step's spinner (which renders this one
    # as completed) when that ingest starts. The post-ingest update_run_step keeps
    # failure tracking (ok=False → failed_steps) and is a no-op on success.
    sf_ok = True
    try:
        if "salesforce" in _systems:
            update_run_step(run_id, "sf_crm")
            sf_data = salesforce.ingest()
            logger.info("Salesforce ingestion: OK")
            clear_connector_auth_failure(org_id, "salesforce")
    except (SFError, CredentialRecordError) as e:
        sf_ok = False
        sf_err = str(e)
        logger.error(f"Salesforce ingestion FAILED: {e}")
        # A 401 / INVALID_SESSION_ID means the token is dead — flag for Reconnect.
        flag_connector_auth_failure(org_id, "salesforce", e)
    update_run_step(run_id, "sf_crm", ok=sf_ok)

    sn_ok = True
    try:
        if "servicenow" in _systems:
            update_run_step(run_id, "sn")
            # MSP-B3 T5: regular ServiceNow data is still ingested once, while
            # CMDB CIs and relationships run through independent incremental
            # checkpoints whose callbacks persist graph state before advancing.
            sn_data = servicenow.ingest(include_cmdb=False)
            if sn_data:
                cmdb_data = servicenow.ingest_cmdb_changes(
                    org_id=org_id,
                    run_id=run_id,
                    class_scope=(
                        servicenow.DEFAULT_CMDB_CLASSES
                        if mode == "offline"
                        else None
                    ),
                )
                sn_data["cmdb"] = cmdb_data
                stream_errors = [
                    stream.get("error")
                    for stream in (cmdb_data.get("streams") or {}).values()
                    if stream.get("error")
                ]
                if stream_errors:
                    sn_ok = False
                    sn_err = "; ".join(stream_errors)
                from .signals.secops_volume import SecOpsVolumeStream

                cmdb_index = {
                    str(ci.get("sys_id")): {
                        "ci_class": ci.get("ci_class") or ci.get("sys_class_name")
                    }
                    for ci in (cmdb_data.get("configuration_items") or [])
                    if isinstance(ci, dict) and ci.get("sys_id")
                }
                secops_volume_stream = SecOpsVolumeStream(cmdb_index=cmdb_index)
                # MSP-B11 T1: Security Operations SIR workflow signal, on the
                # same incremental sys_updated_on rails. Additive (a new
                # sn_data["secops"] key, no existing consumer) and non-blocking:
                # a stream failure degrades ServiceNow to partial, never aborts.
                secops_data = servicenow.ingest_sir_changes(
                    org_id=org_id,
                    run_id=run_id,
                    volume_stream=secops_volume_stream,
                    handoff_security_notes=_any_security_ops,
                )
                sn_data["secops"] = secops_data
                # MSP-B11 T2: Vulnerability Response workflow signal — three
                # independently-checkpointed VR streams, same non-blocking rails.
                vr_data = servicenow.ingest_vr_changes(
                    org_id=org_id,
                    run_id=run_id,
                    volume_stream=secops_volume_stream,
                )
                sn_data["vulnerability_response"] = vr_data
                secops_volume_measurements = secops_volume_stream.measurements(
                    org_id
                ).to_dict()
                secops_data["volume"] = secops_volume_measurements
                vr_data["volume"] = secops_volume_measurements
                sn_data["secops_volume"] = secops_volume_measurements

                # MSP-B12 T3: persist the bounded B11 records behind their lean
                # evidence pointers. The API resolves one record at a time under
                # org + analyst RBAC and emits an audit event.
                if _any_security_ops:
                    try:
                        from .packs.security_ops_evidence_resolver import (
                            RunKVEvidenceRecordStore,
                            index_signal_records,
                        )

                        evidence_store = RunKVEvidenceRecordStore(run_id, org_id)
                        indexed = index_signal_records(
                            evidence_store, org_id, sn_data
                        )
                        evidence_store.flush()
                        sn_data["secops_evidence_resolution"] = {
                            "available": True,
                            "records_indexed": indexed,
                        }
                    except Exception as evidence_exc:  # noqa: BLE001
                        sn_data["secops_evidence_resolution"] = {
                            "available": False,
                            "records_indexed": 0,
                            "error": str(evidence_exc),
                        }
                        secops_data.setdefault("streams", {})[
                            "evidence_record_store"
                        ] = {"error": str(evidence_exc)}
                secops_streams = [
                    stream
                    for streams in (
                        secops_data.get("streams") or {},
                        vr_data.get("streams") or {},
                    )
                    for stream in streams.values()
                    if isinstance(stream, dict)
                ]
                secops_errors = [
                    stream.get("error")
                    for stream in secops_streams
                    if stream.get("error")
                ]
                # A table this instance does not expose (an unactivated
                # ServiceNow module such as Security Incident Response, or one
                # the integration role cannot read) is REPORTED, never counted
                # as a failure: no re-run fixes it, so failing the stage every
                # run would bury the conditions that do need attention.
                sn_unavailable = [
                    f"{stream.get('table') or stream.get('connector_id')}: "
                    f"{stream.get('unavailable_reason')}"
                    for stream in (
                        *(
                            s
                            for s in (cmdb_data.get("streams") or {}).values()
                            if isinstance(s, dict)
                        ),
                        *secops_streams,
                    )
                    if stream.get("status") == "unavailable"
                ]
                if sn_unavailable:
                    sn_data["servicenow_unavailable_tables"] = sn_unavailable
                    logger.info(
                        "ServiceNow: %d table(s) not available on this instance — %s",
                        len(sn_unavailable),
                        "; ".join(sn_unavailable),
                    )
                if secops_errors:
                    sn_ok = False
                    sn_err = "; ".join(
                        part for part in [sn_err, *secops_errors] if part
                    )
            if sn_data and sn_ok:
                logger.info("ServiceNow ingestion: OK")
            elif sn_data:
                logger.warning("ServiceNow ingestion: partial (%s)", sn_err)
            # Ingestion ran without an auth error (a genuine auth failure raises
            # SNError → the except branch below flags it), so clear any prior
            # connector auth-failure flag — a recovered ServiceNow stops prompting
            # Reconnect (dev: connector-auth-failure tracking), matching the
            # salesforce/jira clear calls above/below.
            clear_connector_auth_failure(org_id, "servicenow")
    except SNError as e:
        sn_ok = False
        sn_err = str(e)
        logger.error(f"ServiceNow ingestion FAILED: {e}")
        flag_connector_auth_failure(org_id, "servicenow", e)
    update_run_step(run_id, "sn", ok=sn_ok)

    jira_ok = True
    try:
        if "jira" in _systems:
            update_run_step(run_id, "jira")
            jira_data = jira_mod.ingest()
            if jira_data: logger.info("Jira ingestion: OK")
            clear_connector_auth_failure(org_id, "jira")
    except JiraIngestError as e:
        jira_ok = False
        jira_err = str(e)
        logger.error(f"Jira ingestion FAILED: {e}")
        flag_connector_auth_failure(org_id, "jira", e)
    update_run_step(run_id, "jira", ok=jira_ok)

    # MSP-B8: staged AWS/Azure event histories are an internal Cloud Operations
    # source. Drive them whenever any selected pack needs cloud_ops, independent
    # of the external systems list, and before the no-data guard so a bridge-only
    # cloud run is still allowed to reach detector evaluation.
    if _any_cloud_ops:
        ops_event_bridge_data = _ingest_ops_event_bridge(org_id, run_id)

    # MSP-B2: the NATIVE Azure Event Connector is the live counterpart of the B8
    # bridge — the SAME OperationalEvent record shape, its own (org, "azure_events")
    # checkpoint. It runs only when the Azure connector is connected+selected AND a
    # cloud_ops pack is selected (so its events are actually consumed), and its
    # records feed the SAME cloud-ops assembly seam as the bridge, where the
    # OpsEventStream folds duplicate signatures — so native + bridge never
    # double-count. Non-blocking, exactly like the bridge.
    if _any_cloud_ops and "azure_events" in _systems:
        update_run_step(run_id, "azure_events")
        azure_events_data = _ingest_azure_events(org_id, run_id)
        update_run_step(
            run_id,
            "azure_events",
            ok=_cloud_event_step_ok(azure_events_data),
        )

    # MSP-B1: the NATIVE AWS Event Connector — the AWS half of the B1/B2 pair, and
    # the live counterpart of the B8 bridge for AWS. Identical gating and posture
    # to Azure above: it runs only when the AWS connector is connected+selected AND
    # a cloud_ops pack is selected (so its events are actually consumed), its own
    # (org, "aws_events") checkpoint, and its records feed the SAME cloud-ops
    # assembly seam — where the OpsEventStream folds duplicate signatures, so a
    # native event and its bridged twin never double-count. Non-blocking.
    if _any_cloud_ops and "aws_events" in _systems:
        update_run_step(run_id, "aws_events")
        aws_events_data = _ingest_aws_events(org_id, run_id)
        update_run_step(
            run_id,
            "aws_events",
            ok=_cloud_event_step_ok(aws_events_data),
        )

    # Single-ingest: materialization now hands the runner ALL connected systems
    # (not just the ones a probe pre-pass confirmed had data), so guard against
    # aborting a run that still has usable data. Abort only when NO system of
    # record produced anything — the same net outcome the old probe+succeeded
    # path produced (a Salesforce-empty run still ran ServiceNow/Jira detectors).
    if (
        "salesforce" in _systems
        and not sf_data
        and not sn_data
        and not jira_data
        and not ops_event_bridge_data.get("records")
        and not azure_events_data.get("records")
        and not aws_events_data.get("records")
    ):
        logger.error("No system-of-record data available — cannot run detectors. Aborting.")
        try:
            _elapsed_ms = int((datetime.now(timezone.utc) - _run_started_dt).total_seconds() * 1000)
        except Exception:
            _elapsed_ms = None
        record_event("run.completed", {
            "org_id": org_id,
            "run_id": run_id,
            "source": "run_pipeline",
            "duration_ms": _elapsed_ms,
            "success": False,
            "count": 0,
            "pack_id": pack_id,
            "system_count": len(_systems),
            "deployment_type": _deployment_type,
        })
        # R-1.9.1-L2 / T1 (AC1): the billing record is emitted for EVERY run,
        # including this aborted-early (no source data) one.
        _emit_billing_run_completed(
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            deployment_type=_deployment_type,
            started_at=started_at,
        )
        empty = _empty_run(run_id, org_id, mode, started_at)
        _ps, _succ, _errs = _build_ingest_summary(
            _systems,
            [
                ("salesforce", sf_ok, sf_data, sf_err),
                ("servicenow", sn_ok, sn_data, sn_err),
                ("jira", jira_ok, jira_data, jira_err),
            ],
        )
        empty["perSystem"], empty["succeeded"], empty["ingestErrors"] = _ps, _succ, _errs
        empty["cloudOpsRuntime"] = {
            "eventBridge": dict(ops_event_bridge_data.get("health") or {}),
            "azureEvents": dict(azure_events_data.get("health") or {}),
            "awsEvents": dict(aws_events_data.get("health") or {}),
            "assembly": cloud_ops_runtime_health,
        }
        return empty

    # MSP-B3 T4: current-scope CMDB nodes must exist before detector evaluation
    # so ServiceNow incident signals can carry an exact CI entity identifier.
    # The later full extraction pass confirms the same source entities without
    # duplicating them. Failure is non-blocking and leaves incidents unresolved;
    # it must never trigger a guessed name- or text-based join.
    if sn_data:
        try:
            from app.entity_extractor import prepare_servicenow_ci_resolution

            prepare_servicenow_ci_resolution(
                org_id=org_id,
                run_id=run_id,
                sn_data=sn_data,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ServiceNow incident-to-CI preparation failed (non-blocking): "
                "run_id=%s org_id=%s error=%s",
                run_id,
                org_id,
                exc,
            )

    # 2-pre. Slack change ingest — R16-A2 / AT-421 (T6) + AT-419 (T4).
    # Slack is a connected SOURCE, so it ingests here — after the systems of
    # record (Salesforce CRM / ServiceNow / Jira) and BEFORE the pack-specific
    # second Salesforce pass (sf_ncino) below — so the Discovery Progress list
    # shows all connected sources first, then the selected pack. When Slack is
    # connected, ingest it through the shared change runner: this advances the
    # per-(org, 'slack') checkpoint (incremental, not a full re-read) and emits an
    # ingestion.artifact_changed event per changed Slack artifact (AC7). The
    # changed records are also aggregated into the corroboration block so Slack's
    # escalation pattern (+ activity / cross-references) can corroborate findings
    # from systems of record. Gated on "slack" ∈ connected systems (the engine
    # only reads the block when Slack is connected). The MEDIUM ceiling —
    # Slack-only stays MEDIUM, never standalone HIGH; it elevates only WITH a
    # primary corroborator (COR-06) — is enforced by the engine and the T3 clamp.
    if "slack" in _systems:
        update_run_step(run_id, "slack")
        slack_data = _ingest_slack_corroboration(org_id, run_id) or {}
        if slack_data.get("slack", {}).get("escalation_pattern", {}).get("fired"):
            logger.info("Slack corroboration: escalation pattern present for this run")
        else:
            logger.info("Slack corroboration: no escalation pattern this run (supporting signal only)")

    # 2-pre. Teams change ingest — R17-A1 / AT-435 (T6) + AT-433 (T4).
    # Like Slack, Teams is a connected conversation SOURCE. When connected, drive
    # it through the shared change runner so changed Teams artifacts emit
    # ingestion.artifact_changed events (AC7) and the per-(org, 'teams') Graph
    # delta checkpoint advances (incremental, not a full re-read). The changed
    # records are aggregated into the corroboration block so Teams escalation can
    # corroborate findings from systems of record — capped at MEDIUM unless a
    # primary corroborator is present (the conversation-source ceiling, AC6).
    if "teams" in _systems:
        update_run_step(run_id, "teams")
        teams_data = _ingest_teams_corroboration(org_id, run_id) or {}
        if teams_data.get("teams", {}).get("escalation_pattern", {}).get("fired"):
            logger.info("Teams corroboration: escalation pattern present for this run")
        else:
            logger.info("Teams corroboration: no escalation pattern this run (supporting signal only)")

    # 2-pre. Confluence & SharePoint change ingest — R17-A2. Both are connected
    # knowledge/document SOURCES driven through the shared change runner (like
    # Slack/Teams): changed pages/documents emit ingestion.artifact_changed events
    # and advance the per-(org, connector) checkpoint, and the changed records are
    # aggregated into the connector's corroboration block. Gated on the connector
    # being in the org's connected/live systems.
    if "confluence" in _systems:
        update_run_step(run_id, "confluence")
        confluence_data = _ingest_confluence_corroboration(org_id, run_id) or {}
        logger.info(
            "Confluence ingest: %d space activity block(s) this run",
            len(confluence_data.get("confluence", {}).get("activity", {})),
        )
    if "sharepoint" in _systems:
        update_run_step(run_id, "sharepoint")
        sharepoint_data = _ingest_sharepoint_corroboration(org_id, run_id) or {}
        logger.info(
            "SharePoint ingest: %d library activity block(s) this run",
            len(sharepoint_data.get("sharepoint", {}).get("activity", {})),
        )

    # 2a. nCino ingest — if ncino pack, fetch lending signals from nCino objects
    from .packs.pack_config import is_ncino_pack as _is_ncino
    if _any_ncino and "salesforce" in _systems:
        ncino_ok = True
        try:
            from .ingest.ncino import ingest as ncino_ingest
            # NOTE (reverts the CS-4/AT-310 reuse): sf_data["approval_processes"]
            # is an AGGREGATED per-process-name summary produced by
            # salesforce.get_approval_pending() (process_name / pending_count /
            # approver_count) — NOT raw ProcessInstance rows. nCino's
            # loan-approval detector needs raw rows (TargetObjectId, CompletedDate,
            # SubmittedById) from its own CreatedDate-windowed query. The two SOQL
            # queries select different columns and different filters
            # (Status='Pending' vs CreatedDate=LAST_N_DAYS:90), so they were never
            # genuine duplicates. Forwarding the summary silently zeroed nCino's
            # approval signal (no TargetObjectId to match), so nCino fetches its
            # own ProcessInstance rows. Costs one extra SOQL query per run;
            # correctness over the saved call.
            ncino_data = ncino_ingest()
            # Merge ncino data into sf_data so detectors can find it
            if sf_data is None:
                sf_data = {}
            sf_data["ncino"] = ncino_data
            logger.info("nCino ingestion: OK — %d lending metrics", len(ncino_data))
        except Exception as e:
            ncino_ok = False
            logger.warning("nCino ingestion failed (non-blocking): %s", e)
        update_run_step(run_id, "sf_ncino", ok=ncino_ok)

    # 2a-ii. Financial Services Cloud ingest (2.0-D1 T2) — FSC managed-package
    # objects (FinServ__Referral__c, FinServ__FinancialAccount__c and their
    # histories) alongside the standard-object reads, normalised into ONE
    # detector-visible block at sf_data["fsc"]. Mirrors the nCino block above:
    # gated on the pack being selected AND Salesforce being connected, merged into
    # sf_data, and NON-BLOCKING — an FSC ingest failure degrades this pack's
    # signal rather than aborting a run that may also be running other packs.
    if _any_fsc and "salesforce" in _systems:
        fsc_ok = True
        try:
            from .ingest.fsc import ingest as fsc_ingest
            fsc_data = fsc_ingest()
            if sf_data is None:
                sf_data = {}
            sf_data["fsc"] = fsc_data
            logger.info(
                "FSC ingestion: OK — %d servicing-request type(s), %d referral "
                "route(s), %d review type(s), %d queue(s), %d rework pair(s)",
                len(fsc_data.get("servicing_requests", [])),
                len(fsc_data.get("referral_handoffs", [])),
                len(fsc_data.get("approval_reviews", [])),
                len(fsc_data.get("service_queues", [])),
                len(fsc_data.get("cross_object_rework", [])),
            )
            _fsc_meta = fsc_data.get("_meta", {}) or {}
            if _fsc_meta.get("unavailable_objects"):
                logger.warning(
                    "FSC ingestion: object(s) unavailable in this org — %s. The "
                    "signals that read them degrade to empty.",
                    _fsc_meta["unavailable_objects"],
                )
            if _fsc_meta.get("record_types_unresolved"):
                logger.warning(
                    "FSC ingestion: %s case(s) had an unresolvable RecordType and "
                    "were excluded from scope rather than assumed in-scope.",
                    _fsc_meta["record_types_unresolved"],
                )
        except Exception as e:
            fsc_ok = False
            logger.warning("FSC ingestion failed (non-blocking): %s", e)
        update_run_step(run_id, "sf_fsc", ok=fsc_ok)

    # 2b. STRS Benefits ingest — if strs_benefits pack
    from .packs.pack_config import is_strs_benefits_pack as _is_strs
    if _any_strs and "salesforce" in _systems:
        try:
            from .ingest.strs_benefits import ingest as strs_ingest
            strs_data = strs_ingest()
            if sf_data is None:
                sf_data = {}
            sf_data["strs_benefits"] = strs_data
            logger.info("STRS Benefits ingestion: OK — %d benefit metrics", len(strs_data))
        except Exception as e:
            logger.warning("STRS Benefits ingestion failed (non-blocking): %s", e)

    # 2c. GitHub ingest — when the github_engineering pack is active OR GitHub is
    # a connected system for this run (T1-S12). GitHub is a connected SOURCE like
    # Slack/Teams (see _PASS_THROUGH_SOURCES), so — consistent with every other
    # source, which ingests on membership in _systems — it is ingested whenever it
    # is connected, not only for the engineering pack. Its signals feed the
    # github_engineering detectors when that pack is active and otherwise flow into
    # source payloads / corroboration. Non-blocking: ingest failure never aborts
    # the run. Jira is still ingested above when in _systems so the pack's
    # confidence-elevation corroboration can run.
    if _any_github or "github" in _systems:
        update_run_step(run_id, "github")
        github_data = _ingest_github(org_id, run_id) or {}
        if github_data:
            # Log degraded state per sub-signal so a pagination failure in one
            # signal (e.g. stale_branches) is reported independently and does
            # not imply the other signals are also degraded.
            _GITHUB_SUB_SIGNALS = {
                "pr_review":            "GITHUB_PR_REVIEW_BOTTLENECK",
                "commit_concentration": "GITHUB_COMMIT_CONCENTRATION",
                "stale_branches":       "GITHUB_STALE_BRANCHES",
            }
            for sub_key, detector_id in _GITHUB_SUB_SIGNALS.items():
                if (github_data.get(sub_key) or {}).get("degraded_signal", False):
                    logger.warning(
                        "GitHub ingestion: %s signal degraded — %s detector will not fire",
                        sub_key,
                        detector_id,
                    )
                else:
                    logger.info("GitHub ingestion: %s signal OK", sub_key)
        else:
            logger.warning("GitHub ingestion: empty payload — all three detectors will not fire")

    # 2d. Enterprise Operations ingest — if enterprise_ops pack (ENT-5).
    # The three cross-system detectors read blocks (incident_resolution,
    # change_correlation, sla_breach_by_team on ServiceNow; issue_resolution,
    # team_backlog on Jira) that the live ServiceNow/Jira ingest does not yet
    # compute. In OFFLINE mode we seed the missing blocks from the demo fixture so
    # the pack produces deterministic findings. In LIVE mode we use only real
    # ServiceNow/Jira data — no fixture seeding — so these detectors fire only
    # once the live block computation exists. Real computed data always wins:
    # the seed fills only blocks not already present.
    if _any_enterprise_ops and str(mode).strip().lower() != "live":
        sn_data, jira_data = _attach_enterprise_ops_demo(sn_data, jira_data)

    # 2d. Oracle DB ingest  — T2-S12-A: sqlserver_opsignal pack + oracle_db connector.
    # 2e. PostgreSQL ingest — T2-S12-A: sqlserver_opsignal pack + postgresql connector.
    from .packs.pack_config import is_sqlserver_opsignal_pack as _is_db_opsignal
    db_data: Dict[str, Any] = {}
    _db_connector_id: Optional[str] = None

    if _any_db_opsignal:
        active_db_connectors = sorted(_systems & _DB_CONNECTOR_IDS)
        if len(active_db_connectors) > 1:
            logger.warning(
                "sqlserver_opsignal supports one DB connector per run; got %s. "
                "Skipping DB ingestion to avoid mixed signal_source labels.",
                active_db_connectors,
            )
        elif "oracle_db" in active_db_connectors:
            try:
                try:
                    from connectors.db.oracle_ingestor import ingest as _oracle_ingest
                except ModuleNotFoundError:
                    from backend.connectors.db.oracle_ingestor import ingest as _oracle_ingest
                db_data = _oracle_ingest(
                    org_id=org_id,
                    run_id=run_id,
                    config=_build_db_config("oracle_db", org_id, mode),
                )
                _db_connector_id = "oracle_db"
                db_data["connector_id"] = _db_connector_id
                logger.info("Oracle DB ingestion: OK")
            except Exception as e:
                logger.warning("Oracle DB ingestion failed (non-blocking): %s", e)

        elif "postgresql" in active_db_connectors:
            try:
                try:
                    from connectors.db.postgresql_ingestor import ingest as _pg_ingest
                except ModuleNotFoundError:
                    from backend.connectors.db.postgresql_ingestor import ingest as _pg_ingest
                db_data = _pg_ingest(
                    org_id=org_id,
                    run_id=run_id,
                    config=_build_db_config("postgresql", org_id, mode),
                )
                _db_connector_id = "postgresql"
                db_data["connector_id"] = _db_connector_id
                logger.info("PostgreSQL ingestion: OK")
            except Exception as e:
                logger.warning("PostgreSQL ingestion failed (non-blocking): %s", e)

    # (Slack change ingest runs earlier — before the pack-specific second
    # Salesforce pass — so every connected source appears in Discovery Progress
    # ahead of the selected pack. See the "Slack change ingest" block above.)

    # 2g. Java application change ingest — R17-A3 / T1+T5+T6 (AgentIQ's first
    # non-SaaS enterprise source). When a Java app is connected, ingest its
    # operational surface (framework health/diagnostics + logs) through the shared
    # change runner: this advances the per-(org, 'java_app') checkpoint
    # (incremental, not a full re-read), emits an ingestion.artifact_changed event
    # per changed operational artifact (AC6), and aggregates the changed records
    # into the corroboration block so Java-app operational friction can corroborate
    # findings from other systems (COR-09, AC5). Operational surface only —
    # no source code (AC8). Gated on "java_app" ∈ connected systems.
    if "java_app" in _systems:
        update_run_step(run_id, "java_app")
        java_data = _ingest_java_app_corroboration(org_id, run_id) or {}
        if java_data.get("java_app", {}).get("operational_friction", {}).get("fired"):
            logger.info("Java app corroboration: operational friction present for this run")
        else:
            logger.info("Java app corroboration: no operational friction this run")

    # 2h. .NET application change ingest — R17-A4 / T1+T5+T6. The .NET counterpart
    # to the Java app source above: same shared change runner, same shared
    # operational-signal extraction, differing only at the collection edge
    # (.NET health/diagnostics + EventCounters + .NET log formats). Advances the
    # per-(org, 'dotnet_app') checkpoint (incremental, not a full re-read), emits
    # an ingestion.artifact_changed event per changed operational artifact (AC7),
    # and aggregates the changed records into the corroboration block so .NET-app
    # operational friction can corroborate findings from other systems (COR-10,
    # AC6). Operational surface only — no source code (AC8). Gated on "dotnet_app"
    # ∈ connected systems.
    if "dotnet_app" in _systems:
        update_run_step(run_id, "dotnet_app")
        dotnet_data = _ingest_dotnet_app_corroboration(org_id, run_id) or {}
        if dotnet_data.get("dotnet_app", {}).get("operational_friction", {}).get("fired"):
            logger.info(".NET app corroboration: operational friction present for this run")
        else:
            logger.info(".NET app corroboration: no operational friction this run")

    # MSP-B4/B5/B8 production seam. B3 CI resolution has already run above, and
    # connected knowledge sources have now ingested, so recurrence enrichment and
    # runbook matching see the fullest current-run context. The assembled block is
    # exactly what the existing Cloud Operations detectors consume.
    if _any_cloud_ops:
        try:
            from .cloud_ops_runtime import build_cloud_ops_runtime

            if not isinstance(sn_data, dict):
                sn_data = {}
            sn_data.setdefault("org_id", org_id)
            # Native Azure events (MSP-B2) and staged bridge events (MSP-B8) are the
            # SAME OperationalEvent record shape and are merged into ONE assembly
            # call. The runtime's OpsEventStream folds identical event_signatures, so
            # a native event and its bridged twin collapse to one signal rather than
            # double-counting (MSP §15 transport equivalence).
            _cloud_event_records = (
                list(ops_event_bridge_data.get("records") or ())
                + list(azure_events_data.get("records") or ())
                + list(aws_events_data.get("records") or ())
            )
            runtime = build_cloud_ops_runtime(
                org_id,
                sn_data,
                bridge_records=_cloud_event_records,
                bridge_health=ops_event_bridge_data.get("health"),
            )
            sn_data["cloud_ops"] = runtime.block
            cloud_ops_runtime_health = runtime.health
            logger.info(
                "Cloud Operations runtime assembly: status=%s recurrences=%d "
                "routing_loops=%d event_signatures=%d",
                runtime.health.get("status"),
                len(runtime.block.get("recurrence_records") or ()),
                len(runtime.block.get("oscillation_records") or ()),
                len(runtime.block.get("event_signatures") or ()),
            )
            # Objective 2: record the assembled signature rows so they survive the
            # run. Read-only over `runtime.block`, after every detector input is
            # final — nothing downstream observes this call.
            _persist_cloud_ops_event_signatures(run_id, runtime.block)
        except Exception as exc:  # noqa: BLE001
            cloud_ops_runtime_health = {
                "status": "unavailable",
                "reason": type(exc).__name__,
            }
            logger.warning(
                "Cloud Operations runtime assembly failed (non-blocking): [%s]",
                type(exc).__name__,
            )

    # 2. Context
    org_ctx = build_org_context(sf_data, sn_data, jira_data)

    # 3. Detect + Score — R191-P1 T2: multi-pack execution.
    # Each selected pack runs its OWN detectors against the ONE shared normalised
    # signal ingested above, and its OWN scorer calibration is applied to its OWN
    # findings — the impact scorer NEVER blends calibrations across packs (AC3).
    # A single-pack run is byte-identical to the former pipeline (AC2): the pass
    # body below is exactly the previous single-pack logic with `pack_id` bound to
    # the current pack. Overlapping opportunities from two packs stay two findings,
    # each carrying its own packId — no cross-pack merging (AC4, explicit non-goal).
    from .packs.pack_config import is_ncino_pack

    # ── Shared, pack-independent setup (runs ONCE for the whole run) ──
    # R16-C1 T1: Stack Builder weighting context (run-level, pack-independent).
    from .weighting_context import load_for_run as _load_weighting_context
    _weighting_ctx = _load_weighting_context(run_id)

    # R16-C2 T2: selected Discovery Focus (run-level, pack-independent). Additive
    # emphasis annotation only — never mutates a scoring field.
    from .packs.focus_affinity import (
        load_focus_for_run as _load_focus_for_run,
        build_focus_emphasis as _build_focus_emphasis,
    )
    _focus_id = _load_focus_for_run(run_id)

    # Scorers — one family per pack; each pack selects its own inside the pass so
    # calibrations never mix (AC3). ENG-AIQ-NC-4.
    from .scorer import score as sc_score
    from .lending_scorer import score_lending, is_lending_detector
    from .strs_benefits_scorer import score_strs_benefits, is_strs_benefits_detector
    from .packs.sqlserver_opsignal_scorer import (
        score_sqlserver_opsignal,
        is_sqlserver_opsignal_detector,
    )
    from .packs.github_engineering_scorer import (
        score_github_engineering,
        is_github_engineering_detector,
    )
    from .packs.enterprise_ops_scorer import (
        score_enterprise_ops,
        is_enterprise_ops_detector,
    )
    from .packs.cloud_ops_scorer import (
        score_cloud_ops,
        is_cloud_ops_detector,
        rank_cloud_ops_findings,
    )
    from .packs.security_ops_scorer import (
        score_security_ops,
        is_security_ops_detector,
        rank_security_ops_findings,
    )
    # 2.0-D1 T3: FSC scoring calibration (per-detector base scores in the scorer's
    # _FSC_SCORES with inline provenance; the three dimension weights in pack config).
    from .packs.financial_services_cloud_scorer import (
        score_financial_services_cloud,
        is_financial_services_cloud_detector,
        rank_fsc_findings,
    )
    from .evidence_builder import build_evidence
    # R16-B1 (T3): stable cross-run opportunity identity, computed at assembly.
    from .opportunity_identity import (
        compute_opportunity_identity,
        primary_entity_keys_for_detector,
    )
    # ENT-2: shared cross-system corroboration engine (non-pack-specific).
    # Imported defensively so a failure to import never breaks the run.
    try:
        try:
            from backend.app.corroboration_engine import (
                evaluate_corroboration,
                apply_corroboration_confidence,
                build_corroboration_run_data,
            )
        except ModuleNotFoundError:
            from app.corroboration_engine import (
                evaluate_corroboration,
                apply_corroboration_confidence,
                build_corroboration_run_data,
            )
        _corroboration_available = True
    except Exception as _corr_imp_err:  # noqa: BLE001 — corroboration is optional.
        logger.warning("ENT-2 corroboration engine unavailable (non-blocking): %s", _corr_imp_err)
        _corroboration_available = False

    # ONE shared evidence-id counter for the whole run so evidence ids stay
    # globally unique ACROSS packs (a multi-pack run must never collide ids).
    id_counter = itertools.count(1)
    def id_factory() -> str: return f"{run_id[-6:]}_{next(id_counter):04d}"

    _run_ts_iso = _run_started_dt.isoformat()

    # ── Per-pack execution pass ──
    # Runs ONE selected pack end-to-end against the shared signal and returns its
    # findings plus its execution metadata. Everything here is scoped to the
    # current pack (`pack_id`/`pack_version`/`pack_config`) so two packs in one run
    # never share detector lists, calibration, or by-detector corroboration maps.
    def _run_pack_pass(
        current_pack: Optional[str],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        pack_config = get_pack(current_pack)
        pack_id = pack_config["packId"]
        pack_version = get_pack_version(current_pack)

        # pack-driven detector selection — pack_config.py (ENG-SHARED-1) defines
        # which detectors each pack activates.
        if _is_db_opsignal(pack_id):
            # DB operational signal detectors — shared across SQL Server, Oracle, PostgreSQL (T2-S12-A)
            from .detectors import (
                db_ticket_volume_surge,
                db_sla_breach_rate,
                db_queue_depth_elevated,
            )
            all_detectors = [
                db_ticket_volume_surge,
                db_sla_breach_rate,
                db_queue_depth_elevated,
            ]
            logger.info("Pack: sqlserver_opsignal — 3 DB operational signal detectors active (connector=%s)", _db_connector_id or "none")
        elif is_ncino_pack(pack_id):
            # nCino lending detectors — confirmed objects from SF-NC-2
            from .detectors import (
                loan_origination_routing_friction,
                covenant_tracking_gap,
                checklist_bottleneck,
                spreading_bottleneck,
                approval_bottleneck,
            )
            all_detectors = [
                loan_origination_routing_friction,
                covenant_tracking_gap,
                checklist_bottleneck,
                spreading_bottleneck,
                approval_bottleneck,
            ]
            logger.info("Pack: ncino — 5 lending detectors active")
        elif is_financial_services_cloud_pack(pack_id):
            # 2.0-D1 T2 — five FSC detectors reading sf_data["fsc"].
            from .detectors import (
                fsc_servicing_request_recurrence,
                fsc_referral_handoff_friction,
                fsc_approval_review_cycle,
                fsc_service_queue_ageing,
                fsc_cross_object_rework,
            )
            all_detectors = [
                fsc_servicing_request_recurrence,
                fsc_referral_handoff_friction,
                fsc_approval_review_cycle,
                fsc_service_queue_ageing,
                fsc_cross_object_rework,
            ]
            logger.info("Pack: financial_services_cloud — 5 FSC detectors active")
        elif _is_strs(pack_id):
            from .detectors import (
                application_stall,
                benefit_election_deadline,
                disbursement_overdue,
                disability_review_bottleneck,
            )
            all_detectors = [
                application_stall,
                benefit_election_deadline,
                disbursement_overdue,
                disability_review_bottleneck,
            ]
            logger.info("Pack: strs_benefits — 4 benefit detectors active")
        elif is_github_engineering_pack(pack_id):
            from .detectors import (
                github_pr_bottleneck,
                github_commit_concentration,
                github_stale_branches,
            )
            all_detectors = [
                github_pr_bottleneck,
                github_commit_concentration,
                github_stale_branches,
            ]
            logger.info("Pack: github_engineering — 3 engineering signal detectors active")
        elif is_enterprise_ops_pack(pack_id):
            from .detectors import (
                ent_incident_resolution_lag,
                ent_change_incident_correlation,
                ent_sla_breach_by_team,
            )
            all_detectors = [
                ent_incident_resolution_lag,
                ent_change_incident_correlation,
                ent_sla_breach_by_team,
            ]
            logger.info("Pack: enterprise_ops — 3 cross-system detectors active")
        elif is_cloud_ops_pack(pack_id):
            from .detectors import (
                cloud_ops_recurring_resolution_loop,
                cloud_ops_alert_triage_toil,
                cloud_ops_reassignment_ping_pong,
                cloud_ops_queue_ageing,
                cloud_ops_shared_ci_hotspot,
                cloud_ops_runbook_documentation_gap,
            )
            all_detectors = [
                cloud_ops_recurring_resolution_loop,
                cloud_ops_alert_triage_toil,
                cloud_ops_reassignment_ping_pong,
                cloud_ops_queue_ageing,
                cloud_ops_shared_ci_hotspot,
                cloud_ops_runbook_documentation_gap,
            ]
            logger.info("Pack: cloud_ops — 6 operations detectors active")
        elif is_security_ops_pack(pack_id):
            from .detectors import (
                security_ops_remediation_recurrence,
                security_ops_security_it_pingpong,
                security_ops_sla_deferral_ageing,
                security_ops_shared_infra_concentration,
                security_ops_sir_triage_toil,
            )
            all_detectors = [
                security_ops_remediation_recurrence,
                security_ops_security_it_pingpong,
                security_ops_sla_deferral_ageing,
                security_ops_shared_infra_concentration,
                security_ops_sir_triage_toil,
            ]
            logger.info("Pack: security_ops — 5 SecOps detectors active")
        else:
            # Service Cloud detectors — default
            from .detectors import (
                repetition, handoff_friction, approval_delay,
                knowledge_gap, integration_concentration,
                permission_bottleneck, cross_system_echo,
            )
            all_detectors = [repetition, handoff_friction, approval_delay, knowledge_gap,
                             integration_concentration, permission_bottleneck, cross_system_echo]
            logger.info("Pack: service_cloud — 7 SC detectors active")

        # Capture fired and non-firing detector evaluations before scoring.
        # DB and GitHub packs read their signal from the first positional arg.
        # Keep Service Cloud, nCino, and STRS on Salesforce-shaped data.
        if _is_db_opsignal(pack_id):
            primary_data = db_data
        elif is_github_engineering_pack(pack_id):
            primary_data = github_data
        else:
            primary_data = sf_data

        # Mark "detect" before the phase so the Pattern Detection step shows as
        # in-progress while detectors run (it renders completed once "enrich" starts).
        update_run_step(run_id, "detect")
        detector_results, all_evaluated = _run_detector_phase(
            all_detectors,
            primary_data,
            sn_data,
            jira_data,
        )

        # Preserve each operational pack's contract boundary independently. A
        # combined run must not let one pack weaken or replace the other.
        if is_cloud_ops_pack(pack_id):
            from .packs.cloud_ops_finding import enforce_pack_findings

            _validated = enforce_pack_findings(detector_results)
            logger.info(
                "Pack: cloud_ops — four-part contract enforced on %d finding(s)",
                _validated,
            )

        # 2.0-D1 T2: the FSC pack's four-part contract AND its AC5 aggregation
        # floor are enforced at the pack boundary. Deliberately NOT wrapped in a
        # try/except — a finding missing a contract part, or one that names an
        # individual, must fail the run rather than reach a report.
        if is_financial_services_cloud_pack(pack_id):
            from .packs.fsc_finding import enforce_pack_findings

            _validated = enforce_pack_findings(detector_results)
            logger.info(
                "Pack: financial_services_cloud — four-part contract and "
                "no-individuals floor enforced on %d finding(s)",
                _validated,
            )

        if is_security_ops_pack(pack_id):
            from .packs.security_ops_ai_mode import apply_ai_mode_gate

            _gate = apply_ai_mode_gate(detector_results)
            logger.info(
                "Pack: security_ops — AI-mode gate: mode=%s ai_assembly=%s labelled=%d/%d",
                _gate["mode"],
                _gate["ai_assembly_allowed"],
                _gate["labelled"],
                _gate["count"],
            )

            from .packs.security_ops_finding import enforce_pack_findings

            _validated = enforce_pack_findings(detector_results)
            logger.info(
                "Pack: security_ops — four-part contract enforced on %d finding(s)",
                _validated,
            )

            from .packs.security_ops_aggregation_floor import enforce_pack_output

            _swept = enforce_pack_output(detector_results)
            logger.info(
                "Pack: security_ops — aggregation floor swept %d output(s)",
                _swept,
            )

        pack_executed_at = _snapshot_detector_evaluations(
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            detector_results=detector_results,
            all_evaluated=all_evaluated,
        )
        # Snapshot persistence is deliberately non-blocking and test/integration
        # adapters may return None. Pack provenance must still receive a stable,
        # valid timestamp instead of failing the otherwise successful run.
        if not isinstance(pack_executed_at, datetime):
            pack_executed_at = datetime.now(timezone.utc)
        executed_detector_ids = _record_pack_execution(
            org_id=org_id,
            run_id=run_id,
            pack_id=pack_id,
            pack_name=str(pack_config.get("packName") or pack_id),
            pack_version=pack_version,
            detectors=all_detectors,
            evaluated_count=len(all_evaluated),
            executed_at=pack_executed_at,
        )

        update_run_step(run_id, "enrich")

        try:
            # Entity extraction is synchronous and DB-safe in this context: every
            # resolve_or_create_entity() opens its own short-lived raw sqlite3
            # connection via db.connect(), commits, and closes it (see
            # entity_resolution._connect). There is no SQLAlchemy session or
            # thread-local state to leak across an async boundary — unlike the
            # GitHub ingest above, this call needs no event-loop isolation.
            from app.entity_extractor import extract_entities
            entities = extract_entities(
                org_id=org_id,
                run_id=run_id,
                pack_id=pack_id,
                detector_results=detector_results,
                ingestor_data={
                    "salesforce": sf_data,
                    "servicenow": sn_data,
                    "jira": jira_data,
                },
            ) or []
        except Exception as e:
            entities = []
            logger.warning(
                "Entity extraction failed (non-blocking): run_id=%s error=%s",
                run_id,
                e,
            )

        # T3-S13-A T6: map relationships AFTER extract_entities() — both mapping
        # passes draw edges only between the resolved entity rows written during
        # extraction. map_relationships() is the single entry point (it calls
        # map_directly_observed() + map_inferred_from_detectors() and emits the
        # relationship.mapping_completed telemetry on success). Non-blocking: a
        # failure here must never break opportunity delivery, so the run still
        # completes and OppEnrichment.relationships simply defaults to empty (AC9).
        try:
            from app.relationship_mapper import map_relationships
            if not entities:
                logger.warning("map_relationships skipped: no entities from extraction")
            map_relationships(
                org_id=org_id,
                run_id=run_id,
                ingestor_data={
                    "salesforce": sf_data,
                    "servicenow": sn_data,
                    "jira": jira_data,
                },
                detector_results=detector_results,
                entities=entities,
            )
        except Exception as e:
            logger.warning(
                "Relationship mapping failed (non-blocking): run_id=%s org_id=%s error=%s",
                run_id,
                org_id,
                e,
            )

        # 2.0-B2 T4 (AC3): refresh the cross-source match proposals for this org.
        # Placed AFTER relationship mapping because the ranked engine's tier-3
        # corroboration reads the observed edges written above — scanning earlier
        # would judge the graph as it was before this run.
        #
        # Writes nothing to the graph: the engine only decides (T1), and a pair a
        # human has already confirmed or rejected is never re-proposed (the store
        # keys decisions on a stable source identity, so a decision survives entity
        # row ids changing between runs). Non-blocking for the same reason entity
        # extraction is: a review queue is not worth failing a run over.
        try:
            from app.entity_match_proposals import scan_for_proposals

            _proposal_outcome = scan_for_proposals(org_id)
            if (
                _proposal_outcome.created
                or _proposal_outcome.skipped_already_decided
            ):
                logger.info(
                    "2.0-B2 cross-source match proposals: run_id=%s org_id=%s "
                    "created=%d refreshed=%d already_decided=%d",
                    run_id,
                    org_id,
                    _proposal_outcome.created,
                    _proposal_outcome.refreshed,
                    _proposal_outcome.skipped_already_decided,
                )
        except Exception as e:
            logger.warning(
                "Cross-source match proposal scan failed (non-blocking): "
                "run_id=%s org_id=%s error=%s",
                run_id,
                org_id,
                e,
            )

        # Issue 3 fix: collect Jira/SN lending correlation by detector for ncino pack.
        # Wave 2 (ENG-AIQ-NC-2/NC-3) built lending_correlation — wire it into evidence here.
        jira_by_detector: Dict[str, List[str]] = {}
        sn_by_detector:   Dict[str, List[str]] = {}
        if is_ncino_pack(pack_id):
            if jira_data:
                jira_by_detector = (
                    jira_data.get("lending_correlation", {}).get("by_detector", {})
                )
            if sn_data:
                sn_by_detector = (
                    sn_data.get("lending_correlation", {}).get("by_detector", {})
                )

        # ── STRS Benefits corroboration — ENG-STRS-CORR-1/2 (Fix Pack Sprint 7) ──
        # Same pattern as nCino above. strs_benefits.py ingest() now returns
        # jira_strs_correlation and sn_strs_correlation inside the metrics dict,
        # which is merged into sf_data["strs_benefits"]. Extract by_detector here.
        if _is_strs(pack_id):
            strs_metrics = sf_data.get("strs_benefits", {})
            jira_by_detector = (
                strs_metrics.get("jira_strs_correlation", {}).get("by_detector", {})
            )
            sn_by_detector = (
                strs_metrics.get("sn_strs_correlation", {}).get("by_detector", {})
            )
            if jira_by_detector:
                logger.info(
                    "STRS Jira corroboration: %d detectors have Jira evidence",
                    len(jira_by_detector),
                )
            if sn_by_detector:
                logger.info(
                    "STRS ServiceNow corroboration: %d detectors have SN evidence",
                    len(sn_by_detector),
                )

        # ── ENT-2: build the corroboration run_data for THIS pack ──
        # Scoped per pack: shared connected-systems + source payloads combined with
        # this pack's own by-detector maps (detector ids are disjoint across packs,
        # so no cross-pack blending is possible). Maps already-extracted Jira/
        # ServiceNow correlation by detector and carries Slack (AT-419 / T4) /
        # Confluence corroboration blocks through when an upstream connector payload
        # provides them. connected_systems drives COR-08 (single-source no
        # elevation). This only ever ELEVATES confidence downstream — never downgrades.
        _corr_run_data: Dict[str, Any] = {"connected_systems": sorted(_systems)}
        if _corroboration_available:
            try:
                _corr_run_data = build_corroboration_run_data(
                    systems=_systems,
                    sn_by_detector=sn_by_detector,
                    jira_by_detector=jira_by_detector,
                    run_timestamp_iso=_run_ts_iso,
                    source_payloads=[sf_data, sn_data, jira_data, github_data, db_data, slack_data, teams_data, java_data, dotnet_data],
                )
            except Exception as _corr_data_err:  # noqa: BLE001 — non-blocking.
                logger.warning("ENT-2 corroboration run_data build failed (non-blocking): %s", _corr_data_err)

        _cloud_ops_ranking: Dict[int, Dict[str, Any]] = {}
        if is_cloud_ops_pack(pack_id):
            try:
                _cloud_ops_ranking = rank_cloud_ops_findings(detector_results)
            except Exception as _rank_err:  # noqa: BLE001 — ranking is non-blocking.
                logger.warning(
                    "cloud_ops ops-impact ranking failed (non-blocking): %s",
                    _rank_err,
                )

        _security_ops_ranking: Dict[int, Dict[str, Any]] = {}
        if is_security_ops_pack(pack_id):
            try:
                _security_ops_ranking = rank_security_ops_findings(detector_results)
            except Exception as _rank_err:  # noqa: BLE001 - ranking is non-blocking.
                logger.warning(
                    "security_ops impact ranking failed (non-blocking): %s",
                    _rank_err,
                )

        # 2.0-D1 T3: FSC ops-impact ranking, computed once per run so the three
        # config-weighted dimensions normalise across the whole finding SET.
        _fsc_ranking: Dict[int, Dict[str, Any]] = {}
        if is_financial_services_cloud_pack(pack_id):
            try:
                _fsc_ranking = rank_fsc_findings(detector_results)
            except Exception as _rank_err:  # noqa: BLE001 — ranking is non-blocking.
                logger.warning(
                    "financial_services_cloud ops-impact ranking failed "
                    "(non-blocking): %s",
                    _rank_err,
                )

        pack_opportunities: List[Dict[str, Any]] = []
        for dr in detector_results:
            # Select scorer based on pack — this pack scores ONLY its own detectors
            # with its OWN calibration; a two-key guard (pack AND detector family)
            # means no pack ever applies another pack's calibration (AC3, no blending).
            if is_ncino_pack(pack_id) and is_lending_detector(dr.detector_id):
                scored = score_lending(dr)
            elif _is_strs(pack_id) and is_strs_benefits_detector(dr.detector_id):
                scored = score_strs_benefits(dr)
            elif is_sqlserver_opsignal_pack(pack_id) and is_sqlserver_opsignal_detector(dr.detector_id):
                scored = score_sqlserver_opsignal(dr)
            elif is_github_engineering_pack(pack_id) and is_github_engineering_detector(dr.detector_id):
                # T7/AT-191: PR-bottleneck confidence elevates MEDIUM->HIGH when Jira
                # corroborates. jira_connected mirrors how sources_connected.jira is derived.
                scored = score_github_engineering(
                    dr,
                    jira_data=jira_data,
                    org_id=org_id,
                    jira_connected=bool(jira_data),
                )
            elif is_enterprise_ops_pack(pack_id) and is_enterprise_ops_detector(dr.detector_id):
                # AT-266 T5: ENT_INCIDENT_RESOLUTION_LAG elevates MEDIUM->HIGH via COR-06
                # (ENT-2); ENT_SLA_BREACH_BY_TEAM elevates via ENT-1 entity overlay
                # (result already in dr.raw_evidence, read by scorer).
                scored = score_enterprise_ops(
                    dr,
                    sn_data=sn_data,
                    jira_data=jira_data,
                    org_id=org_id,
                )
            elif is_cloud_ops_pack(pack_id) and is_cloud_ops_detector(dr.detector_id):
                scored = score_cloud_ops(dr, ranking=_cloud_ops_ranking)
            elif is_security_ops_pack(pack_id) and is_security_ops_detector(dr.detector_id):
                scored = score_security_ops(dr, ranking=_security_ops_ranking)
            elif (
                is_financial_services_cloud_pack(pack_id)
                and is_financial_services_cloud_detector(dr.detector_id)
            ):
                # 2.0-D1 T3: FSC-specific calibration. Same two-key guard as every
                # other pack, so no pack ever applies another pack's calibration.
                scored = score_financial_services_cloud(dr, ranking=_fsc_ranking)
            else:
                # R16-C1 T1: pass weighting context so the scorer can read
                # role/priority for dr.signal_source (modulation is T2 work).
                scored = sc_score(dr, weighting_context=_weighting_ctx)

            # ── ENT-2: cross-system corroboration (shared engine, non-blocking) ──
            # Evaluate corroboration AFTER the detector fired and the scorer ran,
            # BEFORE final confidence is locked into the opportunity. Corroboration
            # may only ELEVATE confidence (apply_corroboration_confidence never
            # downgrades), so existing pack behaviour is preserved. Any failure is
            # logged and the scorer's confidence is used unchanged (AC10).
            corr_fields = {
                "corroboration_sources": [],
                "corroboration_label": None,
                "triple_corroboration": False,
                "corroboration_rule_ids": [],
            }
            if _corroboration_available:
                try:
                    _corr = evaluate_corroboration(
                        detector_id=dr.detector_id,
                        pack_id=pack_id,
                        run_data=_corr_run_data,
                        run_timestamp=_run_started_dt,
                        org_id=org_id,
                        # R16-C1 T1: pass weighting context so the corroboration
                        # engine can read role/priority per system.
                        weighting_context=_weighting_ctx,
                    )
                    scored["confidence"] = apply_corroboration_confidence(
                        scored.get("confidence", "MEDIUM"), _corr
                    )
                    corr_fields = {
                        "corroboration_sources": list(_corr.corroboration_sources),
                        "corroboration_label": _corr.corroboration_label,
                        "triple_corroboration": bool(_corr.triple_corroboration),
                        "corroboration_rule_ids": list(_corr.rule_ids),
                    }
                    if _corr.corroboration_sources:
                        logger.info(
                            "  %s: corroboration %s -> %s via %s",
                            dr.detector_id,
                            _corr.original_confidence,
                            scored.get("confidence"),
                            _corr.rule_ids,
                        )
                except Exception as _corr_err:  # noqa: BLE001 — corroboration is optional.
                    logger.warning(
                        "ENT-2 corroboration failed for %s (non-blocking): %s",
                        dr.detector_id, _corr_err,
                    )

            # Pass packId so build_evidence uses nCino banking-language builders
            scored_with_pack = {**scored, "packId": pack_id}
            evidence_list = build_evidence(dr, scored_with_pack, id_factory=id_factory)

            # Issue 3 fix: attach Jira/SN corroboration evidence for ncino pack.
            # These appear as additional evidence items in S4 alongside nCino evidence.
            # Does not yet modulate confidence — deferred to post-Sprint 5.
            if is_ncino_pack(pack_id) or _is_strs(pack_id):
                corroboration_count = 0
                for snippet in jira_by_detector.get(dr.detector_id, []):
                    ev_id = id_factory()
                    evidence_list.append({
                        "id":          ev_id,
                        "tsLabel":     "",
                        "source":      "Jira",
                        "detectorId":  dr.detector_id,
                        "evidenceType":"Metric",
                        "title":       f"Jira corroboration: {dr.detector_id}",
                        "snippet":     snippet,
                        "entities":    [],
                        "confidence":  "MEDIUM",
                        "decision":    "UNREVIEWED",
                        # R191-P1 T3: this evidence item is constructed inline
                        # (not via evidence_builder.build_evidence()), so it
                        # needs its own packId stamp for the same provenance
                        # guarantee every other evidence item carries.
                        "packId":      pack_id,
                    })
                    corroboration_count += 1
                for snippet in sn_by_detector.get(dr.detector_id, []):
                    ev_id = id_factory()
                    evidence_list.append({
                        "id":          ev_id,
                        "tsLabel":     "",
                        "source":      "ServiceNow",
                        "detectorId":  dr.detector_id,
                        "evidenceType":"Metric",
                        "title":       f"ServiceNow corroboration: {dr.detector_id}",
                        "snippet":     snippet,
                        "entities":    [],
                        "confidence":  "MEDIUM",
                        "decision":    "UNREVIEWED",
                        "packId":      pack_id,
                    })
                    corroboration_count += 1
                if corroboration_count > 0:
                    logger.info("  %s: +%d corroborating evidence items (Jira/SN)",
                                dr.detector_id, corroboration_count)
            # ── R16-B1 (T3): stable, cross-run opportunity identity ──
            # Derived ONLY from run-invariant inputs (org, pack, detector/signal,
            # resolved primary entity keys) so the same real-world problem carries
            # the same id run after run. Because pack_id is an identity input, the
            # same detector under two packs yields two distinct identities — the
            # no-cross-pack-merge guarantee at the identity layer (AC4).
            opportunity_identity = compute_opportunity_identity(
                org_id=org_id,
                pack_id=pack_id,
                signal_key=dr.detector_id,
                primary_entity_ids=primary_entity_keys_for_detector(
                    dr.detector_id, dr.signal_source
                ),
            )

            opp = {
                "runId": run_id, "orgId": org_id, "detector_id": dr.detector_id,
                "packId": pack_id, "opportunity_identity": opportunity_identity,
                "packVersion": pack_version,
                "signal_source": dr.signal_source, "metric_value": dr.metric_value,
                "threshold": dr.threshold, "impact": scored["impact"], "effort": scored["effort"],
                "confidence": scored["confidence"], "tier": scored["tier"],
                "roadmap_stage": scored["roadmap_stage"], "evidenceIds": [e["id"] for e in evidence_list],
                "evidence": evidence_list, "raw_evidence": dr.raw_evidence, "score_debug": scored["score_debug"],
                # ENT-2 cross-system corroboration fields (always present; safe defaults).
                "corroboration_sources": corr_fields["corroboration_sources"],
                "corroboration_label": corr_fields["corroboration_label"],
                "triple_corroboration": corr_fields["triple_corroboration"],
                "corroboration_rule_ids": corr_fields["corroboration_rule_ids"],
                # R16-C2 T2: additive Discovery Focus emphasis annotation (always
                # present; descriptive only — never mutates scoring fields).
                "focus_emphasis": _build_focus_emphasis(_focus_id, dr.detector_id),
            }
            if "ops_impact_score" in scored:
                opp["ops_impact_score"] = scored["ops_impact_score"]
                opp["ops_impact_rank"] = scored.get("ops_impact_rank")
            # ENG-AIQ-NC-5 Issue 1: inject approved UI labels from pack UI label files.
            # Deterministic config text — not LLM generated:
            #   title      → s6_title   (S6 opportunity card heading)
            #   category   → s7_category (S7 detail panel category)
            #   description → s6_desc   (S6 one-line description)
            # LLM-generated narrative (from run_llm_enrichment):
            #   aiSummary / aiWhyBullets / aiRisks / aiSuggestedNextSteps → S4
            #   s9_roadmap label seeds the LLM blueprint prompt → S9
            #   s10_exec label seeds the LLM exec summary prompt → S10
            from .packs.pack_config import get_ui_labels
            ui_labels = get_ui_labels(pack_id) or {}
            if ui_labels:
                det_labels = ui_labels.get(dr.detector_id, {})
                opp["title"]       = det_labels.get("s6_title", dr.detector_id)
                opp["category"]    = det_labels.get("s7_category", "Automation Opportunity")
                opp["description"] = det_labels.get("s6_desc", "")
                opp["s9_roadmap"]  = det_labels.get("s9_roadmap", "")
                opp["s10_exec"]    = det_labels.get("s10_exec", "")
                opp["compliance_guardrail"] = det_labels.get("compliance_guardrail")

            pack_opportunities.append(opp)

        return pack_opportunities, {
            "packId": pack_id,
            "packName": str(pack_config.get("packName") or pack_id),
            "packVersion": pack_version,
            "detectorsExecuted": executed_detector_ids,
            "packExecutedAt": pack_executed_at.isoformat(),
        }

    # ── Run every selected pack against the ONE shared normalised signal ──
    # Each pass returns its own findings (each stamped with its packId) and its
    # execution metadata; the findings concatenate — no cross-pack merging (AC4).
    opportunities: List[Dict[str, Any]] = []
    pack_execution_meta: List[Dict[str, Any]] = []
    for _pack_arg, _ in _pack_configs:
        _pack_opps, _pack_meta = _run_pack_pass(_pack_arg)
        opportunities.extend(_pack_opps)
        pack_execution_meta.append(_pack_meta)

    # Primary pack = first selection. The backward-compatible scalar fields below
    # report it, so a single-pack run is byte-identical to the former pipeline (AC2).
    _primary_meta = pack_execution_meta[0]

    try:
        _elapsed_ms = int((datetime.now(timezone.utc) - _run_started_dt).total_seconds() * 1000)
    except Exception:
        _elapsed_ms = None
    record_event("run.completed", {
        "org_id": org_id,
        "run_id": run_id,
        "source": "run_pipeline",
        "duration_ms": _elapsed_ms,
        "success": True,
        "count": len(opportunities),
        "pack_id": primary_pack_id,
        "system_count": len(_systems),
        "deployment_type": _deployment_type,
    })
    # R-1.9.1-L2 / T1 (AC1): billing record for the completed run (every AI mode).
    _emit_billing_run_completed(
        org_id=org_id,
        run_id=run_id,
        pack_id=pack_id,
        deployment_type=_deployment_type,
        started_at=started_at,
    )

    update_run_step(run_id, "complete")

    # Single-ingest: hand materialization the per-system status it used to build
    # with a second (discarded) ingest pass. Derived from this run's actual
    # ingest results.
    _per_system, _succeeded, _ingest_errors = _build_ingest_summary(
        _systems,
        [
            ("salesforce", sf_ok, sf_data, sf_err),
            ("servicenow", sn_ok, sn_data, sn_err),
            ("jira", jira_ok, jira_data, jira_err),
        ],
    )

    return {
        "runId": run_id, "orgId": org_id, "mode": mode,
        "packId": _primary_meta["packId"],
        # R16-C2 T2: surface the selected focus so the seed/ranking path can
        # apply focus emphasis deterministically (None => unbiased view).
        "focusId": _focus_id,
        "packVersion": _primary_meta["packVersion"],
        "packName": _primary_meta["packName"],
        "detectorsExecuted": _primary_meta["detectorsExecuted"],
        "packExecutedAt": _primary_meta["packExecutedAt"],
        # R191-P1 T2: full multi-pack execution surface. For a single-pack run
        # these carry exactly one entry and the scalar fields above mirror it.
        "packIds": [m["packId"] for m in pack_execution_meta],
        "packVersions": {m["packId"]: m["packVersion"] for m in pack_execution_meta},
        "packs": pack_execution_meta,
        "startedAt": started_at, "completedAt": datetime.now(timezone.utc).isoformat(),
        "inputs": org_ctx, "opportunities": opportunities,
        "perSystem": _per_system,
        "succeeded": _succeeded,
        "ingestErrors": _ingest_errors,
        "secopsVolume": secops_volume_measurements,
        "cloudOpsRuntime": {
            "eventBridge": dict(ops_event_bridge_data.get("health") or {}),
            "azureEvents": dict(azure_events_data.get("health") or {}),
            "awsEvents": dict(aws_events_data.get("health") or {}),
            "assembly": cloud_ops_runtime_health,
        },
    }

def _empty_run(run_id: str, org_id: str, mode: str, started_at: str) -> Dict:
    return {"runId": run_id, "orgId": org_id, "mode": mode, "startedAt": started_at,
            "completedAt": datetime.now(timezone.utc).isoformat(), "inputs": {}, "opportunities": [],
            "perSystem": {}, "succeeded": [], "ingestErrors": {}}

def main():
    parser = argparse.ArgumentParser(description="AgentIQ discovery runner")

    # Dynamically read INGEST_MODE from environment, fallback to "offline"
    default_mode = os.environ.get("INGEST_MODE", "offline").strip().lower()
    if default_mode not in ("offline", "live"):
        default_mode = "offline"

    parser.add_argument("--mode", choices=["offline", "live"], default=default_mode)
    parser.add_argument("--systems", help="Comma-separated list of systems (e.g. salesforce,jira)")
    parser.add_argument("--pack", default=None, help="Pack ID: service_cloud (default) or ncino")
    parser.add_argument("--pack-ids", default=None, help="R191-P1: comma-separated pack IDs for a multi-pack run (e.g. service_cloud,github_engineering)")
    parser.add_argument("--output", help="Output JSON file path")
    parser.add_argument("--run-id", help="Explicit run ID")
    parser.add_argument("--org-id", default="demo-org")
    parser.add_argument("--output-format", choices=["internal", "track_a_seed"], default="internal")

    args = parser.parse_args()

    # Parse systems string into a list if provided
    systems_list = None
    if args.systems:
        systems_list =[s.strip().lower() for s in args.systems.split(",") if s.strip()]

    # R191-P1 T2: parse an optional multi-pack selection.
    pack_ids_list = None
    if args.pack_ids:
        pack_ids_list = [p.strip() for p in args.pack_ids.split(",") if p.strip()]

    payload = run(
        mode=args.mode,
        run_id=args.run_id,
        org_id=args.org_id,
        systems=systems_list,
        pack=args.pack,
        pack_ids=pack_ids_list,
    )

    if args.output_format == "track_a_seed":
        payload = export_track_a_seed(payload)

    # 2.0-B1 T5 (AC5): this CLI writes the FULL run payload (opportunities +
    # evidence) to disk, so it is an export path — redact secrets and enforce
    # the 1.9 aggregation floor before anything is serialised.
    from .export_safety import guard_exported_payload
    payload = guard_exported_payload(payload, where="runner CLI payload export")

    out = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(out, encoding="utf-8")
        logger.info(f"Output written to {args.output}")
    else:
        print(out)

if __name__ == "__main__":
    main()
