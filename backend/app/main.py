from contextlib import asynccontextmanager
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

logger = logging.getLogger(__name__)

from . import db
from .db import (
    get_all,
    get_one,
    kv_get,
    kv_set,
    run_get,
    upsert,
    tenancy_get_all,
    tenancy_get_one,
    tenancy_get_runs,
    org_connectors_list,
    org_connector_get,
    org_connector_set,
)
from . import license_limits
from . import billing_ledger
from . import connector_roadmap
from .normalization_enrichment import KV_NORMALIZATION, enrich_ambiguous_mappings
from .opportunity_display import (
    with_display,
    with_display_title,
    with_display_titles,
    with_exec_report_display_titles,
    with_roadmap_display_titles,
)
from .replay import replay_run as replay_run_
from .roadmap_engine import apply_learned_adjustment, build_roadmap
from .terminology import apply_run_terminology
from .routes_normalization import register_normalization_routes
from .routes_connector_auth import register_connector_auth_routes
from .routes_cloud_connectors import register_cloud_connector_routes
from .routes_stack_builder import register_stack_builder_routes
from .routes_stack_builder_launch import register_stack_builder_launch_routes
from .routes_salesforce_products import register_salesforce_products_routes
from .routes_slack_channels import register_slack_channels_routes
from .routes_teams_channels import register_teams_channels_routes
from .routes_jira_projects import register_jira_projects_routes
from .routes_confluence_spaces import register_confluence_spaces_routes
from .routes_sharepoint_sites import register_sharepoint_sites_routes
from .routes_github_repos import register_github_repos_routes
from .routes_sprint4_t1 import register_sprint4_t1_routes
from .routes_sprint4_t2 import register_sprint4_t2_routes
from .routes_sprint4_t3 import register_sprint4_t3_routes
from .routes_sprint4_t4 import register_sprint4_t4_routes
from .routes_sprint4_t6 import register_sprint4_t6_routes
from .routes_sprint41_blueprint import register_blueprint_routes
from .run_store import read_run, read_run_events, start_run_
from .routes_workspace_catalog import register_workspace_catalog_routes
from .routes_workspace import register_workspace_routes
from .routes_db_connectors import register_db_connector_routes
from .routes_temporal import register_temporal_routes
from .routes_entities import register_entities_routes
from .routes_causal import register_causal_routes
from .routes_graph import register_graph_routes
from .routes_retrieval import register_retrieval_routes
from .routes_cloud_ops_signatures import register_cloud_ops_signature_routes
from .routes_run_health import register_run_health_routes
from .routes_auth import register_auth_routes
from .routes_audit_export import register_audit_export_routes
from .routes_license import register_license_routes
from .routes_usage_report import register_usage_report_routes
from .routes_usage_summary import register_usage_summary_routes
from .routes_ingestion import register_ingestion_routes
from .routes_runbook_matches import register_runbook_match_routes
from .routes_secops_evidence import register_secops_evidence_routes
from .routes_opportunity_lifecycle import register_opportunity_lifecycle_routes
from .routes_opportunity_baseline import register_opportunity_baseline_routes
from .routes_opportunity_movement import register_opportunity_movement_routes
from .routes_learning_feedback import register_learning_routes
from .routes_learning_adjustment import register_learning_adjustment_routes
from .routes_outcomes import register_outcome_routes
from .security import require_auth
from .auth.configs import CONNECTOR_AUTH_CONFIGS
from .auth.secrets import validate_all_secrets
from .middleware.audit import (
    CONNECTOR_CONFIGURED,
    CONNECTOR_CONNECTED,
    CONNECTOR_DISCONNECTED,
    EVIDENCE_DECISION_RECORDED,
    OPPORTUNITY_DECISION_RECORDED,
    OUTCOME_SUCCESS,
    log_event as audit_log_event,
)
from .middleware.tenancy import get_current_org_id, register_tenancy
from .middleware.license_gate import register_license_gate
from .retrieval.default_resolvers import register_default_content_resolvers
from .rbac import _get_user_id_from_token, require_role, seed_owner

_DEV_USER = os.getenv("DEV_JWT", "dev-token-change-me")
_DEV_ORG = "default"


def _verify_db_driver_imports() -> None:
    """Log DB driver availability early so operators see issues at startup."""
    for module_name, connector_name in (
        ("oracledb", "Oracle DB"),
        ("psycopg2", "PostgreSQL"),
    ):
        try:
            __import__(module_name)
            logger.info("%s DB driver import check passed (%s)", connector_name, module_name)
        except Exception as exc:
            logger.warning(
                "%s DB driver import check failed (%s): %s. "
                "The connector will degrade on first use until the driver is installed.",
                connector_name,
                module_name,
                exc,
            )


def _is_production() -> bool:
    """True when this process should be treated as production.

    Delegates to :func:`app.deployment_profile.is_production`, the single
    source of truth for this check (R1.9.1-H1 T4) — kept as a thin wrapper
    here so existing call sites in this module are unaffected.
    """
    from .deployment_profile import is_production

    return is_production()


