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
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv

from app.telemetry import record_event

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


def run(
    mode: Optional[str] = None,
    run_id: Optional[str] = None,
    org_id: str = "demo-org",
    systems: Optional[List[str]] = None,
    pack: Optional[str] = None,
) -> Dict[str, Any]:
    # ENG-SHARED-1: resolve pack config — replaces temporary is_ncino_pack conditional
    from .packs.pack_config import (
        get_pack,
        get_pack_domain,
        is_ncino_pack,
        is_sqlserver_opsignal_pack,
        is_github_engineering_pack,
        is_enterprise_ops_pack,
    )
    pack_config = get_pack(pack)
    pack_id     = pack_config["packId"]
    pack_domain = pack_config["pack_domain"]

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

    record_event("run.started", {
        "org_id": org_id,
        "run_id": run_id,
        "source": "run_pipeline",
    })

    # 1. Ingest
    from .ingest import salesforce, servicenow, jira as jira_mod
    from .ingest.salesforce import IngestError as SFError
    from .ingest.servicenow import ServiceNowIngestError as SNError
    from .ingest.jira import JiraIngestError

    sf_data, sn_data, jira_data = {}, {}, {}
    github_data: Dict[str, Any] = {}
    logger.info(f"Systems: {sorted(list(_systems))}")

    try:
        if "salesforce" in _systems:
            sf_data = salesforce.ingest()
            logger.info("Salesforce ingestion: OK")
    except SFError as e:
        logger.error(f"Salesforce ingestion FAILED: {e}")

    try:
        if "servicenow" in _systems:
            sn_data = servicenow.ingest()
            if sn_data: logger.info("ServiceNow ingestion: OK")
    except SNError as e:
        logger.error(f"ServiceNow ingestion FAILED: {e}")

    try:
        if "jira" in _systems:
            jira_data = jira_mod.ingest()
            if jira_data: logger.info("Jira ingestion: OK")
    except JiraIngestError as e:
        logger.error(f"Jira ingestion FAILED: {e}")

    if not sf_data and "salesforce" in _systems:
        logger.error("Salesforce data unavailable — cannot run detectors. Aborting.")
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
        })
        return _empty_run(run_id, org_id, mode, started_at)

    # 2a. nCino ingest — if ncino pack, fetch lending signals from nCino objects
    from .packs.pack_config import is_ncino_pack as _is_ncino
    if _is_ncino(pack_id) and "salesforce" in _systems:
        try:
            from .ingest.ncino import ingest as ncino_ingest
            # CS-4 / AT-310: ProcessInstance is queried by both salesforce.ingest()
            # and ncino.ingest(). Forward the Salesforce CRM approval data so the
            # nCino ingestor reuses it instead of issuing a duplicate
            # ProcessInstance query, reducing total Salesforce API calls by 1 per
            # discovery run. Default to an empty list when approval_processes is
            # absent so ncino.ingest() still skips the duplicate fetch.
            ncino_data = ncino_ingest(
                preloaded_process_instances=sf_data.get("approval_processes", [])
            )
            # Merge ncino data into sf_data so detectors can find it
            if sf_data is None:
                sf_data = {}
            sf_data["ncino"] = ncino_data
            logger.info("nCino ingestion: OK — %d lending metrics", len(ncino_data))
        except Exception as e:
            logger.warning("nCino ingestion failed (non-blocking): %s", e)

    # 2b. STRS Benefits ingest — if strs_benefits pack
    from .packs.pack_config import is_strs_benefits_pack as _is_strs
    if _is_strs(pack_id) and "salesforce" in _systems:
        try:
            from .ingest.strs_benefits import ingest as strs_ingest
            strs_data = strs_ingest()
            if sf_data is None:
                sf_data = {}
            sf_data["strs_benefits"] = strs_data
            logger.info("STRS Benefits ingestion: OK — %d benefit metrics", len(strs_data))
        except Exception as e:
            logger.warning("STRS Benefits ingestion failed (non-blocking): %s", e)

    # 2c. GitHub Engineering ingest — if github_engineering pack (T1-S12).
    # GitHub is the first engineering signal source and does not depend on
    # Salesforce. Jira is still ingested above when in _systems so the pack's
    # confidence-elevation corroboration can run.
    if is_github_engineering_pack(pack_id):
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
    if is_enterprise_ops_pack(pack_id) and str(mode).strip().lower() != "live":
        sn_data, jira_data = _attach_enterprise_ops_demo(sn_data, jira_data)

    # 2d. Oracle DB ingest  — T2-S12-A: sqlserver_opsignal pack + oracle_db connector.
    # 2e. PostgreSQL ingest — T2-S12-A: sqlserver_opsignal pack + postgresql connector.
    from .packs.pack_config import is_sqlserver_opsignal_pack as _is_db_opsignal
    db_data: Dict[str, Any] = {}
    _db_connector_id: Optional[str] = None

    if _is_db_opsignal(pack_id):
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

    # 2. Context
    org_ctx = build_org_context(sf_data, sn_data, jira_data)

    # 3. Detect — ENG-AIQ-NC-4: pack-driven detector selection
    # Replaces hardcoded Service Cloud detector list.
    # pack_config.py (ENG-SHARED-1) defines which detectors each pack activates.
    from .packs.pack_config import is_ncino_pack

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

    detector_results, all_evaluated = _run_detector_phase(
        all_detectors,
        primary_data,
        sn_data,
        jira_data,
    )

    _snapshot_detector_evaluations(
        org_id=org_id,
        run_id=run_id,
        pack_id=pack_id,
        detector_results=detector_results,
        all_evaluated=all_evaluated,
    )

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

    # 4. Score + Evidence
    # ENG-AIQ-NC-4: use lending_scorer for ncino pack, SC scorer for service_cloud
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
    from .evidence_builder import build_evidence
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
    id_counter = itertools.count(1)
    def id_factory() -> str: return f"{run_id[-6:]}_{next(id_counter):04d}"

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

    # ── ENT-2: build the shared corroboration run_data once for this run ──
    # Maps already-extracted Jira/ServiceNow correlation by detector and carries
    # Slack/Confluence corroboration blocks through when an upstream connector
    # payload provides them. connected_systems drives COR-08 (single-source no
    # elevation). This only ever ELEVATES confidence downstream — it never
    # downgrades.
    _run_ts_iso = _run_started_dt.isoformat()
    _corr_run_data: Dict[str, Any] = {"connected_systems": sorted(_systems)}
    if _corroboration_available:
        try:
            _corr_run_data = build_corroboration_run_data(
                systems=_systems,
                sn_by_detector=sn_by_detector,
                jira_by_detector=jira_by_detector,
                run_timestamp_iso=_run_ts_iso,
                source_payloads=[sf_data, sn_data, jira_data, github_data, db_data],
            )
        except Exception as _corr_data_err:  # noqa: BLE001 — non-blocking.
            logger.warning("ENT-2 corroboration run_data build failed (non-blocking): %s", _corr_data_err)

    opportunities = []
    for dr in detector_results:
        # Select scorer based on pack
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
        else:
            scored = sc_score(dr)

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
                })
                corroboration_count += 1
            if corroboration_count > 0:
                logger.info("  %s: +%d corroborating evidence items (Jira/SN)",
                            dr.detector_id, corroboration_count)
        opp = {
            "runId": run_id, "orgId": org_id, "detector_id": dr.detector_id,
            "packId": pack_id,
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
        }
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

        opportunities.append(opp)

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
        "pack_id": pack_id,
        "system_count": len(_systems),
    })

    return {
        "runId": run_id, "orgId": org_id, "mode": mode,
        "packId": pack_id,
        "startedAt": started_at, "completedAt": datetime.now(timezone.utc).isoformat(),
        "inputs": org_ctx, "opportunities": opportunities,
    }

