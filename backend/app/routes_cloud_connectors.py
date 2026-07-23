"""MSP-B13 / AT-745 (T3) — Cloud Connector Onboarding config/validation routes.

The thin backend half of MSP-B13: onboard the AWS and Azure Event Connectors
(MSP-B1 / MSP-B2) through the existing Integration Hub as its first
*multi-scope* connector cards — one connection, many accounts/subscriptions, each
scope a system. This module adds ONLY configuration + validation routes; it
introduces NO new ingestion logic and reuses the existing credential vault and the
B1/B2 connector implementations (auth, partition/environment maps, health).

Routes (Section 1 of the story):

    POST   /api/connectors/{aws_events|azure_events}          create + vault write
    POST   /api/connectors/{id}/test                          auth + reachability probe
    GET    /api/connectors/{id}/scopes                        list pinned scopes + candidates
    POST   /api/connectors/{id}/scopes                        pin a scope (validated)
    DELETE /api/connectors/{id}/scopes/{scope}                unpin (forward-only)
    GET    /api/connectors/{id}/scopes/{scope}/health         per-scope health (card + run health)

Design rules held here:

  * WRITE-ONLY secrets (B13 AC2 / R17-D3): every secret field is encrypted into
    the per-org vault via the existing static-credential machinery and is NEVER
    returned by any response, render, or edit form. Responses carry non-secret
    metadata only.
  * RBAC (B13 AC1): Owner-only create / test / pin / unpin; Analyst/Viewer see
    health and scope lists only (never credentials or edit controls).
  * Forward-only scope activation (B13 AC4 / MSP-B2 AC7): a scope only ingests
    once an Owner PINS it. Discovered-but-unpinned subscriptions/accounts are
    surfaced as CANDIDATES, never ingested. Unpinning stops ingestion forward-only
    and retains history (the pinned set is the only thing removed).
  * Test-connection validates BEFORE save (B13 AC3): the probe runs against the
    submitted credentials without persisting them, and reports provider-specific
    failures (bad role ARN / wrong partition / expired secret) in actionable
    language. The probes are module-level so tests substitute deterministic ones
    (no boto3 / no network).
  * Partition / environment selection (B13 AC8): drives the endpoint map from
    MSP-B1 (``aws_partitions``) / MSP-B2 (``azure_environments``); an invalid
    value is rejected at config time.

The pinned scopes live on this org's connector record (``db.org_connector_get`` /
``org_connector_set``) under ``scopes``; the per-scope system counting the pricing
sentence needs is layered on top by T4 (AT-746), which reads the same record.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel

from app import db
from app.auth.secrets import MissingSecretError
from app.middleware.audit import log_event
from app.middleware.tenancy import get_current_org_id
from app.rbac import _get_user_id_from_token, require_role
from app.security import require_auth

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connector identity
# ---------------------------------------------------------------------------

AWS_EVENTS = "aws_events"
AZURE_EVENTS = "azure_events"

#: The multi-scope cloud connectors this module onboards. Any other id is a 404
#: through these routes (they are OAuth / static / native-DB connectors handled by
#: routes_connector_auth / the DB-connector routes).
CLOUD_CONNECTORS = frozenset({AWS_EVENTS, AZURE_EVENTS})

_PROVIDER_OF = {AWS_EVENTS: "aws", AZURE_EVENTS: "azure"}
_SCOPE_KIND = {AWS_EVENTS: "aws_account", AZURE_EVENTS: "azure_subscription"}

# Per-scope health vocabulary — SHARED with the connectors' run-health surfaces
# (aws_health.STATUS_* / the Azure subscription_status categories) so a failure
# reads the same word on the card and in run health (B13 AC7). ``pending`` is the
# state of a freshly-pinned scope that has not been polled by a run yet.
SCOPE_STATUS_PENDING = "pending"
SCOPE_STATUS_OK = "ok"
SCOPE_STATUS_AUTH_FAILED = "auth_failed"
SCOPE_STATUS_PARTIAL = "partial"
SCOPE_STATUS_FAILED = "failed"

_HEALTHY_STATUSES = frozenset({SCOPE_STATUS_OK})


class CloudProbeError(Exception):
    """A test/validation probe failed with a provider-specific, actionable reason.

    ``reason`` is a stable machine code (e.g. ``authentication_failed``); ``message``
    is the human-facing, actionable text. Never carries a secret value.
    """

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(f"{reason}: {message}")


# ---------------------------------------------------------------------------
# Validation probes (module-level so tests substitute deterministic ones)
#
# The defaults reach the real providers (STS GetCallerIdentity / AssumeRole; the
# Azure AD client-credentials token exchange). They are pure functions of their
# inputs, catch provider exceptions, and translate them into a CloudProbeError with
# a provider-specific reason — so the route layer stays provider-agnostic and the
# tests can seed each failure by substituting the probe. boto3 is imported lazily
# inside the AWS probes, so importing this module never requires the SDK.
# ---------------------------------------------------------------------------


def probe_aws_hub_credentials(
    *,
    access_key_id: str,
    secret_access_key: str,
    session_token: str = "",
    partition: str = "aws",
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate hub/direct AWS keys via ``sts:GetCallerIdentity`` (auth probe).

    Raises :class:`CloudProbeError` with a provider-specific reason on a bad
    partition/region, rejected keys, or an unreachable endpoint. Returns the caller
    account id on success (non-secret identity).
    """
    _validate_aws_partition(partition, region)
    probe_region = region or _default_probe_region(partition)
    try:
        from discovery.ingest.aws_auth import AWSCredentials, Boto3ClientFactory

        creds = AWSCredentials(
            access_key_id,
            secret_access_key,
            session_token=session_token or None,
            source="hub",
        )
        sts = Boto3ClientFactory().client("sts", region=probe_region, credentials=creds)
        resp = sts.get_caller_identity() or {}
    except CloudProbeError:
        raise
    except Exception as exc:  # noqa: BLE001 — classify into an actionable reason
        raise _classify_aws_failure(exc) from exc
    return {"identity": str(resp.get("Account") or resp.get("Arn") or "")}