def _validate_org_approval_config() -> None:
    """Validate the AUTH-2 org-approval env config at startup (review #3/#4/#8).

    AGENTIQ_BACKEND_URL backs the approve/reject links in the admin email and
    AGENTIQ_ADMIN_EMAIL is where that email is sent. If either is unset the code
    falls back to a localhost URL / a hardcoded address, which silently breaks
    org approval in a real deployment (links unreachable, emails misdirected).
    Surface it loudly: warn everywhere, and hard-fail in production so a
    misconfigured deploy cannot start with broken approvals.
    """
    problems: list[str] = []

    backend_url = os.getenv("AGENTIQ_BACKEND_URL", "").strip()
    if not backend_url:
        problems.append(
            "AGENTIQ_BACKEND_URL is not set — org approval links will point to "
            "localhost and be unreachable for the admin."
        )
    elif "localhost" in backend_url or "127.0.0.1" in backend_url:
        problems.append(
            f"AGENTIQ_BACKEND_URL points to a local address ({backend_url}) — "
            "org approval links will be unreachable for the admin."
        )

    admin_email = os.getenv("AGENTIQ_ADMIN_EMAIL", "").strip()
    if not admin_email:
        problems.append(
            "AGENTIQ_ADMIN_EMAIL is not set — org approval request emails will go "
            "to the hardcoded default address and may be misdirected."
        )

    if not problems:
        return

    production = _is_production()
    for problem in problems:
        if production:
            logger.error("Org approval config: %s", problem)
        else:
            logger.warning("Org approval config: %s", problem)
    if production:
        raise RuntimeError(
            "Org registration approval is misconfigured for production: "
            + " ".join(problems)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Only enforce secret presence when REQUIRE_CONNECTOR_SECRETS=1 (production).
    # Dev and test environments run without connector secrets set.
    if os.getenv("REQUIRE_CONNECTOR_SECRETS") == "1":
        validate_all_secrets(CONNECTOR_AUTH_CONFIGS)
        # R1.9.1-H1 T4 (follow-up): the production DEPLOYMENT PROFILE is selected
        # ONLY by ENVIRONMENT=production (see app.deployment_profile.is_production).
        # REQUIRE_CONNECTOR_SECRETS=1 enforces secret presence but does NOT make a
        # process production — so a deployment that sets only the latter still runs
        # under the dev profile, which leaves the customer-tenant CUSTOMER_TENANT_API_KEY
        # env fallback ENABLED (vault revocation could be bypassed). Warn loudly so an
        # operator who set REQUIRE_CONNECTOR_SECRETS=1 as a production signal also sets
        # ENVIRONMENT=production, which is now mandatory for the production profile.
        if not _is_production():
            logger.warning(
                "REQUIRE_CONNECTOR_SECRETS=1 is set but ENVIRONMENT is not "
                "'production'. The production deployment profile is selected ONLY by "
                "ENVIRONMENT=production; without it this process runs under the dev "
                "profile and the CUSTOMER_TENANT_API_KEY environment fallback stays "
                "enabled. Set ENVIRONMENT=production on production deployments."
            )
    # AUTH-2 review #3/#4/#8: fail fast (prod) / warn (dev) if the org-approval
    # email config is missing, so broken approval links never ship silently.
    _validate_org_approval_config()
    # Seed the dev user as owner of the default org so existing routes pass RBAC.
    seed_owner(_DEV_ORG, _DEV_USER)
    # Seed the configured static role tokens (VIEWER_JWT / ANALYST_JWT /
    # ADMIN_JWT) as members of the default org so they resolve their role on
    # role-gated routes. DEV_JWT is skipped (already seeded as owner above), and
    # seeding is default-org-only so org-scoping still denies them elsewhere.
    # Gated to dev/test only: production (REQUIRE_CONNECTOR_SECRETS=1) never seeds
    # static role tokens, so they cannot resolve a role on any gated route there.
    if os.getenv("REQUIRE_CONNECTOR_SECRETS") != "1":
        from .rbac import seed_static_token_members
        seed_static_token_members(_DEV_ORG)
    # R16-D1 T2: validate model provider config at startup so an unknown
    # MODEL_GENERATION_PROVIDER or MODEL_EMBEDDING_PROVIDER value raises a
    # clear ValueError before the first model call (T2-AC4).
    from .model_gateway import validate_provider_config
    validate_provider_config()
    # T2-S12-A: surface Oracle/PostgreSQL driver install issues at startup.
    _verify_db_driver_imports()
    # Ensure the temporal signal_snapshots table exists. A fresh dev.db is built
    # by seed_loader.py, which does not run alembic migrations, so without this
    # the baseline/temporal enrichment silently fails and the Baseline Context
    # panel never renders. Idempotent (IF NOT EXISTS) — no-op once migrated.
    from .temporal import ensure_signal_snapshots_table
    ensure_signal_snapshots_table()
    # AUTH-1 / AT-233: ensure orgs/users/login_attempts exist for the auth layer.
    # seed_loader.py does not run alembic, so create them idempotently here too
    # (CREATE TABLE IF NOT EXISTS — no-op once migrations 0004/0005 have run).
    from .auth.user_auth import ensure_auth_tables
    ensure_auth_tables()
    # LIC-1 / T4 (AT-345): validate the installed license once at startup.
    # One-shot and idempotent; never raises (startup must not fail on it) and
    # runs regardless of AGENTIQ_DISABLE_BACKGROUND_JOBS — only the periodic
    # re-check below is a gated background job.
    from .license_runtime import run_startup_validation
    run_startup_validation()
    # ENT-1: register customer entity-extraction overlays before the first run.
    # No-op by default (no customer overlays hardcoded into the core); the
    # function is the documented hook for deployment-time registration. Never
    # blocks startup.
    try:
        from .entity_overlays.overlay_registry import register_startup_overlays
        register_startup_overlays()
    except Exception as exc:  # pragma: no cover — startup must not fail on this
        import logging
        logging.getLogger(__name__).warning(
            "entity overlay startup registration skipped: %s", exc
        )
    # AT-90: start connector health check background job.
    from .jobs.connector_health import start_health_check_job, stop_health_check_job
    from .jobs.baseline_calculator import (
        scheduler as baseline_scheduler,
        start_scheduler as start_baseline_scheduler,
    )
    # Proactive OAuth token refresher: renews vault tokens before they expire so
    # connected sources stay live without the user re-running the OAuth flow.
    from .jobs.token_refresher import (
        scheduler as token_refresh_scheduler,
        start_scheduler as start_token_refresh_scheduler,
    )
    # R18-B1 T3: async retrieval embedding worker. Content is indexed before it is
    # embedded; this job drains the pending backlog off the run path so embedding
    # lag never blocks a discovery run (AC7).
    from .jobs.embedding_worker import (
        scheduler as embedding_worker_scheduler,
        start_scheduler as start_embedding_worker_scheduler,
    )
    # R18-B2 T3: async retrieval refresh worker. When a source artifact changes its
    # chunks are marked stale and queued; this job drains the queue off the run path
    # — re-extract, hash-compare, re-embed only what changed, atomic swap, clear
    # stale — so re-embedding never blocks a discovery run.
    from .jobs.refresh_worker import (
        scheduler as refresh_worker_scheduler,
        start_scheduler as start_refresh_worker_scheduler,
    )
    # LIC-1 / T4 (AT-345): periodic license re-check (gated background job).
    from .license_runtime import start_license_scheduler, stop_license_scheduler

    background_jobs_disabled = os.getenv("AGENTIQ_DISABLE_BACKGROUND_JOBS") == "1"
    # R18-B2 T3: teach the refresh worker how to re-extract changed artifacts from
    # the production connectors before the worker starts draining its queue.
    register_default_content_resolvers()
    if not background_jobs_disabled:
        start_health_check_job()
        start_baseline_scheduler()
        start_token_refresh_scheduler()
        start_embedding_worker_scheduler()
        start_refresh_worker_scheduler()
        start_license_scheduler()
    yield
    # AT-90: shut down scheduler on SIGTERM / graceful shutdown (wait=False).
    if not background_jobs_disabled:
        stop_health_check_job()
        if baseline_scheduler.running:
            baseline_scheduler.shutdown(wait=False)
        if token_refresh_scheduler.running:
            token_refresh_scheduler.shutdown(wait=False)
        if embedding_worker_scheduler.running:
            embedding_worker_scheduler.shutdown(wait=False)
        if refresh_worker_scheduler.running:
            refresh_worker_scheduler.shutdown(wait=False)
        stop_license_scheduler()


app = FastAPI(title="AgentIQ Layer 1 API Skeleton", version="0.1.0", lifespan=lifespan)
# LIC-1 / T5 (AT-346): gate discovery-run endpoints when the license is
# read-only/invalid. Registered BEFORE tenancy so it runs AFTER TenancyMiddleware
# in the request path (Starlette runs middleware in reverse registration order):
# the per-request org context is established before the gate evaluates, and the
# gate's 402 carries a resolved org for the audit trail. The gate still resolves
# the org directly from the request (resolve_request_org_id) as a safety net.
# CORS is added last (below) so it stays outermost and blocked 402s keep their
# CORS headers; reads/login/valid+grace are untouched.
register_license_gate(app)
register_tenancy(app)

# Register routes in order
register_stack_builder_routes(app)
register_stack_builder_launch_routes(app)
register_workspace_catalog_routes(app)
register_salesforce_products_routes(app)
register_slack_channels_routes(app)
register_teams_channels_routes(app)
register_jira_projects_routes(app)
register_confluence_spaces_routes(app)
register_sharepoint_sites_routes(app)
register_github_repos_routes(app)
# Sprint 4 routes are registered in dependency order T1 → T2 → T3 → T4 → T6.
# FastAPI resolves routes in registration order, so a module registered earlier
# wins any shared path prefix. T6 (LLM enrichment / evidence-trace) MUST be
# registered AFTER T1–T4 — its own module contract states "register after T4" —
# so a future T1–T5 route can never be shadowed by T6's handler.
register_sprint4_t1_routes(app)
register_sprint4_t2_routes(app)
register_sprint4_t3_routes(app)
register_sprint4_t4_routes(app)
register_sprint4_t6_routes(app)
register_blueprint_routes(app)
register_normalization_routes(app)
if not any(r.path == "/api/connectors/oauth/callback" for r in app.routes):
    register_connector_auth_routes(app)
register_cloud_connector_routes(app)
register_db_connector_routes(app)
register_temporal_routes(app)
register_workspace_routes(app)
register_entities_routes(app)
register_causal_routes(app)
register_graph_routes(app)
# R18-B2 T6: retrieval freshness metrics for the run-health dashboard (AC7).
register_retrieval_routes(app)
# Read-only view of the cloud-event signature rows a run assembled — the values
# needed to author matching ServiceNow incidents. Additive; nothing else reads it.
register_cloud_ops_signature_routes(app)
# R18-C2 T1: Run-Health Dashboard aggregation endpoints (connectors, runs,
# content/freshness, packs) — org-scoped, Owner/Analyst read-only.
register_run_health_routes(app)
register_auth_routes(app)
# LIC-1 / T6 (AT-347): Owner-only license status + update-key admin routes.
register_license_routes(app)
# 2.0-D4 T2 — Owner-only signed audit export (period-scoped, org-scoped in SQL).
register_audit_export_routes(app)
# R-1.9.1-L2 / T3 (AT-695): Owner-only signed usage-report generator route.
register_usage_report_routes(app)
# R-1.9.1-L2 / T5 (AT-697): Owner-only pre-invoice usage-summary route (AC6).
register_usage_summary_routes(app)
# R16-A1 / AT-383 (T7): admin ingestion-checkpoint reset.
register_ingestion_routes(app)
# MSP-B5 T4: analyst accept/dismiss/defer lifecycle for proposed runbook matches.
register_runbook_match_routes(app)
# MSP-B12 T3: analyst-only, audited resolution of one SecOps evidence pointer.
register_secops_evidence_routes(app)
# 2.0-A2 T1: analyst-driven opportunity lifecycle (open -> actioned -> monitoring
# -> measured, plus dismissed/stalled), keyed on the stable opportunity_identity.
register_opportunity_lifecycle_routes(app)
# 2.0-A2 T2: read-only retrieval of the immutable baseline artifact frozen at
# finding creation. No write verb — the pipeline is the only writer.
register_opportunity_baseline_routes(app)
# 2.0-A2 T3: read-only post-action movement records (baseline vs current, with a
# comparability verdict and both run ids). Measured by the pipeline, never on read.
register_opportunity_movement_routes(app)
# 2.0-A2 T6: customer-facing outcome surfaces, assembled from stored lifecycle and
# movement artifacts with caveat counts and run-id evidence.
register_outcome_routes(app)
# 2.0-A3 T1: the learning signal set — analyst accept/dismiss/defer-with-reason
# plus A2 outcome results, outcome-weighted. Records and inspects signals only;
# the bounded ranking adjustment is T2 and is not registered here.
register_learning_routes(app)
# 2.0-A3 T2: the bounded adjustment layer's read surface + recomputation.
# The adjustment itself is applied at serve time via the one function in
# app/learning_adjustment.py; these routes inspect and recompute its state.
register_learning_adjustment_routes(app)

origins = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:5176",
    ).split(",")
    if o.strip()
]


