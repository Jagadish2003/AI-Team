import time
from typing import Any, Dict, List, Tuple

from discovery.packs.security_ops_aggregation_floor import (
    SecOpsAggregationFloorViolation,
    assert_output_safe,
)

from . import db

SECURITY_OPS_PACK_ID = "security_ops"


def _now_epoch() -> int:
    return int(time.time())


def set_status(run_id: str, payload: Dict[str, Any]) -> None:
    db.run_kv_set("status", run_id, payload)


def get_status(run_id: str) -> Dict[str, Any]:
    run = db.run_get(run_id)
    started_at = run.get("startedAt", db.now_iso()) if run else db.now_iso()

    return db.run_kv_get(
        "status",
        run_id,
        {
            "runId": run_id,
            "status": "running",
            "startedAt": started_at,
            "updatedAt": started_at,
        },
    )


def _pack_id_for_run(run: Dict[str, Any] | None) -> str | None:
    if not run:
        return None
    inputs = run.get("inputs") or {}
    input_pack_id = inputs.get("packId") if isinstance(inputs, dict) else None
    return input_pack_id or run.get("packId") or None


def _pack_ids_for_run(run: Dict[str, Any] | None) -> List[str] | None:
    """R191-P1 T2: the multi-pack selection for a run, if one was configured.

    Reads the plural ``packIds`` (R191-P1 T1) from the run inputs first, then the
    run record. Returns ``None`` when no multi-pack selection is present — the
    runner then falls back to the singular ``pack`` (byte-identical single-pack
    behaviour for every pre-multi-pack run).
    """
    if not run:
        return None
    inputs = run.get("inputs") or {}
    input_pack_ids = inputs.get("packIds") if isinstance(inputs, dict) else None
    ids = input_pack_ids or run.get("packIds")
    if isinstance(ids, list):
        cleaned = [str(p) for p in ids if str(p).strip()]
        if cleaned:
            return cleaned
    return None


def resolve_effective_pack(
    explicit_pack_id: str | None, live_systems: List[str] | None
) -> str | None:
    """Choose the pack for a run, defaulting a GitHub-connected run to its pack.

    An explicit pack (Stack Builder selection / run input) ALWAYS wins. Otherwise,
    when the GitHub connector is among the org's live systems, default to
    ``github_engineering`` so a GitHub-connected run fires the GitHub detectors
    instead of falling back to ``service_cloud`` (which ingests nothing for GitHub
    and would leave the connection a no-op). Returns ``None`` when neither applies
    — the runner then falls back to ``service_cloud`` as before.
    """
    if explicit_pack_id:
        return explicit_pack_id
    if live_systems and "github" in live_systems:
        return "github_engineering"
    return None


def _org_id_for_run(run: Dict[str, Any] | None, fallback: str | None = None) -> str | None:
    if not run:
        return fallback
    inputs = run.get("inputs") or {}
    input_org_id = None
    if isinstance(inputs, dict):
        input_org_id = inputs.get("orgId") or inputs.get("org_id")
    return run.get("orgId") or run.get("org_id") or input_org_id or fallback


def _audit_prepend(run_id: str, event: Dict[str, Any]) -> None:
    audit = db.run_kv_get("audit", run_id, [])
    db.run_kv_set("audit", run_id, [event] + audit)


def _audit_event(action: str, by: str = "System") -> Dict[str, Any]:
    return {
        "id": f"aud_{_now_epoch()}",
        "tsLabel": db.now_iso(),
        "tsEpoch": _now_epoch(),
        "action": action,
        "by": by,
    }