def probe_aws_assume_role(org_id: str, account_config: Any) -> Dict[str, Any]:
    """Validate an AWS account scope by assuming its read-only role from the hub.

    Uses the org's VAULTED hub credential (already stored by the create route) via
    :class:`~discovery.ingest.aws_auth.AWSAuthenticator`, so the assume-role probe
    proves the whole cross-account path works. Raises :class:`CloudProbeError` when
    the role cannot be assumed (bad ARN, missing trust, no hub credential).
    """
    try:
        from discovery.ingest.aws_auth import AWSAuthenticator

        creds = AWSAuthenticator().credentials_for(org_id, account_config)
    except CloudProbeError:
        raise
    except Exception as exc:  # noqa: BLE001 — AWSAuthError or a boto3 error
        raise _classify_aws_failure(exc, assume_role=True) from exc
    return {"identity": getattr(creds, "source", "")}


async def probe_azure_service_principal(
    org_id: str, *, service_principal: Any, config: Any
) -> Dict[str, Any]:
    """Validate an Azure service principal by minting an ARM-scoped token.

    Exercises the exact client-credentials exchange the connector uses, so a
    rejected secret / wrong tenant / unreachable authority surfaces here BEFORE
    save. Raises :class:`CloudProbeError` on failure.
    """
    try:
        from discovery.ingest.azure_events import acquire_arm_token

        await acquire_arm_token(
            org_id, config, service_principal=service_principal
        )
    except CloudProbeError:
        raise
    except Exception as exc:  # noqa: BLE001 — AzureAuthError / OAuthError / network
        raise _classify_azure_failure(exc) from exc
    return {"identity": getattr(service_principal, "tenant_id", "")}


def _default_probe_region(partition: str) -> Optional[str]:
    """A usable region for the STS probe (GovCloud has no global STS endpoint)."""
    from discovery.ingest.aws_partitions import PARTITION_GOVCLOUD

    return "us-gov-west-1" if partition == PARTITION_GOVCLOUD else None