def build_cors_kwargs(environment: str, allowed_origins: List[str]) -> Dict[str, Any]:
    """Build CORS middleware kwargs.

    The permissive ``http://localhost:<port>`` regex is convenient for local dev
    (it lets any localhost port through), but in production it would allow any
    localhost process to make authenticated cross-origin requests. We therefore
    only attach ``allow_origin_regex`` outside of production.
    """
    cors_kwargs: Dict[str, Any] = {
        "allow_origins": allowed_origins,
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }
    if (environment or "").strip().lower() != "production":
        cors_kwargs["allow_origin_regex"] = r"http://localhost:\d+"
    return cors_kwargs


_environment = os.getenv("ENVIRONMENT", "").strip().lower()
app.add_middleware(CORSMiddleware, **build_cors_kwargs(_environment, origins))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_kv_get(key: str, run_id: str, default: Any = None) -> Any:
    value = kv_get(f"{key}:{run_id}")
    return value if value is not None else default


def run_kv_set(key: str, run_id: str, value: Any) -> None:
    kv_set(f"{key}:{run_id}", value)


def default_audit() -> List[Dict[str, Any]]:
    return [
        {
            "id": "audit_seed_001",
            "tsLabel": "18 Mar 2026, 10:12",
            "tsEpoch": 1773828720,
            "action": "DISCOVERY_STARTED",
            "by": "System",
        },
        {
            "id": "audit_seed_002",
            "tsLabel": "18 Mar 2026, 10:15",
            "tsEpoch": 1773828900,
            "action": "ANALYSIS_COMPLETE",
            "by": "System",
        },
    ]


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "ts": now_iso()}


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    # AT-288 F1-AC6: report database connectivity. A live PostgreSQL connection
    # (SELECT 1) is required for status 'healthy'. The legacy {ok, ts} fields are
    # retained for backward compatibility with existing consumers.
    db_status = "ok"
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            con.close()
    except Exception as exc:
        logger.warning("health check: database connectivity failed: %s", exc)
        db_status = "error"
    healthy = db_status == "ok"
    return {
        "ok": healthy,
        "ts": now_iso(),
        "status": "healthy" if healthy else "unhealthy",
        "checks": {"database": {"status": db_status}},
    }