def _secops_seed_slice(
    opportunities: List[Dict[str, Any]],
    evidence: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return only the Security Operations materialization slice."""
    secops_opps = [
        opportunity
        for opportunity in opportunities
        if opportunity.get("packId") == SECURITY_OPS_PACK_ID
    ]
    evidence_ids = {
        evidence_id
        for opportunity in secops_opps
        for evidence_id in (opportunity.get("evidenceIds") or [])
    }
    secops_evidence = [
        item
        for item in evidence
        if item.get("packId") == SECURITY_OPS_PACK_ID
        or item.get("id") in evidence_ids
    ]
    return secops_opps, secops_evidence


def _assert_secops_materialized(
    value: Any,
    *,
    where: str,
    enabled: bool,
) -> None:
    if enabled:
        assert_output_safe(value, where=where)


def _ingest_summary_from_payload(
    payload: Dict[str, Any], systems: List[str]
) -> Tuple[Dict[str, str], List[str], Dict[str, str]]:
    """Single-ingest: derive (per_system, succeeded, errors) from the runner payload.

    Replaces the removed ``_probe_systems`` pre-pass. The discovery runner
    (``discovery.runner.run``) is now the SOLE place enterprise systems are
    ingested; it returns the per-system status in its payload, so materialization
    no longer ingests Salesforce/ServiceNow/Jira a second time (discarding the
    data) just to build this summary and the "any data?" gate. Falls back to a
    conservative all-skipped map if an older/empty payload lacks the keys.
    """
    per_system = payload.get("perSystem") or {
        s: "skipped" for s in ["salesforce", "servicenow", "jira"]
    }
    succeeded = list(payload.get("succeeded") or [])
    errors = dict(payload.get("ingestErrors") or {})
    return per_system, succeeded, errors


def _finalise(
    run_id: str,
    run: Dict[str, Any],
    status: str,
    mode: str,
    systems: List[str],
    per_system: Dict[str, str],
    counts: Dict[str, int],
    errors: Dict[str, str],
    audit_action: str,
) -> None:
    set_status(
        run_id,
        {
            "runId": run_id,
            "status": status,
            "modeUsed": mode,
            "systemsUsed": systems,
            "perSystem": per_system,
            "counts": counts,
            "errors": errors,
            "updatedAt": db.now_iso(),
        },
    )
    _audit_prepend(run_id, _audit_event(audit_action))
    run["status"] = status
    run["updatedAt"] = db.now_iso()
    # A materialised run (complete/partial) has finished the whole pipeline, so
    # its stored current_step must read "complete" — otherwise this run_set (which
    # writes the whole run dict, possibly carrying a stale earlier current_step)
    # reverts the runner's own end-of-pipeline stamp, and the Discovery Progress
    # UI shows an early step still spinning on an already-finished run. A failed
    # run keeps the step it failed at.
    if status in ("complete", "completed", "partial"):
        run["current_step"] = "complete"
    db.run_set(run_id, run)


def _emit_event(run_id: str, stage: str, message: str, level: str = "INFO") -> None:
    event = {
        "id": f"ev_{int(time.time() * 1000)}_{stage}",
        "tsLabel": db.now_iso(),
        "stage": stage,
        "message": message,
        "level": level,
    }
    events = db.kv_get(f"events:{run_id}") or []
    db.kv_set(f"events:{run_id}", events + [event])


def _selected_system_ids_for_report(
    run_id: str,
    run: Dict[str, Any],
    run_inputs: Dict[str, Any],
    systems: List[str],
) -> List[str]:
    setup_ctx = db.run_kv_get("setup_context", run_id, {})
    candidates = [
        setup_ctx.get("selected_system_ids") if isinstance(setup_ctx, dict) else None,
        run.get("selectedSystemIds") if isinstance(run, dict) else None,
        run_inputs.get("connectedSources") if isinstance(run_inputs, dict) else None,
        systems,
    ]

    for candidate in candidates:
        if isinstance(candidate, list) and candidate:
            return candidate
    return []


def run_trackb_and_persist(
    run_id: str, mode: str, systems: List[str], run_inputs: Dict[str, Any]
) -> None:
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"Starting trackb materialization for run {run_id} in mode: {mode}")

    run = db.run_get(run_id)
    if run is None:
        raise RuntimeError(f"Run '{run_id}' not found — cannot materialise")

    # CS-2: prefer the org's authenticated connectors. If the user connected
    # Salesforce / ServiceNow / Jira via the Integration Hub OAuth flow, ingest
    # LIVE from exactly those connectors using their stored OAuth tokens —
    # instead of the offline-fixture default. Falls back to the requested
    # mode/systems when nothing is authenticated (offline demo path unchanged).
    org_id = _org_id_for_run(run, fallback="default")
    try:
        from app.live_ingest_credentials import resolve_live_systems

        live_systems = resolve_live_systems(org_id)
    except Exception:
        logger.exception("Live connector resolution failed; using requested mode/systems")
        live_systems = []

    if live_systems:
        mode = "live"
        systems = live_systems
        _emit_event(
            run_id,
            "CONNECT",
            f"Using authenticated connectors: {', '.join(live_systems)}",
        )
    else:
        _emit_event(run_id, "CONNECT", f"Connected sources: {', '.join(systems)}")

    # Default a GitHub-connected run to the github_engineering pack unless the run
    # explicitly selected a pack. Stored on the in-memory run so every downstream
    # _pack_id_for_run(run) (runner, enrichment, temporal) resolves to it.
    _explicit_pack = _pack_id_for_run(run)
    _effective_pack = resolve_effective_pack(_explicit_pack, live_systems)
    if _effective_pack and _effective_pack != _explicit_pack:
        run["packId"] = _effective_pack
        _emit_event(
            run_id, "CONNECT", f"GitHub connected — defaulting to the {_effective_pack} pack"
        )

    per_system: Dict[str, str] = {
        s: "skipped" for s in ["salesforce", "servicenow", "jira"]
    }
    errors: Dict[str, str] = {}

    try:
        # Single-ingest: the discovery runner is the SOLE place enterprise systems
        # are ingested. The old _probe_systems pre-pass ingested Salesforce/
        # ServiceNow/Jira a second time (throwing the data away) purely to build
        # the per-system status and the "any data?" gate — doubling the most
        # expensive live ingest. The runner now returns that summary in its
        # payload, and receives ALL connected systems (not a probe-filtered set).
        _emit_event(run_id, "INGEST", "Ingesting data from enterprise systems...")

        from discovery.runner import run as trackb_run
        from discovery.track_a_adapter import export_track_a_seed

        _emit_event(
            run_id, "EXTRACT", "Extracting entities and identifying patterns..."
        )
        pack_id = _pack_id_for_run(run)
        # R191-P1 T2: pass the full multi-pack selection when the run configured
        # one (T1). The runner runs each selected pack's detectors against the ONE
        # shared normalised signal with per-pack calibration; `pack` stays the
        # primary alias so a single-pack run is unchanged.
        pack_ids = _pack_ids_for_run(run)
        run_org_id = _org_id_for_run(run, "demo-org")
        payload = trackb_run(
            mode=mode,
            systems=systems,
            run_id=run_id,
            org_id=run_org_id,
            pack=pack_id,
            pack_ids=pack_ids,
        )

        # R18-C2 T2: preserve the exact pack execution snapshot returned by the
        # runner. The Run-Health dashboard reads these immutable run fields (and
        # the matching run.pack_executed event) instead of consulting today's
        # mutable pack registry for a historical run.
        executed_detectors = payload.get("detectorsExecuted")
        if isinstance(executed_detectors, list):
            run["packId"] = payload.get("packId") or pack_id
            run["packName"] = payload.get("packName") or run.get("packName")
            run["packVersion"] = payload.get("packVersion")
            run["executedDetectorIds"] = [
                str(detector_id)
                for detector_id in executed_detectors
                if str(detector_id).strip()
            ]
            run["packExecutedAt"] = payload.get("packExecutedAt")
            # R191-P1 T2: preserve the full multi-pack execution snapshot the
            # runner returns (each selected pack with its version stamp + the
            # per-pack execution metadata). Scalar fields above stay the PRIMARY
            # pack for backward compatibility; a single-pack run lists one entry.
            payload_pack_ids = payload.get("packIds")
            if isinstance(payload_pack_ids, list) and payload_pack_ids:
                run["packIds"] = [str(p) for p in payload_pack_ids if str(p).strip()]
                payload_pack_versions = payload.get("packVersions")
                if isinstance(payload_pack_versions, dict):
                    run["packVersions"] = payload_pack_versions
                payload_packs = payload.get("packs")
                if isinstance(payload_packs, list):
                    run["packs"] = payload_packs

        per_system, succeeded, ingest_errors = _ingest_summary_from_payload(
            payload, systems
        )
        errors.update(ingest_errors)

        if not succeeded:
            _emit_event(
                run_id,
                "ERROR",
                "No data could be ingested from any system",
                level="ERROR",
            )
            _finalise(
                run_id,
                run,
                "failed",
                mode,
                systems,
                per_system,
                {"opportunities": 0, "evidence": 0},
                errors,
                "DISCOVERY_FAILED",
            )
            return

        _emit_event(
            run_id, "NORMALIZE", f"Successfully ingested from: {', '.join(succeeded)}"
        )

        seed = export_track_a_seed(payload)

        opps = seed.get("opportunities", [])
        ev = seed.get("evidence", [])
        secops_opps, secops_ev = _secops_seed_slice(opps, ev)
        selected_pack_ids = payload.get("packIds") or (
            [payload.get("packId")] if payload.get("packId") else []
        )
        secops_enabled = SECURITY_OPS_PACK_ID in selected_pack_ids
        _assert_secops_materialized(
            {"opportunities": secops_opps, "evidence": secops_ev},
            where="Track-A Security Operations seed",
            enabled=secops_enabled,
        )

        db.run_kv_set("opps", run_id, opps)
        db.run_kv_set("evidence", run_id, ev)
        if payload.get("secopsVolume") is not None:
            # 2.0-B1 T5 (AC5): sweep the SecOps volume artifact like every other
            # SecOps materialization output. It is designed to be aggregate-only
            # (counts keyed on vulnerability_class x ci_class x remediation_path,
            # never hosts), but before T5 this was the ONE SecOps KV write in this
            # block with no floor sweep — the guarantee rested on a docstring
            # rather than an enforced boundary. It is also viewer-readable via
            # GET /api/runs/{id}/secops/volume, so an enumeration reaching it
            # would be broadly exposed. Swept unconditionally (not gated on
            # secops_enabled): if this artifact exists at all the run produced
            # SecOps volume data, whatever the pack list says.
            _assert_secops_materialized(
                payload["secopsVolume"],
                where="Security Operations volume artifact",
                enabled=True,
            )
            db.run_kv_set("secops_volume", run_id, payload["secopsVolume"])

        # R16-B1 (T6): persist the queryable evidence-pointer trail so a finding
        # can later be walked back to the source artifacts that produced it
        # (source_system + source_artifact + source_timestamp). Additive and
        # non-blocking — never aborts materialization.
        try:
            from .evidence_pointers import store_evidence_pointers

            store_evidence_pointers(
                run_id, opps, evidence=ev,
                run_completed_at=payload.get("completedAt"),
            )
        except Exception as e:  # noqa: BLE001 — provenance storage is additive.
            errors["evidence_pointers"] = str(e)

        # Keep Integration Hub connector cards in sync with the actual run data.
        from .connector_metrics import update_connector_metrics_from_run

        update_connector_metrics_from_run(payload, succeeded, run_org_id)

        _emit_event(
            run_id,
            "SCORE",
            f"Found {len(opps)} opportunities and {len(ev)} evidence items",
        )

        # T3 — compute and store cross-system linked clusters
        try:
            _emit_event(run_id, "ANALYZE", "Clustering cross-system references...")
            from .materialize_t3_hook import compute_and_store_clusters

            compute_and_store_clusters(run_id, ev)
        except Exception as e:
            errors["clusters"] = str(e)

        # roadmap
        try:
            _emit_event(run_id, "PLAN", "Generating implementation roadmap...")
            from .roadmap_engine import build_roadmap

            roadmap = build_roadmap(opps)
            if secops_enabled:
                secops_roadmap = (
                    roadmap
                    if len(secops_opps) == len(opps)
                    else build_roadmap(secops_opps)
                )
                _assert_secops_materialized(
                    secops_roadmap,
                    where="Security Operations roadmap",
                    enabled=True,
                )
            db.run_kv_set("roadmap", run_id, roadmap)
        except SecOpsAggregationFloorViolation:
            raise
        except Exception as e:
            errors["roadmap"] = str(e)

        # executive report
        try:
            _emit_event(run_id, "REPORT", "Building executive summary report...")
            from .executive_report_engine import build_executive_report

            roadmap = db.run_kv_get("roadmap", run_id, {})
            selected_system_ids = _selected_system_ids_for_report(
                run_id, run, run_inputs, systems
            )
            er = build_executive_report(
                run_id=run_id,
                opps=opps,
                roadmap=roadmap,
                selected_system_ids=selected_system_ids,
            )

            sa = er.get("sourcesAnalyzed", {})
            sa["totalConnected"] = len(selected_system_ids)
            sa["uploadedFiles"] = len(run_inputs.get("uploadedFiles", []))
            sa["sampleWorkspaceEnabled"] = bool(
                run_inputs.get("sampleWorkspaceEnabled", False)
            )
            er["sourcesAnalyzed"] = sa

            if secops_enabled:
                if len(secops_opps) == len(opps):
                    secops_report = er
                else:
                    secops_roadmap = build_roadmap(secops_opps)
                    secops_report = build_executive_report(
                        run_id=run_id,
                        opps=secops_opps,
                        roadmap=secops_roadmap,
                        selected_system_ids=selected_system_ids,
                    )
                _assert_secops_materialized(
                    secops_report,
                    where="Security Operations executive report",
                    enabled=True,
                )

            db.run_kv_set("executive_report", run_id, er)
        except SecOpsAggregationFloorViolation:
            raise
        except Exception as e:
            errors["exec_report"] = str(e)
            db.run_kv_set(
                "executive_report",
                run_id,
                {
                    "confidence": "Moderate",
                    "sourcesAnalyzed": {
                        "recommendedConnected": 0,
                        "totalConnected": len(
                            _selected_system_ids_for_report(
                                run_id, run, run_inputs, systems
                            )
                        ),
                        "uploadedFiles": len(run_inputs.get("uploadedFiles", [])),
                        "sampleWorkspaceEnabled": bool(
                            run_inputs.get("sampleWorkspaceEnabled", False)
                        ),
                    },
                    "topQuickWins": [],
                    "snapshotBubbles": [],
                    "roadmapHighlights": [],
                },
            )

        # T6 — LLM enrichment (post-processing, non-blocking)
        try:
            _emit_event(
                run_id, "AI_ANALYZE", "Starting AI-driven analysis and enrichment..."
            )
            from .llm_enrichment import KV_LLM_ENRICHMENT, run_llm_enrichment

            exec_report = db.run_kv_get("executive_report", run_id, {})
            sources_analyzed = exec_report.get("sourcesAnalyzed", {})
            from .pack_aware_enrichment import run_pack_aware_enrichment

            execution_pack_ids = list(payload.get("packIds") or pack_ids or [])
            if not execution_pack_ids and pack_id:
                execution_pack_ids = [pack_id]
            enrichment = run_pack_aware_enrichment(
                run_id=run_id,
                opportunities=opps,
                evidence=ev,
                sources_analyzed=sources_analyzed,
                pack_ids=execution_pack_ids,
                org_id=run_org_id,
                enrichment_fn=run_llm_enrichment,
            )
            if enrichment.get("ai_mode_label"):
                _emit_event(run_id, "AI_ANALYZE", enrichment["ai_mode_label"])
            secops_enrichment = [
                result
                for result in (enrichment.get("packResults") or [])
                if isinstance(result, dict)
                and result.get("packId") == SECURITY_OPS_PACK_ID
            ]
            _assert_secops_materialized(
                secops_enrichment,
                where="Security Operations enrichment",
                enabled=secops_enabled,
            )
            db.run_kv_set(KV_LLM_ENRICHMENT, run_id, enrichment)
            if enrichment.get("executiveSummary"):
                exec_report["aiExecutiveSummary"] = enrichment["executiveSummary"]
                db.run_kv_set("executive_report", run_id, exec_report)
            _emit_event(run_id, "COMPLETE", "AI analysis and enrichment completed")
        except SecOpsAggregationFloorViolation:
            raise
        except Exception as e:
            errors["llm_enrichment"] = str(e)
            _emit_event(run_id, "AI_ERROR", f"AI analysis failed: {e}", level="WARNING")

        # T7 — temporal enrichment (non-blocking, AT-143)
        try:
            from .llm_enrichment import KV_LLM_ENRICHMENT as _KV_LLM
            from .jobs.baseline_calculator import calculate_baselines_for_org
            from .telemetry import record_event
            from .temporal_enrichment import enrich_opportunities_with_temporal_context

            # Read baselines under the SAME org_id the snapshots were written
            # with (the value passed to trackb_run above). Resolving the org
            # again with a different fallback would let a run with no explicit
            # orgId write under "demo-org" but read under "default", so it
            # would never find its own history.
            _org_id = run_org_id
            # AT-158: baselines are computed per-org; pass the run's org explicitly
            # (the old no-arg calculate_baselines() was removed in that refactor).
            calculate_baselines_for_org(_org_id)
            _fallback_pack_id = _pack_id_for_run(run) or ""
            _opps_by_pack: Dict[str, List[Dict[str, Any]]] = {}
            for _opp in opps:
                _opp_pack_id = str(
                    _opp.get("packId") or _opp.get("pack_id") or _fallback_pack_id
                )
                _opps_by_pack.setdefault(_opp_pack_id, []).append(_opp)
            if len(_opps_by_pack) == 1:
                _current_pack_id, _pack_opps = next(iter(_opps_by_pack.items()))
                # Preserve the original single-pack assignment contract while
                # multi-pack runs below remain isolated by their own pack id.
                opps = enrich_opportunities_with_temporal_context(
                    run_id, _org_id, _current_pack_id, _pack_opps
                )
            else:
                for _current_pack_id, _pack_opps in _opps_by_pack.items():
                    enrich_opportunities_with_temporal_context(
                        run_id, _org_id, _current_pack_id, _pack_opps
                    )

            _temporal_keys = (
                "baseline_context", "trend_direction", "anomaly_score",
                "is_anomalous", "first_deviation", "baseline_mean", "run_count",
                "baseline_stddev", "baseline_window_days", "current_value",
                "recent_values", "signal_key", "pack_id",
            )
            _stored = db.run_kv_get(_KV_LLM, run_id, {})
            _per_opp = _stored.get("perOpportunity", {})
            for _opp in opps:
                _oid = _opp.get("id", "")
                if _oid in _per_opp:
                    for _k in _temporal_keys:
                        if _k in _opp:
                            _per_opp[_oid][_k] = _opp[_k]
            _stored["perOpportunity"] = _per_opp
            db.run_kv_set(_KV_LLM, run_id, _stored)
            record_event(
                "temporal.enrichment_completed",
                {"run_id": run_id, "org_id": _org_id},
            )
        except Exception as e:
            logger.warning("T7 temporal enrichment failed (non-blocking): %s", e)

        status = "complete" if len(succeeded) == len(systems) else "partial"
        audit_action = (
            "DISCOVERY_MATERIALIZED" if status == "complete" else "DISCOVERY_PARTIAL"
        )
        _finalise(
            run_id,
            run,
            status,
            mode,
            systems,
            per_system,
            {"opportunities": len(opps), "evidence": len(ev)},
            errors,
            audit_action,
        )
        _emit_event(run_id, "DONE", "Discovery run complete (100%)")

    except Exception as e:
        errors["exception"] = str(e)
        _finalise(
            run_id,
            run,
            "failed",
            mode,
            systems,
            per_system,
            {"opportunities": 0, "evidence": 0},
            errors,
            "DISCOVERY_FAILED",
        )