def _validate_aws_partition(partition: str, region: Optional[str]) -> None:
    """Raise :class:`CloudProbeError` for an unknown partition or contradictory region."""
    from discovery.ingest.aws_partitions import PartitionError, validate_region

    try:
        validate_region(partition, region)
    except PartitionError as exc:
        raise CloudProbeError("invalid_partition", str(exc)) from exc


def _classify_aws_failure(exc: Exception, *, assume_role: bool = False) -> CloudProbeError:
    """Map a boto3 / AWS auth exception onto a provider-specific probe failure."""
    try:
        from discovery.ingest.aws_auth import AWSAuthError
    except Exception:  # pragma: no cover - import guard
        AWSAuthError = ()  # type: ignore[assignment]

    # A resolved-credential failure from the authenticator (no hub key / cannot
    # assume) — the assume-role path's most common cause.
    if AWSAuthError and isinstance(exc, AWSAuthError):
        if assume_role:
            return CloudProbeError(
                "assume_role_failed",
                "Could not assume the account role. Check the role ARN, its trust "
                "policy for the hub identity, and the external id.",
            )
        return CloudProbeError(
            "authentication_failed",
            "AWS rejected the credentials. Check the access key id and secret.",
        )

    # botocore ClientError carries a provider error code we can act on.
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code") or "")

    if code in ("InvalidClientTokenId", "SignatureDoesNotMatch", "UnrecognizedClientException"):
        return CloudProbeError(
            "authentication_failed",
            "AWS rejected the credentials (invalid access key id or secret).",
        )
    if code in ("ExpiredToken", "ExpiredTokenException", "TokenRefreshRequired"):
        return CloudProbeError(
            "credentials_expired",
            "The AWS session token has expired. Provide fresh credentials.",
        )
    if code in ("AccessDenied", "AccessDeniedException"):
        return CloudProbeError(
            "assume_role_failed" if assume_role else "authorization_failed",
            "AWS denied access. Check the role's trust policy and the minimal "
            "read-only IAM policy are attached.",
        )
    try:
        from discovery.ingest.aws_health import is_throttle_error

        if is_throttle_error(exc):
            return CloudProbeError(
                "throttled", "AWS throttled the request. Retry the test shortly."
            )
    except Exception:  # pragma: no cover - defensive
        pass
    return CloudProbeError(
        "unreachable",
        f"Could not reach AWS to validate the connection ({type(exc).__name__}).",
    )


def _classify_azure_failure(exc: Exception) -> CloudProbeError:
    """Map an Azure auth / OAuth exception onto a provider-specific probe failure."""
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
    if status == 401 or "auth" in type(exc).__name__.lower():
        return CloudProbeError(
            "authentication_failed",
            "Azure AD rejected the service principal. Check the client id, client "
            "secret, and tenant id.",
        )
    if status == 403:
        return CloudProbeError(
            "authorization_failed",
            "The service principal authenticated but lacks Reader access. Assign "
            "the minimal read-only role on the subscriptions.",
        )
    return CloudProbeError(
        "unreachable",
        f"Could not reach Azure AD / ARM to validate the connection "
        f"({type(exc).__name__}).",
    )


# ---------------------------------------------------------------------------
# Request / response models (metadata only — never a secret)
# ---------------------------------------------------------------------------


class TestConnectionResult(BaseModel):
    """Outcome of a test-connection probe (B13 AC3).

    ``ok`` is the pass/fail; ``reason`` is a stable code and ``message`` the
    actionable text on failure. The endpoint itself returns 200 (the probe ran) —
    the body carries the verdict so the UI can render provider-specific guidance.
    Never carries a credential value.
    """

    connector_id: str
    provider: str
    ok: bool
    reason: Optional[str] = None
    message: str
    identity: Optional[str] = None


class CloudConnectionStatus(BaseModel):
    """Non-secret status of a cloud connection (B13 AC2 — write-only secrets)."""

    connector_id: str
    provider: str
    configured: bool
    status: str
    partition: Optional[str] = None       # AWS
    environment: Optional[str] = None      # Azure
    mode: Optional[str] = None             # Azure (lighthouse / direct)
    scope_count: int = 0
    updated_at: Optional[str] = None