def _empty_run(run_id: str, org_id: str, mode: str, started_at: str) -> Dict:
    return {"runId": run_id, "orgId": org_id, "mode": mode, "startedAt": started_at,
            "completedAt": datetime.now(timezone.utc).isoformat(), "inputs": {}, "opportunities": []}

def main():
    parser = argparse.ArgumentParser(description="AgentIQ discovery runner")

    # Dynamically read INGEST_MODE from environment, fallback to "offline"
    default_mode = os.environ.get("INGEST_MODE", "offline").strip().lower()
    if default_mode not in ("offline", "live"):
        default_mode = "offline"

    parser.add_argument("--mode", choices=["offline", "live"], default=default_mode)
    parser.add_argument("--systems", help="Comma-separated list of systems (e.g. salesforce,jira)")
    parser.add_argument("--pack", default=None, help="Pack ID: service_cloud (default) or ncino")
    parser.add_argument("--output", help="Output JSON file path")
    parser.add_argument("--run-id", help="Explicit run ID")
    parser.add_argument("--org-id", default="demo-org")
    parser.add_argument("--output-format", choices=["internal", "track_a_seed"], default="internal")

    args = parser.parse_args()

    # Parse systems string into a list if provided
    systems_list = None
    if args.systems:
        systems_list =[s.strip().lower() for s in args.systems.split(",") if s.strip()]

    payload = run(
        mode=args.mode,
        run_id=args.run_id,
        org_id=args.org_id,
        systems=systems_list,
        pack=args.pack,
    )

    if args.output_format == "track_a_seed":
        payload = export_track_a_seed(payload)

    out = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(out, encoding="utf-8")
        logger.info(f"Output written to {args.output}")
    else:
        print(out)

if __name__ == "__main__":
    main()
 