@app.get("/")
def root() -> Dict[str, Any]:
    return {"ok": True, "service": "AgentIQ API", "health": "/api/health"}


@app.get("/api/connectors", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def list_connectors() -> List[Dict[str, Any]]:
    # Per-org: shared catalog overlaid with THIS org's connection state, so a
    # fresh org sees disconnected connectors regardless of what other orgs did.
    return org_connectors_list(get_current_org_id())


@app.post(
    "/api/connectors/{connector_id}/connect",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def connect_connector(
    connector_id: str,
    body: Dict[str, Any],
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    org_id = get_current_org_id()
    status = body.get("status", "connected")
    # R191-R1 T5 (AT-726 / AC2): a roadmap connector (SAP/D365 and any tile whose
    # ingestion does not ship yet) is NOT connectable — refuse a connect attempt
    # with an actionable, named reason so it can never become a connected system
    # a run could select. This guard runs BEFORE the catalog lookup below so a
    # roadmap tile is refused with 409 regardless of whether it is pre-seeded in
    # the org's connector catalog — a roadmap tile absent from the catalog must
    # not fall through to the 404 and silently bypass the block. Applies only to
    # the connect transition; a disconnect on an unconnected roadmap tile is a
    # harmless no-op.
    if status == license_limits.CONNECTED_STATUS and connector_roadmap.is_roadmap(
        connector_id
    ):
        raise HTTPException(
            409,
            connector_roadmap.roadmap_block_message(connector_id),
        )
    c = org_connector_get(org_id, connector_id)
    if not c:
        raise HTTPException(404, "connector not found")
    # R17-D4 Addendum A / T9: enforce limits.max_systems at connection time.
    # Only a transition TO "connected" adds a system; forward-only, so reconnects
    # of an already-connected connector and any non-connect status change (e.g.
    # disconnect) are never blocked. Raises HTTP 402 when at the licensed limit.
    if status == license_limits.CONNECTED_STATUS:
        license_limits.enforce_can_connect(org_id, connector_id)
    # R-1.9.1-L2 / T2 (AC2): capture the connection state BEFORE the write so the
    # billing ledger records only a genuine transition on this bidirectional toggle.
    was_connected = c.get("status") == license_limits.CONNECTED_STATUS
    c["status"] = status
    c["lastSynced"] = c.get("lastSynced", "—")
    # Write to THIS org's namespaced row — never the shared catalog.
    org_connector_set(org_id, connector_id, c)
    billing_ledger.record_connection_change(
        org_id,
        connector_id,
        was_connected=was_connected,
        now_connected=status == license_limits.CONNECTED_STATUS,
    )
    # 2.0-D4 T1 (AC1): D4 names connector create/edit/delete. Delete was audited
    # and the OAuth CALLBACK emitted connector_connected, but this direct status
    # toggle — the path a non-OAuth connector actually takes — recorded nothing.
    # The event follows the resulting state rather than the route name, so a
    # toggle to a non-connected status reads as a disconnect and matches what the
    # OAuth revoke path emits for the same outcome.
    # Two explicit calls rather than one with a conditional event type: the
    # conformance sweep resolves each log_event call site's type statically, and
    # a type chosen by an expression is exactly the case it cannot verify.
    if status == license_limits.CONNECTED_STATUS:
        audit_log_event(
            CONNECTOR_CONNECTED,
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
            target=connector_id,
            status=status,
            was_connected=was_connected,
            outcome=OUTCOME_SUCCESS,
        )
    else:
        audit_log_event(
            CONNECTOR_DISCONNECTED,
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
            target=connector_id,
            status=status,
            was_connected=was_connected,
            outcome=OUTCOME_SUCCESS,
        )
    return c


@app.post(
    "/api/connectors/{connector_id}/configure",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def configure_connector(
    connector_id: str,
    body: Optional[Dict[str, Any]] = None,
    token: str = Depends(require_auth),
) -> Dict[str, Any]:
    org_id = get_current_org_id()
    c = org_connector_get(org_id, connector_id)
    if not c:
        raise HTTPException(404, "connector not found")
    if c.get("status") != "connected":
        raise HTTPException(400, "connector must be connected before configuring")
    if connector_id == "servicenow" and body is not None:
        if "cmdb_class_scope" in body:
            try:
                from discovery.ingest.servicenow import normalize_cmdb_class_scope

                class_scope = normalize_cmdb_class_scope(body["cmdb_class_scope"])
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            # Store the canonical exact list on THIS org's connector override.
            # An empty list intentionally disables the bounded CMDB read.
            c["cmdb_class_scope"] = list(class_scope)
    c["configured"] = True
    c["lastSynced"] = "Just now"
    org_connector_set(org_id, connector_id, c)
    # 2.0-D4 T1 (AC1): the "edit" half of D4's connector create/edit/delete. The
    # payload names WHICH settings the request changed, never their values — the
    # body can carry customer configuration, and an audit row is not the place to
    # copy it. cmdb_class_scope is recorded by name because narrowing or widening
    # it changes what ServiceNow data a run may read.
    audit_log_event(
        CONNECTOR_CONFIGURED,
        connector_id=connector_id,
        user_id=_get_user_id_from_token(token),
        target=connector_id,
        settings_changed=sorted(body.keys()) if isinstance(body, dict) else [],
        outcome=OUTCOME_SUCCESS,
    )
    return c


@app.get("/api/confidence/explanation", dependencies=[Depends(require_auth)])
def confidence_explanation() -> Dict[str, Any]:
    return {
        "level": "MEDIUM",
        "why": ["Missing Microsoft 365 signals"],
        "nextAction": "Connect Microsoft 365 to reach High confidence",
        "recommendedNextSourceId": "m365",
    }


# ✅ ADDED FOR FRONTEND MOCK REMOVAL
@app.get("/api/confidence", dependencies=[Depends(require_auth)])
def get_confidence() -> Dict[str, Any]:
    rows = get_all("confidence")
    if rows:
        return rows[0]

    return {
        "level": "MEDIUM",
        "why": ["Missing Microsoft 365 signals"],
        "nextAction": "Connect Microsoft 365 to reach High confidence",
        "recommendedNextSourceId": "m365",
    }


@app.get("/api/permissions", dependencies=[Depends(require_auth)])
def list_permissions() -> List[Dict[str, Any]]:
    return get_all("permissions")


# ✅ ADDED FOR FRONTEND MOCK REMOVAL
@app.get("/api/mappings", dependencies=[Depends(require_auth)])
def get_mappings() -> List[Dict[str, Any]]:
    return get_all("mappings")


@app.get("/api/uploads", dependencies=[Depends(require_auth)])
def list_uploads() -> List[Dict[str, Any]]:
    return get_all("uploads")


@app.post("/api/uploads", dependencies=[Depends(require_auth)])
def add_upload(body: Dict[str, Any]) -> Dict[str, Any]:
    name = body.get("name")
    size_label = body.get("sizeLabel", "—")
    if not name:
        raise HTTPException(400, "name required")
    item = {
        "id": f"up_{uuid.uuid4().hex[:6]}",
        "name": name,
        "sizeLabel": size_label,
        "uploadedLabel": "Just now",
    }
    upsert("uploads", item["id"], item)
    return item


@app.get("/api/runs", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def list_runs() -> List[Dict[str, Any]]:
    """Runs owned by the caller's org, newest-first.

    Org-scoped LIST (not a run-scoped 'latest' fallback): lets any member of an
    org see the workspace's runs — and the creator find a run again after the
    in-memory session token is gone — instead of the run living only in one
    browser's localStorage. Run-scoped artifact endpoints still require an
    explicit runId in the URL.
    """
    org_id = get_current_org_id()
    runs = tenancy_get_runs(org_id)
    runs.sort(key=lambda r: str(r.get("startedAt") or ""), reverse=True)
    return [
        {
            "id": r.get("id"),
            "status": r.get("status"),
            "startedAt": r.get("startedAt"),
            "updatedAt": r.get("updatedAt"),
            "packId": r.get("packId"),
            "source": r.get("source"),
        }
        for r in runs
    ]


def _run_order_key(run: Dict[str, Any]) -> tuple[str, str]:
    timestamp = str(run.get("updatedAt") or run.get("startedAt") or "")
    run_id = str(run.get("id") or run.get("runId") or "")
    return (timestamp, run_id)


# NOTE: must be declared before "/api/runs/{run_id}" so "latest" is not captured
# as a run_id path param. Strictly org-scoped: a run is visible only when it
# belongs to the caller's org. Isolation goes through the shared tenancy_get_runs
# filter (same helper as GET /api/runs) instead of an ad-hoc Python filter, so
# there is no path — including untagged legacy runs — by which another org's run
# can surface here (cross-tenant exposure guard).
@app.get("/api/runs/latest", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def get_latest_run() -> Dict[str, Any]:
    db.init_tables()
    org_id = get_current_org_id()
    runs = tenancy_get_runs(org_id)
    if not runs:
        raise HTTPException(404, "run not found")

    return max(runs, key=_run_order_key)


@app.get("/api/runs/{run_id}", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def get_run(run_id: str) -> Dict[str, Any]:
    try:
        return read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")


@app.get("/api/runs/{run_id}/events", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def get_events(run_id: str) -> List[Dict[str, Any]]:
    try:
        return read_run_events(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")


@app.get("/api/runs/{run_id}/evidence", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def list_evidence(run_id: str) -> List[Dict[str, Any]]:
    run_get(run_id)
    run_ev = run_kv_get("evidence", run_id, None)
    if run_ev is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No evidence found for run '{run_id}'. "
                "Ensure PATCH_materialize_t2.py has been applied "
                "and the run has completed successfully."
            ),
        )
    return run_ev


@app.post(
    "/api/runs/{run_id}/evidence/{evidence_id}/decision",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def set_evidence_decision(
    run_id: str, evidence_id: str, body: Dict[str, Any]
) -> Dict[str, Any]:
    run_get(run_id)
    run_ev = run_kv_get("evidence", run_id, None)
    if run_ev is None:
        raise HTTPException(404, "evidence not found for this run")

    idx = next((i for i, e in enumerate(run_ev) if e["id"] == evidence_id), None)
    if idx is None:
        raise HTTPException(404, "evidence not found")

    decision = body.get("decision")
    if decision not in ("APPROVED", "REJECTED", "UNREVIEWED"):
        raise HTTPException(400, "invalid decision")

    run_ev[idx] = {**run_ev[idx], "decision": decision}
    run_kv_set("evidence", run_id, run_ev)
    e = run_ev[idx]

    audit_event = {
        "id": f"audit_{uuid4().hex[:8]}",
        "tsLabel": now_iso(),
        "tsEpoch": int(time.time()),
        "action": f"EVIDENCE_{decision}",
        "by": "Architect",
        "evidenceId": evidence_id,
    }
    audit = run_kv_get("audit", run_id, default_audit())
    run_kv_set("audit", run_id, [audit_event, *audit])
    # 2.0-D4 T1: same reasoning as the opportunity override — the run-scoped entry
    # is a view, the audit_log row is the durable record of the human decision.
    audit_log_event(
        EVIDENCE_DECISION_RECORDED,
        run_id=run_id,
        target=evidence_id,
        outcome=OUTCOME_SUCCESS,
        decision=decision,
    )
    return e



@app.get("/api/runs/{run_id}/mappings", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def list_mappings(run_id: str) -> List[Dict[str, Any]]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    # Serve precomputed enrichment when available — never re-run the LLM on a
    # read. enrich_ambiguous_mappings caches its enriched rows under
    # KV_NORMALIZATION (marked with 'batchCallMade', written only by it). Return
    # those if present; only compute — the one synchronous Claude batch call —
    # when the cache is absent, so repeat GETs never trigger an LLM round-trip.
    cached = run_kv_get(KV_NORMALIZATION, run_id, None)
    if (
        isinstance(cached, dict)
        and "batchCallMade" in cached
        and isinstance(cached.get("rows"), list)
    ):
        return cached["rows"]
    raw_mappings = get_all("mappings")
    return enrich_ambiguous_mappings(run_id, raw_mappings, db)


@app.get("/api/runs/{run_id}/opportunities", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def list_opportunities(run_id: str) -> List[Dict[str, Any]]:
    try:
        run = read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    opps = run_kv_get("opps", run_id, None)
    if opps is None:
        # The run EXISTS (read_run above did not 404) but has not materialised
        # opportunities yet — it is still computing. Return an EMPTY list (200),
        # not 404, so a client can fetch opportunities in parallel with run
        # status without erroring on an in-progress run; the "still preparing"
        # vs "no opportunities" distinction is made from run status client-side.
        # A genuinely unknown/cross-org run still 404s at read_run above, so
        # tenant isolation and the "no latest-run fallback" contract are intact.
        return []
    # Apply the full display shaping (title overrides + the stable matrix score
    # offset). The decision/override endpoints below apply the SAME shaping via
    # with_display(), so a bubble keeps its coordinates when its decision changes.
    # R18-C1 T4: then adapt the finding WORDING to the run's active template
    # (lending language for Commercial Lending). No-op when no template is active.
    # 2.0-A1 T5 / AC3 — "no projection output — API, UI, report, or export".
    # This IS the API output the Opportunity Review renders, so the projection
    # vocabulary guard runs on the way out, uniformly with the executive report.
    # Applied AFTER terminology (dev's per-pack apply_run_terminology) so a
    # template's own wording is covered too, and on copies — the stored
    # opportunity is never rewritten.
    from .projection_copy_guard import scrub_opportunity_narratives

    # 2.0-A3 T2: the bounded learned adjustment, at SERVE time — which is what
    # makes "base scoring untouched and always recoverable" structurally true
    # rather than a convention: the stored order IS the base order, so turning
    # learning off restores it with nothing to undo, and every returned finding
    # carries its own baseRank/baseImpact.
    #
    # Applied BEFORE display shaping, deliberately. with_display_scores adds a
    # deterministic per-id offset (up to +0.6) to spread bubbles on the matrix
    # chart; running the adjustment after it would compute the score cap from
    # that offset impact, making the cap vary by opportunity id for a purely
    # cosmetic reason. The cap must be a fraction of the REAL base score.
    adjusted = _apply_learned_ranking(opps)
    return scrub_opportunity_narratives(
        apply_run_terminology([with_display(opp) for opp in adjusted], run_id)
    )


def _apply_learned_ranking(opps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Route the served list through the ONE adjustment function.

    A call site, not a second implementation — ``app.learning_adjustment`` is
    the only place a learned adjustment is computed, because a second one would
    compound into movement nobody could explain.

    Non-blocking: a learning failure serves BASE order rather than no list. That
    is also the safe direction if the adjustment state is unavailable — no
    learning beats partial or stale learning.
    """
    try:
        from .learning_adjustment import adjust_ranking
        from .learning_adjustment_state import get_adjustments
        from .learning_signals import collect_learning_signals

        org_id = get_current_org_id()
        adjustments = get_adjustments(org_id)
        if not adjustments:
            return opps
        signal_set = collect_learning_signals(org_id)
        result = adjust_ranking(
            opps,
            adjustments,
            is_active=signal_set.is_active,
            inactive_reason=signal_set.inactive_reason,
        )
        return list(result.ordered)
    except Exception as exc:  # noqa: BLE001 - ranking is advisory, never fatal
        logger.warning("Learned ranking not applied: %s", exc)
        return opps


#: 2.0-A3 T1 — how a review decision maps onto a learning action. UNREVIEWED is
#: absent on purpose: clearing a decision is the absence of a judgement, and
#: recording it as one would let a reset teach the ranking layer something.
_REVIEW_DECISION_TO_LEARNING_ACTION = {"APPROVED": "accept", "REJECTED": "dismiss"}


def _mirror_decision_to_learning(
    opp: Dict[str, Any], decision: str, run_id: str
) -> None:
    """Record a review decision as a durable learning signal. Never blocks.

    The review decision answers "is this finding real?" per run; the learning
    record answers "is this finding type worth our team's time?" across runs.
    They are different questions with different lifetimes, which is why this
    mirrors rather than replaces — see ``database/models/opportunity_feedback.py``.

    A finding with no ``opportunity_identity`` (materialized before A1 T6 stamped
    it) is skipped rather than recorded under a run-scoped id: a learning signal
    that cannot be matched on the next run is worse than no signal, because it
    counts towards the cold-start threshold while informing nothing.
    """
    action = _REVIEW_DECISION_TO_LEARNING_ACTION.get(decision)
    if not action:
        return
    identity = str(opp.get("opportunity_identity") or "").strip()
    if not identity:
        return
    try:
        from .learning_feedback import record_feedback

        debug = opp.get("_debug") if isinstance(opp.get("_debug"), dict) else {}
        record_feedback(
            get_current_org_id(),
            identity,
            action,
            actor_id="review_decision",
            detector_id=(debug or {}).get("detector_id"),
            pack_id=opp.get("packId"),
            run_id=run_id,
        )
    except Exception as exc:  # noqa: BLE001 - learning must never break review
        logger.warning("Could not mirror decision to learning record: %s", exc)


@app.post(
    "/api/runs/{run_id}/opportunities/{opp_id}/decision",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def set_opp_decision(run_id: str, opp_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    decision = body.get("decision")
    if decision not in ("APPROVED", "REJECTED", "UNREVIEWED"):
        raise HTTPException(400, "invalid decision")
    run_opps = run_kv_get("opps", run_id)
    if run_opps is not None:
        idx = next((i for i, o in enumerate(run_opps) if o["id"] == opp_id), None)
        if idx is None:
            raise HTTPException(404, "opportunity not found")
        previous_decision = run_opps[idx].get("decision", "UNREVIEWED")
        run_opps[idx] = {**run_opps[idx], "decision": decision}
        run_kv_set("opps", run_id, run_opps)
        o = run_opps[idx]
    else:
        o = get_one("opportunities", opp_id)
        if not o:
            raise HTTPException(404, "opportunity not found")
        previous_decision = o.get("decision", "UNREVIEWED")
        o["decision"] = decision
        upsert("opportunities", opp_id, o)
    # R18-C0 P8: a decision change is recorded as a NEW audit/feedback event —
    # never an overwrite. Each event captures the actor, timestamp, the new
    # decision, and the prior decision it replaced, so the full decision history
    # (and each transition) stays queryable for audit and the 2.0
    # feedback-learning loop. The opportunity's `decision` field holds only the
    # current value; the append-only audit list is the history of record. A
    # genuine no-op re-click (same decision) does not append a spurious event.
    if decision != previous_decision:
        event = {
            "id": f"audit_{uuid4().hex[:8]}",
            "tsLabel": now_iso(),
            "tsEpoch": int(time.time()),
            "action": decision,
            "previousDecision": previous_decision,
            "by": "Architect",
            "opportunityId": opp_id,
        }
        audit = run_kv_get("audit", run_id, default_audit())
        run_kv_set("audit", run_id, [event, *audit])
        # 2.0-A3 T1: mirror the review decision into the durable learning record.
        # APPROVED/REJECTED is a per-run judgement stored in a KV blob that
        # materialization rewrites and replay resets; the learning layer needs it
        # keyed on the stable opportunity_identity and surviving the run. Mirroring
        # here means the existing analyst UI feeds learning with no frontend
        # change. UNREVIEWED is deliberately not mirrored — clearing a decision is
        # the absence of a judgement, not a third kind of one.
        _mirror_decision_to_learning(o, decision, run_id)
    # with_display (not just title) so impact/effort carry the same stable matrix
    # offset as the list endpoint — otherwise the bubble jumps when its decision
    # response replaces the listed opportunity in the UI. R18-C1 T4: same
    # template terminology as the list endpoint so wording stays consistent.
    return apply_run_terminology(with_display(o), run_id)


@app.post(
    "/api/runs/{run_id}/opportunities/{opp_id}/override",
    dependencies=[Depends(require_auth), Depends(require_role("analyst"))],
)
def set_opp_override(run_id: str, opp_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    # --- VALIDATION START ---
    rationale = body.get("rationaleOverride", "").strip()
    reason = body.get("overrideReason", "").strip()

    # Validation: If rationale is provided, a reason must be provided.
    # Exception: If both are empty, it's a "reset" and should be allowed (as per test 15).
    if rationale and not reason:
        raise HTTPException(
            status_code=400,
            detail="An override reason is required when providing a rationale override.",
        )
    # --- VALIDATION END ---
    try:
        read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")
    run_opps = run_kv_get("opps", run_id)
    if run_opps is not None:
        idx = next((i for i, o in enumerate(run_opps) if o["id"] == opp_id), None)
        if idx is None:
            raise HTTPException(404, "opportunity not found")
        o = dict(run_opps[idx])
    else:
        o = get_one("opportunities", opp_id)
        if not o:
            raise HTTPException(404, "opportunity not found")
    override = o.get("override") or {}
    override["rationaleOverride"] = body.get(
        "rationaleOverride", override.get("rationaleOverride", "")
    )
    override["overrideReason"] = body.get(
        "overrideReason", override.get("overrideReason", "")
    )
    override["isLocked"] = bool(body.get("isLocked", override.get("isLocked", False)))
    override["updatedAt"] = now_iso()
    o["override"] = override
    if run_opps is not None:
        run_opps[idx] = o
        run_kv_set("opps", run_id, run_opps)
    else:
        upsert("opportunities", opp_id, o)
    event = {
        "id": f"audit_{uuid4().hex[:8]}",
        "tsLabel": now_iso(),
        "tsEpoch": int(time.time()),
        "action": "OVERRIDE_SAVED",
        "by": "Architect",
        "opportunityId": opp_id,
    }
    audit = run_kv_get("audit", run_id, default_audit())
    run_kv_set("audit", run_id, [event, *audit])
    # 2.0-D4 T1: the run-scoped entry above feeds the run's audit VIEW, but it lives
    # in the `audit` run-KV blob, which materialization rewrites and replay resets —
    # so it is not a durable record that a human made this call. D4 names "analyst
    # decisions" as a state-changing action, so the organisation-wide immutable
    # audit_log gets its own row.
    audit_log_event(
        OPPORTUNITY_DECISION_RECORDED,
        run_id=run_id,
        target=opp_id,
        outcome=OUTCOME_SUCCESS,
        action="override_saved",
        is_locked=override["isLocked"],
        has_rationale_override=bool(override.get("rationaleOverride")),
        override_reason=override.get("overrideReason") or None,
    )
    # with_display so the override response carries the same stable matrix offset
    # as the list endpoint (keeps the bubble coordinate-stable on override save).
    # R18-C1 T4: same template terminology as the list endpoint.
    return apply_run_terminology(with_display(o), run_id)


@app.get("/api/runs/{run_id}/audit", dependencies=[Depends(require_auth), Depends(require_role("owner"))])
def list_audit(run_id: str) -> List[Dict[str, Any]]:
    run_get(run_id)
    audit = run_kv_get("audit", run_id, default_audit())
    return sorted(audit, key=lambda e: int(e.get("tsEpoch", 0)), reverse=True)


@app.get("/api/runs/{run_id}/roadmap", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def get_roadmap(run_id: str) -> Dict[str, Any]:
    run_get(run_id)
    # R18-C1 T4: adapt roadmap wording (embedded finding titles/descriptions,
    # stage summaries) to the run's active template. No-op without a template.
    run_roadmap = run_kv_get("roadmap", run_id, None)
    if run_roadmap is not None:
        # 2.0-A3 T2: the learned adjustment at SERVE time. The STORED roadmap is
        # base order — build_roadmap runs during materialization and must stay
        # learning-free, or disabling learning could not restore what was stored.
        return apply_run_terminology(
            with_roadmap_display_titles(apply_learned_adjustment(run_roadmap)), run_id
        )
    opps = run_kv_get("opps", run_id, None)
    if opps is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No opportunities for run '{run_id}'. "
                "T2 materialisation has not completed for this run."
            ),
        )
    return apply_run_terminology(
        apply_learned_adjustment(build_roadmap(with_display_titles(opps))), run_id
    )


def _with_run_completeness(report: Dict[str, Any], run: Dict[str, Any]) -> Dict[str, Any]:
    """2.0-D4 T5 — stamp the run's completeness onto an executive report.

    Applied on the SERVE path, and to the stored report as well as a freshly
    composed one, so a report materialised before this shipped still tells the
    truth about its run. Same retrofit shape the outcome section uses.

    This is the surface the subtask singles out: the executive report is the
    artifact most likely to reach someone who will never open a health panel. If
    a partial run's report reads identically to a complete one, none of the rest
    of this work matters.

    ``sourcesAnalyzed`` is corrected here too. It counted the sources the run was
    CONFIGURED with, so a run whose ServiceNow died reported the same "2
    connected" as a clean one — the precise failure this subtask exists to stop.
    """
    if not isinstance(report, dict):
        return report
    try:
        from .run_completeness import build_run_completeness

        # No live model/storage probing on a read of a historical run: the
        # question here is what THIS run delivered, not what the environment
        # looks like right now.
        completeness = build_run_completeness(run, include_environment=False)
        stamped = {**report, "runCompleteness": completeness.to_dict()}

        sources = stamped.get("sourcesAnalyzed")
        if isinstance(sources, dict):
            succeeded = run.get("succeeded")
            if isinstance(succeeded, (list, tuple)):
                stamped["sourcesAnalyzed"] = {
                    **sources,
                    # What actually contributed, not what was configured.
                    "totalConnected": len(succeeded),
                    "sourcesRequested": len(_requested_system_ids(run)),
                    "sourcesFailed": len(completeness.degraded_components),
                }
        return stamped
    except Exception as exc:  # noqa: BLE001 - a report must still render
        logger.warning("Could not stamp run completeness: %s", exc)
        return report


def _requested_system_ids(run: Dict[str, Any]) -> List[str]:
    inputs = run.get("inputs") or {}
    systems = inputs.get("systems")
    return [str(s) for s in systems] if isinstance(systems, (list, tuple)) else []


@app.get("/api/runs/{run_id}/executive-report", dependencies=[Depends(require_auth), Depends(require_role("viewer"))])
def get_exec_report(run_id: str) -> Dict[str, Any]:
    try:
        run = read_run(run_id)
    except KeyError:
        raise HTTPException(404, "run not found")

    # R18-C1 T4: adapt executive-report wording (the AI executive summary and the
    # embedded quick-win finding titles/descriptions) to the active template.
    er = run_kv_get("executive_report", run_id, None)

    if er:
        if isinstance(er, dict) and "outcomeSection" not in er:
            from .outcome_surfaces import build_executive_outcome_section

            er = {
                **er,
                "outcomeSection": build_executive_outcome_section(
                    get_current_org_id(),
                    run_id,
                ),
            }
        return apply_run_terminology(
            with_exec_report_display_titles(_with_run_completeness(er, run)), run_id
        )

    inputs = run.get("inputs") or {}
    connected_sources = inputs.get("connectedSources") or []
    uploaded_files = inputs.get("uploadedFiles") or []
    sample_enabled = bool(inputs.get("sampleWorkspaceEnabled", False))

    connectors = org_connectors_list(get_current_org_id())
    name_to_tier = {c.get("name"): c.get("tier") for c in connectors}
    recommended_connected = sum(
        1 for n in connected_sources if name_to_tier.get(n) == "recommended"
    )

    sources_analyzed = {
        "recommendedConnected": recommended_connected,
        "totalConnected": len(connected_sources),
        "uploadedFiles": len(uploaded_files),
        "sampleWorkspaceEnabled": sample_enabled,
    }

    opps = run_kv_get("opps", run_id, None)
    if opps is None:
        raise HTTPException(
            status_code=404,
            detail=f"No opportunities for run '{run_id}'. T2 materialisation has not completed.",
        )

    opps = with_display_titles(opps)

    # 2.0-A1 T5 / AC3 — this route composes the executive report from stored
    # opps directly rather than through build_executive_report, so the
    # projection vocabulary guard has to run here too. Scrubbing returns copies;
    # the stored opportunity a run persisted is never rewritten.
    from .projection_copy_guard import scrub_opportunity_narratives

    opps = scrub_opportunity_narratives(opps)
    quick_wins = [o for o in opps if o.get("tier") == "Quick Win"]
    from .outcome_surfaces import build_executive_outcome_section

    return apply_run_terminology(
        _with_run_completeness({
            "confidence": "Moderate",
            "sourcesAnalyzed": sources_analyzed,
            "topQuickWins": quick_wins,
            "snapshotBubbles": [],
            "roadmapHighlights": {
                "next30Count": sum(1 for o in opps if o.get("tier") == "Quick Win"),
                "next60Count": sum(1 for o in opps if o.get("tier") == "Strategic"),
                "next90Count": sum(1 for o in opps if o.get("tier") == "Complex"),
                "blockerCount": 0,
            },
            "outcomeSection": build_executive_outcome_section(
                get_current_org_id(),
                run_id,
            ),
        }, run),
        run_id,
    )


# ---------------------------------------------------------------------------
# Audit log viewer — AT-82 T8
# ---------------------------------------------------------------------------

@app.get(
    "/api/audit-log",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def list_audit_log(
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Return org-scoped audit log entries newest-first (owner only)."""
    from .middleware.tenancy import get_current_org_id
    from .middleware.audit import _ensure_table

    _ensure_table()
    org_id = get_current_org_id()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, org_id, event_type, user_id, run_id, connector_id, payload, timestamp
            FROM audit_log
            WHERE org_id = %s
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
            """,
            (org_id, limit, offset),
        )
        rows = cur.fetchall()
    finally:
        con.close()

    import json as _json
    return [
        {
            "id": r[0], "org_id": r[1], "event_type": r[2], "user_id": r[3],
            "run_id": r[4], "connector_id": r[5],
            "payload": _json.loads(r[6]) if r[6] else None,
            "timestamp": r[7],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Workspace members — AT-82 T8
# ---------------------------------------------------------------------------

@app.get(
    "/api/workspace/members",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def list_members() -> List[Dict[str, Any]]:
    """List workspace members (owner only)."""
    from .middleware.tenancy import get_current_org_id
    from .rbac import _ensure_members_table

    _ensure_members_table()
    org_id = get_current_org_id()
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT org_id, user_id, role, created_at FROM workspace_members "
            "WHERE org_id = %s AND is_deleted = FALSE",
            (org_id,),
        )
        rows = cur.fetchall()
    finally:
        con.close()
    return [{"org_id": r[0], "user_id": r[1], "role": r[2], "created_at": r[3]} for r in rows]


@app.post(
    "/api/workspace/members",
    dependencies=[Depends(require_auth), Depends(require_role("owner"))],
)
def add_member(body: Dict[str, Any]) -> Dict[str, Any]:
    """Add or update a workspace member (owner only)."""
    from .middleware.tenancy import get_current_org_id
    from .rbac import _ensure_members_table

    _ensure_members_table()
    org_id = get_current_org_id()
    user_id = body.get("user_id", "").strip()
    role = body.get("role", "").strip()
    if not user_id:
        raise HTTPException(400, "user_id required")
    if role not in ("owner", "analyst", "viewer"):
        raise HTTPException(400, "role must be owner, analyst, or viewer")

    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (org_id, user_id) DO UPDATE SET role = EXCLUDED.role, is_deleted = FALSE
            """,
            (org_id, user_id, role, db.now_iso()),
        )
        con.commit()
    finally:
        con.close()
    return {"org_id": org_id, "user_id": user_id, "role": role}