class ScopeView(BaseModel):
    """One pinned scope as the card shows it (its identity as ONE SYSTEM)."""

    scope_id: str
    kind: str
    label: Optional[str] = None
    status: str
    pinned_at: Optional[str] = None
    # AWS
    role_arn: Optional[str] = None
    external_id_set: bool = False
    regions: List[str] = []
    partition: Optional[str] = None
    # Azure
    environment: Optional[str] = None
    # populated by discovery runs (card fields), absent until a run polls the scope
    last_checkpoint_at: Optional[str] = None
    event_volume_last_run: Optional[int] = None


class ScopesResponse(BaseModel):
    """The scope panel: pinned scopes + candidates pending Owner approval (AC4)."""

    connector_id: str
    provider: str
    scopes: List[ScopeView]
    candidates: List[str] = []


class ScopeHealthResponse(BaseModel):
    """Per-scope health — the same state the card and run health share (AC7)."""

    connector_id: str
    scope_id: str
    status: str
    healthy: bool
    message: Optional[str] = None
    last_checkpoint_at: Optional[str] = None
    event_volume_last_run: Optional[int] = None
    surfaces_ok: List[str] = []
    surfaces_failed: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# Record helpers (the pinned scopes live on this org's connector record)
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_cloud_connector(connector_id: str) -> str:
    if connector_id not in CLOUD_CONNECTORS:
        raise HTTPException(status_code=404, detail="Unknown cloud connector")
    return _PROVIDER_OF[connector_id]


def _load_record(org_id: str, connector_id: str) -> Dict[str, Any]:
    return dict(db.org_connector_get(org_id, connector_id) or {})


def _scopes_of(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    scopes = record.get("scopes")
    return [dict(s) for s in scopes] if isinstance(scopes, list) else []


def _save_record(org_id: str, connector_id: str, record: Dict[str, Any]) -> None:
    db.org_connector_set(org_id, connector_id, record)


def _scope_view(scope: Dict[str, Any]) -> ScopeView:
    return ScopeView(
        scope_id=str(scope.get("scope_id", "")),
        kind=str(scope.get("kind", "")),
        label=scope.get("label"),
        status=str(scope.get("status", SCOPE_STATUS_PENDING)),
        pinned_at=scope.get("pinned_at"),
        role_arn=scope.get("role_arn"),
        external_id_set=bool(scope.get("external_id_set")),
        regions=list(scope.get("regions") or []),
        partition=scope.get("partition"),
        environment=scope.get("environment"),
        last_checkpoint_at=scope.get("last_checkpoint_at"),
        event_volume_last_run=scope.get("event_volume_last_run"),
    )


def _connection_status_from(connector_id: str, record: Dict[str, Any]) -> CloudConnectionStatus:
    return CloudConnectionStatus(
        connector_id=connector_id,
        provider=_PROVIDER_OF[connector_id],
        configured=bool(record.get("configured")),
        status=str(record.get("status", "not_configured")),
        partition=record.get("partition"),
        environment=record.get("environment"),
        mode=record.get("mode"),
        scope_count=len(_scopes_of(record)),
        updated_at=record.get("connection_updated_at"),
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_cloud_connector_routes(app: FastAPI) -> None:

    # -----------------------------------------------------------------------
    # Create connection (Owner) — vault write, write-only secrets (AC1/AC2/AC8)
    # -----------------------------------------------------------------------
    @app.post(
        "/api/connectors/{connector_id}",
        response_model=CloudConnectionStatus,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    def create_cloud_connection(
        connector_id: str,
        body: Dict[str, Any],
        token: str = Depends(require_auth),
    ) -> CloudConnectionStatus:
        """Create (or rotate) an AWS/Azure Event connection for the caller's org.

        Owner-only. Secret fields are encrypted into the per-org vault via the
        R17-D3 static-credential path and NEVER returned (AC2). The non-secret
        partition (AWS) / environment + mode (Azure) selection is stored on the
        connector record and drives the endpoint map (AC8). The org is taken from
        the tenancy context, never the body.
        """
        provider = _require_cloud_connector(connector_id)
        body = body or {}
        org_id = get_current_org_id()

        record = _load_record(org_id, connector_id)
        if connector_id == AWS_EVENTS:
            _store_aws_connection(org_id, body, record)
        else:
            _store_azure_connection(org_id, body, record)

        record["provider"] = provider
        record["configured"] = True
        record["status"] = "connected"
        record["connection_updated_at"] = _now_iso()
        record.setdefault("scopes", [])
        _save_record(org_id, connector_id, record)

        # Audit the ACTION and actor only — never the credential values (AC2).
        log_event(
            "connector_credentials_set",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return _connection_status_from(connector_id, record)

    # -----------------------------------------------------------------------
    # Test connection (Owner) — validates BEFORE save, provider-specific (AC3)
    # -----------------------------------------------------------------------
    @app.post(
        "/api/connectors/{connector_id}/test",
        response_model=TestConnectionResult,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    async def test_cloud_connection(
        connector_id: str, body: Optional[Dict[str, Any]] = None
    ) -> TestConnectionResult:
        """Validate the submitted credentials WITHOUT persisting them (AC3).

        Owner-only. Runs the provider auth+reachability probe against the request
        body (a Test-connection precedes save), returning a pass/fail verdict with a
        provider-specific, actionable reason on failure. The endpoint returns 200
        even on a failed probe — the probe ran; the verdict is in the body.
        """
        provider = _require_cloud_connector(connector_id)
        body = body or {}
        org_id = get_current_org_id()
        try:
            if connector_id == AWS_EVENTS:
                result = _test_aws(body)
            else:
                result = await _test_azure(org_id, body)
        except CloudProbeError as exc:
            return TestConnectionResult(
                connector_id=connector_id,
                provider=provider,
                ok=False,
                reason=exc.reason,
                message=exc.message,
            )
        return TestConnectionResult(
            connector_id=connector_id,
            provider=provider,
            ok=True,
            message="Connection validated.",
            identity=result.get("identity") or None,
        )

    # -----------------------------------------------------------------------
    # List scopes (Viewer+) — pinned scopes + candidates (AC4)
    # -----------------------------------------------------------------------
    @app.get(
        "/api/connectors/{connector_id}/scopes",
        response_model=ScopesResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    def list_scopes(connector_id: str) -> ScopesResponse:
        """List the org's PINNED scopes (each shown as one system) plus candidates.

        Viewer+ (read-only). Candidates are discovered-but-unpinned scopes surfaced
        for Owner approval; they are reported, never ingested, so the connected
        estate never grows on its own (AC4 / MSP-B2 AC7).
        """
        provider = _require_cloud_connector(connector_id)
        org_id = get_current_org_id()
        record = _load_record(org_id, connector_id)
        scopes = [_scope_view(s) for s in _scopes_of(record)]
        candidates = [str(c) for c in (record.get("candidate_scopes") or [])]
        pinned_ids = {s.scope_id for s in scopes}
        candidates = [c for c in candidates if c not in pinned_ids]
        return ScopesResponse(
            connector_id=connector_id,
            provider=provider,
            scopes=scopes,
            candidates=candidates,
        )

    # -----------------------------------------------------------------------
    # Pin a scope (Owner) — validated, forward-only activation (AC3/AC4)
    # -----------------------------------------------------------------------
    @app.post(
        "/api/connectors/{connector_id}/scopes",
        response_model=ScopesResponse,
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    def pin_scope(
        connector_id: str,
        body: Dict[str, Any],
        token: str = Depends(require_auth),
    ) -> ScopesResponse:
        """Pin (activate) a scope for the caller's org — forward-only (AC4).

        Owner-only. AWS: add an account by role ARN, validated by an assume-role
        probe (or direct keys, validated by an auth probe); Azure: pin a
        subscription id. A pinned scope is the only thing the connector ingests, so
        activation is explicit and never automatic. Re-pinning an existing scope
        updates it in place (idempotent).
        """
        provider = _require_cloud_connector(connector_id)
        body = body or {}
        org_id = get_current_org_id()
        record = _load_record(org_id, connector_id)
        if not record.get("configured"):
            raise HTTPException(
                status_code=400,
                detail="Create the connection before pinning a scope.",
            )

        if connector_id == AWS_EVENTS:
            scope = _pin_aws_scope(org_id, body)
        else:
            scope = _pin_azure_scope(record, body)

        scopes = _scopes_of(record)
        scopes = [s for s in scopes if s.get("scope_id") != scope["scope_id"]]
        scopes.append(scope)
        record["scopes"] = scopes
        # A pinned scope is no longer a candidate.
        record["candidate_scopes"] = [
            c for c in (record.get("candidate_scopes") or [])
            if str(c) != scope["scope_id"]
        ]
        _save_record(org_id, connector_id, record)

        log_event(
            "scope_declared",
            connector_id=connector_id,
            user_id=_get_user_id_from_token(token),
        )
        return ScopesResponse(
            connector_id=connector_id,
            provider=provider,
            scopes=[_scope_view(s) for s in scopes],
            candidates=[str(c) for c in (record.get("candidate_scopes") or [])],
        )

    # -----------------------------------------------------------------------
    # Unpin a scope (Owner) — stops ingestion forward-only, retains history (AC6)
    # -----------------------------------------------------------------------
    @app.delete(
        "/api/connectors/{connector_id}/scopes/{scope_id}",
        dependencies=[Depends(require_auth), Depends(require_role("owner"))],
    )
    def unpin_scope(
        connector_id: str, scope_id: str, token: str = Depends(require_auth)
    ) -> Response:
        """Unpin a scope — stops future ingestion, retains history (AC6).

        Owner-only. Removing the scope from the pinned set stops the connector
        ingesting it on the NEXT run (forward-only); already-ingested history is
        untouched (no silent data deletion). Idempotent — a 204 even when the scope
        was never pinned.
        """
        _require_cloud_connector(connector_id)
        org_id = get_current_org_id()
        record = _load_record(org_id, connector_id)
        scopes = _scopes_of(record)
        remaining = [s for s in scopes if s.get("scope_id") != scope_id]
        if len(remaining) != len(scopes):
            record["scopes"] = remaining
            _save_record(org_id, connector_id, record)
            log_event(
                "scope_declared",
                connector_id=connector_id,
                user_id=_get_user_id_from_token(token),
            )
        return Response(status_code=204)

    # -----------------------------------------------------------------------
    # Scope health (Viewer+) — the state the card + run health share (AC4/AC7)
    # -----------------------------------------------------------------------
    @app.get(
        "/api/connectors/{connector_id}/scopes/{scope_id}/health",
        response_model=ScopeHealthResponse,
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    def scope_health(connector_id: str, scope_id: str) -> ScopeHealthResponse:
        """Return one scope's health (auth/partial/failed) + last-run facts (AC4/AC7).

        Viewer+ (read-only). The status uses the SAME vocabulary as the connector's
        run-health surface, so a revoked credential reads ``auth_failed`` on the
        card exactly as it does in run health. A freshly-pinned scope not yet polled
        by a run is ``pending``.
        """
        _require_cloud_connector(connector_id)
        org_id = get_current_org_id()
        record = _load_record(org_id, connector_id)
        scope = next(
            (s for s in _scopes_of(record) if s.get("scope_id") == scope_id), None
        )
        if scope is None:
            raise HTTPException(status_code=404, detail="Scope not found")
        status = str(scope.get("status", SCOPE_STATUS_PENDING))
        return ScopeHealthResponse(
            connector_id=connector_id,
            scope_id=scope_id,
            status=status,
            healthy=status in _HEALTHY_STATUSES,
            message=scope.get("health_message"),
            last_checkpoint_at=scope.get("last_checkpoint_at"),
            event_volume_last_run=scope.get("event_volume_last_run"),
            surfaces_ok=list(scope.get("surfaces_ok") or []),
            surfaces_failed=dict(scope.get("surfaces_failed") or {}),
        )


# ---------------------------------------------------------------------------
# Provider-specific create / test / pin helpers
# ---------------------------------------------------------------------------


def _missing_fields(pairs: List[tuple]) -> List[str]:
    return [label for label, value in pairs if not (value or "").strip()]


def _store_aws_connection(
    org_id: str, body: Dict[str, Any], record: Dict[str, Any]
) -> None:
    """Vault the AWS hub credential + record the (non-secret) partition (AC2/AC8)."""
    from discovery.ingest.aws_auth import HUB_CONNECTOR_ID
    from discovery.ingest.aws_partitions import PARTITION_AWS, PartitionError, partition_for

    partition = str(body.get("partition") or PARTITION_AWS).strip() or PARTITION_AWS
    try:
        partition_for(partition)
    except PartitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    access_key_id = str(body.get("access_key_id") or "").strip()
    secret_access_key = str(body.get("secret_access_key") or "")
    missing = _missing_fields(
        [("access key id", access_key_id), ("secret access key", secret_access_key)]
    )
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required field(s): {', '.join(missing)}.",
        )

    _vault_store_static(
        org_id,
        HUB_CONNECTOR_ID,
        username=access_key_id,
        secret=secret_access_key,
        base_url=partition,
    )
    record["partition"] = partition


def _store_azure_connection(
    org_id: str, body: Dict[str, Any], record: Dict[str, Any]
) -> None:
    """Vault the Azure service principal + record environment/mode (AC2/AC8)."""
    from discovery.ingest.azure_events_config import (
        CONNECTOR_ID,
        DEFAULT_MODE,
        _VALID_MODES,
        resolve_environment,
        AzureEventConfigError,
    )

    environment = str(body.get("environment") or "").strip()
    try:
        env = resolve_environment(environment or None)
    except AzureEventConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    mode = str(body.get("mode") or DEFAULT_MODE).strip().lower() or DEFAULT_MODE
    if mode not in _VALID_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown Azure access mode {mode!r}; supported: {sorted(_VALID_MODES)}.",
        )

    tenant_id = str(body.get("tenant_id") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    client_secret = str(body.get("client_secret") or "")
    missing = _missing_fields(
        [("tenant id", tenant_id), ("client id", client_id), ("client secret", client_secret)]
    )
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required field(s): {', '.join(missing)}.",
        )

    # Reuse the connector's own store helper (username=client_id, secret=client_secret,
    # base_url=tenant_id) so the vault field mapping lives in ONE place.
    try:
        from discovery.ingest.azure_events import store_service_principal

        store_service_principal(
            org_id,
            client_id=client_id,
            client_secret=client_secret,
            tenant_id=tenant_id,
            credential_ref=CONNECTOR_ID,
        )
    except MissingSecretError:
        raise _vault_not_configured(CONNECTOR_ID)
    record["environment"] = env.name
    record["mode"] = mode


def _vault_store_static(
    org_id: str, connector_id: str, *, username: str, secret: str, base_url: str
) -> None:
    """Store a static credential, translating vault-config errors into HTTP errors."""
    from app.auth.vault import store_static_credential

    try:
        store_static_credential(
            org_id, connector_id, username=username, secret=secret, base_url=base_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MissingSecretError:
        raise _vault_not_configured(connector_id)


def _vault_not_configured(connector_id: str) -> HTTPException:
    logger.error(
        "Cannot store cloud credential for %s: CREDENTIAL_VAULT_KEY is not configured",
        connector_id,
    )
    return HTTPException(status_code=500, detail="Credential vault is not configured.")


def _test_aws(body: Dict[str, Any]) -> Dict[str, Any]:
    """Run the AWS auth probe against the submitted keys (never persisted)."""
    from discovery.ingest.aws_partitions import PARTITION_AWS

    access_key_id = str(body.get("access_key_id") or "").strip()
    secret_access_key = str(body.get("secret_access_key") or "")
    if not access_key_id or not secret_access_key:
        raise CloudProbeError(
            "missing_credentials",
            "Enter the access key id and secret access key to test the connection.",
        )
    partition = str(body.get("partition") or PARTITION_AWS).strip() or PARTITION_AWS
    region = (str(body.get("region") or "").strip() or None)
    return probe_aws_hub_credentials(
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        session_token=str(body.get("session_token") or ""),
        partition=partition,
        region=region,
    )


async def _test_azure(org_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Run the Azure service-principal probe against the submitted SP (never persisted)."""
    from discovery.ingest.azure_events import AzureServicePrincipal
    from discovery.ingest.azure_events_config import (
        AzureEventConfig,
        AzureEventConfigError,
        DEFAULT_MODE,
        resolve_environment,
    )

    tenant_id = str(body.get("tenant_id") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    client_secret = str(body.get("client_secret") or "")
    if not (tenant_id and client_id and client_secret):
        raise CloudProbeError(
            "missing_credentials",
            "Enter the tenant id, client id, and client secret to test the connection.",
        )
    try:
        env = resolve_environment(str(body.get("environment") or "").strip() or None)
        config = AzureEventConfig(
            environment=env,
            mode=str(body.get("mode") or DEFAULT_MODE).strip().lower() or DEFAULT_MODE,
            subscriptions=[],
        )
    except AzureEventConfigError as exc:
        raise CloudProbeError("invalid_environment", str(exc)) from exc
    sp = AzureServicePrincipal(
        client_id=client_id, client_secret=client_secret, tenant_id=tenant_id
    )
    return await probe_azure_service_principal(org_id, service_principal=sp, config=config)


def _pin_aws_scope(org_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + build one pinned AWS account scope (assume-role or direct keys)."""
    from discovery.ingest.aws_auth import (
        AWSAccountConfig,
        account_key_connector_id,
    )
    from discovery.ingest.aws_partitions import PartitionError

    account_id = str(body.get("account_id") or "").strip()
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id is required.")
    role_arn = str(body.get("role_arn") or "").strip() or None
    external_id = str(body.get("external_id") or "").strip() or None
    regions = tuple(str(r).strip() for r in (body.get("regions") or []) if str(r).strip())
    partition = str(body.get("partition") or "").strip()
    label = str(body.get("label") or "").strip() or None
    direct_akid = str(body.get("access_key_id") or "").strip()
    direct_secret = str(body.get("secret_access_key") or "")

    if not role_arn and not (direct_akid and direct_secret):
        raise HTTPException(
            status_code=400,
            detail="Provide a role_arn (assume-role) or direct access keys for the account.",
        )

    # Build (and validate) the account config first — partition/region contradictions
    # surface here as a 400 before any probe or vault write.
    try:
        account_config = AWSAccountConfig(
            account_id=account_id,
            role_arn=role_arn,
            external_id=external_id,
            regions=regions,
            partition=partition,
            label=label,
        )
    except PartitionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    probe_region = regions[0] if regions else None
    try:
        if role_arn:
            # Assume-role probe uses the org's VAULTED hub credential (AC3).
            probe_aws_assume_role(org_id, account_config)
        else:
            # Direct-keys account: validate the keys against AWS BEFORE persisting
            # them (validate-before-save, AC3), then vault them write-only.
            probe_aws_hub_credentials(
                access_key_id=direct_akid,
                secret_access_key=direct_secret,
                partition=account_config.partition,
                region=probe_region,
            )
            _vault_store_static(
                org_id,
                account_key_connector_id(account_id),
                username=direct_akid,
                secret=direct_secret,
                base_url=account_config.partition,
            )
    except CloudProbeError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": exc.reason, "message": exc.message}
        ) from exc

    return {
        "scope_id": account_id,
        "kind": _SCOPE_KIND[AWS_EVENTS],
        "label": label,
        "status": SCOPE_STATUS_PENDING,
        "pinned_at": _now_iso(),
        "role_arn": role_arn,
        "external_id_set": bool(external_id),
        "regions": list(regions),
        "partition": account_config.partition,
    }


def _pin_azure_scope(record: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    """Build one pinned Azure subscription scope (forward-only activation, AC4)."""
    subscription_id = str(body.get("subscription_id") or "").strip()
    if not subscription_id:
        raise HTTPException(status_code=400, detail="subscription_id is required.")
    label = str(body.get("label") or "").strip() or None
    return {
        "scope_id": subscription_id,
        "kind": _SCOPE_KIND[AZURE_EVENTS],
        "label": label,
        "status": SCOPE_STATUS_PENDING,
        "pinned_at": _now_iso(),
        "environment": record.get("environment"),
    }
