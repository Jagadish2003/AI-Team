"""
SF-2.3 — ServiceNow Ingestion Module

Offline mode: reads backend/discovery/ingest/fixtures/servicenow_sample.json
Live mode:    calls ServiceNow REST Table API

Environment variables for live mode (OAuth-only):
    SERVICENOW_URL    e.g. https://myinstance.service-now.com (captured at OAuth connect)
    SERVICENOW_TOKEN  OAuth Bearer token (hydrated from the credential vault)

Known fix applied (vs earlier stub):
    - total_count is fetched from the aggregate API, not hardcoded as 0
    - echo_score = match_count / total_count (not hardcoded as 0.0)

D7 signal produced:
    sn_echo_score = incidents referencing SF case IDs / total incidents in window
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import quote, urlsplit

from app.provenance import EvidencePointer
from discovery.signals.resolution_signature import (
    compute_incident_identity_signature,
    compute_resolution_signature,
)

from . import is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch, tombstone

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "servicenow_sample.json"
CMDB_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "servicenow_cmdb_sample.json"
SN_API_VERSION = "v1"
WINDOW_DAYS = 90

# MSP-B3 T1: deliberately small estate scope.  These are ServiceNow's common
# base classes for the six product categories promised by the MSP pack.  An org
# can replace this list through its own ServiceNow connector configuration.
DEFAULT_CMDB_CLASSES: Tuple[str, ...] = tuple(
    sorted(
        {
            "cmdb_ci_service_auto",   # application services
            "cmdb_ci_server",         # servers / compute
            "cmdb_ci_db_instance",    # databases
            "cmdb_ci_storage_device", # storage
            "cmdb_ci_netgear",        # network equipment
            "cmdb_ci_lb",             # load balancers
        }
    )
)
CMDB_CLASS_SCOPE_CONFIG_KEY = "cmdb_class_scope"
CMDB_RECORD_CAP = 10_000
CMDB_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "name",
    "sys_class_name",
    "operational_status",
    "assignment_group",
    "owned_by",
    "environment",
    "sys_updated_on",
)
CMDB_RELATIONSHIP_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "parent",
    "child",
    "type",
    "active",
    "sys_updated_on",
)
CMDB_DELETION_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "documentkey",
    "tablename",
    "sys_updated_on",
)
CMDB_CI_CHECKPOINT_ID = "servicenow:cmdb_ci"
CMDB_RELATIONSHIP_CHECKPOINT_ID = "servicenow:cmdb_rel_ci"
CMDB_CURSOR_FORMAT = "%Y-%m-%d %H:%M:%S"
INCIDENT_CI_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "number",
    "category",
    "state",
    "assigned_to",
    "assignment_group",
    "caller_id",
    "resolved_at",
    "sys_created_on",
    "sys_updated_on",
    "cmdb_ci",
)

# MSP-B4 T1 — resolution-depth fields read alongside the existing incident
# metrics on the SAME incremental incident query (no separate broad scan).
# These describe HOW an incident ended: the structured close/resolution fields
# and the timestamps needed to compute time-to-resolve. ``close_notes`` is the
# resolution-notes free text; it is read so the note can be stored AS EVIDENCE
# (an EvidencePointer back to the incident) and mined for DETERMINISTIC data
# only (a referenced runbook identifier). Its raw text never enters the
# detector-facing resolution payload — semantic matching of notes is MSP-B5.
INCIDENT_RESOLUTION_FIELDS: Tuple[str, ...] = (
    "close_code",
    "close_notes",
    "subcategory",
    "opened_at",
    "closed_at",
)

# MSP-B4 T4 — the only ServiceNow audit fields needed to reconstruct incident
# assignment-group movement.  The query is bounded to incident records already
# admitted by the current organization-scoped incident read.  Deliberately do
# not request ``user``, ``assigned_to``, or any other individual-level field.
ASSIGNMENT_HISTORY_TABLE = "sys_audit"
ASSIGNMENT_HISTORY_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "documentkey",
    "oldvalue",
    "newvalue",
    "sys_created_on",
)
ASSIGNMENT_HISTORY_RECORD_CAP = 10_000
_SN_SYS_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

# ServiceNow has no universally-standard "first assigned" timestamp; orgs that
# track it expose it under a custom column. Read it only when the deployment
# names the field, so first-assignment time is supported "where available"
# without inventing a value. Instance config, not a credential.
FIRST_ASSIGNED_FIELD_ENV = "SERVICENOW_FIRST_ASSIGNED_FIELD"

# Deterministic runbook / knowledge-article identifiers explicitly referenced in
# resolution notes. This is the ONLY thing B4 mines from note text — a stable,
# structured reference, never semantic content. Kept conservative and anchored
# so ordinary prose does not produce false hits.
# A RUNBOOK identifier must be a structured, separator-joined token
# (RUNBOOK-LOAN-CLOSE) — not the bare English word "runbook" that appears in
# prose ("resolved per runbook KB0010234", where KB0010234 is the real id).
_RUNBOOK_REF_RE = re.compile(
    r"\b(?:KB\d{4,}|RB\d{3,}|RUNBOOK[-_][A-Z0-9]+(?:[-_][A-Z0-9]+)*)\b",
    re.IGNORECASE,
)

# ServiceNow's canonical datetime format for resolution timestamps.
_SN_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
AFFECTED_CI_TASK_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "task",
    "ci_item",
    "sys_updated_on",
)
AFFECTED_CI_TASK_SOURCE_TYPE = "servicenow_task_ci"
CMDB_RELATIONSHIP_SOURCE_TYPE = "servicenow_cmdb_rel_ci"
CMDB_GRAPH_RELATIONSHIP_TYPES = frozenset(
    {"depends_on", "used_by", "runs_on", "connects_to"}
)
_CMDB_CLASS_RE = re.compile(r"^cmdb_ci_[a-z0-9_]+$")


@dataclass(frozen=True)
class ServiceNowConfigurationItem:
    """Stable, bounded internal representation of one ServiceNow CI.

    It intentionally contains no raw ServiceNow payload.  In particular,
    discovery credentials, connection attributes, and arbitrary CMDB columns
    cannot cross this boundary into later graph tasks.
    """

    sys_id: str
    name: str
    ci_class: str
    operational_status: Optional[str]
    assignment_group: Optional[str]
    owned_by: Optional[str]
    environment: Optional[str]
    updated_at: Optional[str]
    source_url: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CMDBRelationshipRule:
    """Map a ServiceNow descriptor to a graph verb and endpoint direction."""

    relationship_type: str
    reverse_endpoints: bool = False


# ServiceNow relationship types commonly expose both descriptors as
# ``parent descriptor::child descriptor``.  Keeping every string decision here
# makes direction reviewable and lets callers extend the map without changing
# the ingestion loop.  Reverse descriptors swap the ServiceNow endpoints; they
# must never silently acquire the direction of their forward counterpart.
DEFAULT_CMDB_RELATIONSHIP_RULES: Mapping[str, CMDBRelationshipRule] = (
    MappingProxyType(
        {
            "depends on": CMDBRelationshipRule("depends_on"),
            "depends upon": CMDBRelationshipRule("depends_on"),
            "uses": CMDBRelationshipRule("depends_on"),
            "used by": CMDBRelationshipRule("used_by", reverse_endpoints=True),
            "is used by": CMDBRelationshipRule("used_by", reverse_endpoints=True),
            "runs on": CMDBRelationshipRule("runs_on"),
            "hosted on": CMDBRelationshipRule("runs_on"),
            "runs": CMDBRelationshipRule("runs_on", reverse_endpoints=True),
            "hosts": CMDBRelationshipRule("runs_on", reverse_endpoints=True),
            "connects to": CMDBRelationshipRule("connects_to"),
            "connected to": CMDBRelationshipRule("connects_to"),
            "connected by": CMDBRelationshipRule(
                "connects_to", reverse_endpoints=True
            ),
            "depends on::used by": CMDBRelationshipRule("depends_on"),
            "depends upon::used by": CMDBRelationshipRule("depends_on"),
            "uses::used by": CMDBRelationshipRule("depends_on"),
            "used by::uses": CMDBRelationshipRule(
                "used_by", reverse_endpoints=True
            ),
            "runs on::runs": CMDBRelationshipRule("runs_on"),
            "hosted on::hosts": CMDBRelationshipRule("runs_on"),
            "runs::runs on": CMDBRelationshipRule(
                "runs_on", reverse_endpoints=True
            ),
            "hosts::hosted on": CMDBRelationshipRule(
                "runs_on", reverse_endpoints=True
            ),
            "connects to::connected by": CMDBRelationshipRule("connects_to"),
            "connected by::connects to": CMDBRelationshipRule(
                "connects_to", reverse_endpoints=True
            ),
        }
    )
)


@dataclass(frozen=True)
class IncidentResolution:
    """Structured, detector-facing record of HOW one incident was resolved.

    MSP-B4 T1. This is the stable payload downstream signature (T2) and detector
    (T3/T4) logic consume. Two deliberate boundaries:

    * **Groups, never people.** ``resolved_by_group`` is the incident's
      assignment group — a queue, not an individual. No person is carried here.
    * **Notes are evidence, not text.** The resolution note is represented by
      ``notes_evidence`` (an observed :class:`EvidencePointer` back to the
      incident, so the note stays reachable in the source system under its own
      access control) plus ``runbook_references`` (the only DETERMINISTIC thing
      mined from the note). The raw free-text never appears on this record —
      semantic matching of notes belongs to MSP-B5.

    Timestamps are normalised to stable internal fields so recurrence detection
    can compute median time-to-resolve consistently. ``time_to_resolve_seconds``
    measures from the first-assignment time when the deployment tracks it, else
    from creation, to the resolved (or, failing that, closed) time.
    """

    incident_sys_id: str
    is_resolved: bool
    close_code: Optional[str]
    resolution_category: Optional[str]
    resolution_subcategory: Optional[str]
    resolved_by_group: Optional[str]
    created_at: Optional[str]
    first_assigned_at: Optional[str]
    resolved_at: Optional[str]
    closed_at: Optional[str]
    time_to_resolve_seconds: Optional[int]
    has_resolution_notes: bool
    runbook_references: Tuple[str, ...]
    notes_evidence: Optional[Dict[str, Any]]
    evidence: Dict[str, Any]
    # MSP-B4 T2 — deterministic structured signatures (see
    # discovery/signals/resolution_signature.py). ``incident_identity_signature``
    # is WHAT KIND of incident this is (always present). ``resolution_signature``
    # is HOW it was resolved and is present only for resolved incidents.
    incident_identity_signature: Optional[str] = None
    resolution_signature: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["runbook_references"] = list(self.runbook_references)
        return data


@dataclass(frozen=True)
class ServiceNowCIRelationship:
    """Stable observed edge derived from one explicit ``cmdb_rel_ci`` row."""

    sys_id: str
    relationship_type: str
    source_ci_id: str
    target_ci_id: str
    servicenow_parent_id: str
    servicenow_child_id: str
    source_relationship_name: str
    source_type: str
    source_timestamp: Optional[str]
    source_url: Optional[str]
    origin: str = "observed"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Custom exception
# ─────────────────────────────────────────────────────────────────────────────


class ServiceNowIngestError(Exception):
    """Raised when live ServiceNow ingestion fails."""


class ServiceNowTableUnavailable(ServiceNowIngestError):
    """A ServiceNow table is not present or not readable on this instance.

    This is a DEPLOYMENT FACT, not an ingestion fault: the Security Operations
    tables (``sn_si_incident``, ``sn_vul_*``) only exist once the Security
    Incident Response / Vulnerability Response plugins are activated, and they
    are readable only by a role the integration user may not hold.  Neither is
    something a re-run fixes, so a stream that hits it degrades to
    ``status='unavailable'`` with a named reason instead of failing the
    ServiceNow stage every run (MSP-B11 — the module is an expansion, so an
    instance without it must stay a clean no-op).
    """

    def __init__(self, table: str, reason: str) -> None:
        super().__init__(f"ServiceNow table/{table} is unavailable: {reason}")
        self.table = table
        self.reason = reason


# ─────────────────────────────────────────────────────────────────────────────
# Offline loader
# ─────────────────────────────────────────────────────────────────────────────


def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise ServiceNowIngestError(f"ServiceNow fixture not found: {FIXTURE_PATH}")
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _load_cmdb_fixture() -> Dict[str, Any]:
    if not CMDB_FIXTURE_PATH.exists():
        raise ServiceNowIngestError(
            f"ServiceNow CMDB fixture not found: {CMDB_FIXTURE_PATH}"
        )
    with open(CMDB_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────────────────
# ServiceNow REST client
# ─────────────────────────────────────────────────────────────────────────────


class ServiceNowClient:
    """
    Minimal ServiceNow Table API client with pagination support.

    Auth (selected by whether ``username`` is present — R18-A3 outbound modes):

    * OAuth Bearer token — ``username`` empty (existing behaviour).
    * Basic (user + password) — ``username`` set; the static vault credential
      path (the outbound-only connect in a no-public-inbound deployment).
      ServiceNow's Table API supports Basic auth natively.
    """

    # HTTP statuses that mean "this table is not part of THIS instance's
    # surface" rather than "this request was wrong". ServiceNow answers a
    # request for a table that does not exist (an unactivated plugin) with
    # 400 + "Invalid table"; an existing table the caller has no role for
    # answers 401/403.
    _TABLE_MISSING_STATUSES = frozenset({400, 404})
    _TABLE_FORBIDDEN_STATUSES = frozenset({401, 403})
    _TABLE_MISSING_MARKERS = (
        "invalid table",
        "no such table",
        "table does not exist",
        "not a valid table",
    )

    def __init__(self, instance_url: str, token: str = "", username: str = ""):
        self.instance_url = instance_url.rstrip("/")
        self.token = token
        self.username = username
        self._session = None
        # Per-client memo so one run probes a given table at most once.
        self._table_availability: Dict[str, Tuple[bool, str]] = {}

    def _get_session(self):
        try:
            import requests
        except ImportError:
            raise ServiceNowIngestError(
                "requests library required for live mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                }
            )
            if self.token and self.username:
                # Static user/password credential → Basic auth.
                self._session.auth = (self.username, self.token)
            elif self.token:
                self._session.headers["Authorization"] = f"Bearer {self.token}"
            else:
                raise ServiceNowIngestError(
                    "Live mode requires a ServiceNow credential (OAuth Bearer "
                    "token or user/password) from the credential vault."
                )
        return self._session

    @staticmethod
    def _response_message(response: Any) -> str:
        """Extract ServiceNow's own error text from a failed response.

        ServiceNow answers a failed Table API call with
        ``{"error": {"message": ..., "detail": ...}, "status": "failure"}``.
        Only that short message is kept: the request URL is deliberately NOT
        carried into the exception, because it embeds the full encoded query and
        field projection and ends up rendered verbatim in run health.
        """
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 — a non-JSON error body is still an error
            body = None
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
                detail = str(error.get("detail") or "").strip()
                text = message or detail
                if text:
                    return text[:200]
            if isinstance(error, str) and error.strip():
                return error.strip()[:200]
        reason = str(getattr(response, "reason", "") or "").strip()
        return reason or "no error detail returned"

    def _translate_http_error(self, table: str, exc: Exception) -> ServiceNowIngestError:
        """Turn a transport failure into a compact, classified ingest error."""
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if response is None or status is None:
            # Timeout, DNS, connection reset — no response to read.
            return ServiceNowIngestError(
                f"ServiceNow table/{table} query failed: {type(exc).__name__}: {exc}"
            )
        message = self._response_message(response)
        lowered = message.casefold()
        if status in self._TABLE_MISSING_STATUSES and any(
            marker in lowered for marker in self._TABLE_MISSING_MARKERS
        ):
            return ServiceNowTableUnavailable(
                table,
                f"the table does not exist on this instance (HTTP {status}: "
                f"{message}). The plugin that provides it is not activated.",
            )
        if status in self._TABLE_FORBIDDEN_STATUSES:
            return ServiceNowTableUnavailable(
                table,
                f"the integration user cannot read this table (HTTP {status}: "
                f"{message}). Grant the role that covers it.",
            )
        return ServiceNowIngestError(
            f"ServiceNow table/{table} query failed: HTTP {status}: {message}"
        )

    def table_available(self, table: str) -> Tuple[bool, str]:
        """Probe once whether ``table`` is readable on this instance.

        Returns ``(available, reason)``; ``reason`` is empty when available.
        The probe is a single-row, ``sys_id``-only read — the cheapest request
        that still exercises table existence AND read access — and its outcome
        is memoized per client, so a run pays it at most once per table.

        Only a genuine unavailability answer is cached. A transient failure
        (timeout, 5xx) is re-raised so the caller treats it as the retryable
        error it is, rather than silently recording the table as missing.
        """
        cached = self._table_availability.get(table)
        if cached is not None:
            return cached
        try:
            self._request_page(
                table,
                {
                    "sysparm_fields": "sys_id",
                    "sysparm_limit": 1,
                    "sysparm_offset": 0,
                    "sysparm_display_value": "false",
                    "sysparm_exclude_reference_link": "true",
                },
            )
        except ServiceNowTableUnavailable as exc:
            outcome = (False, exc.reason)
            self._table_availability[table] = outcome
            return outcome
        outcome = (True, "")
        self._table_availability[table] = outcome
        return outcome

    def _request_page(self, table: str, query_params: Dict[str, Any]) -> List[Dict]:
        """Fetch one Table API page, translating transport failures compactly."""
        session = self._get_session()
        base_url = f"{self.instance_url}/api/now/table/{table}"
        try:
            resp = session.get(base_url, params=query_params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — every transport failure is translated
            raise self._translate_http_error(table, exc) from exc
        records = data.get("result", [])
        return records if isinstance(records, list) else []

    def table_query(
        self,
        table: str,
        params: Dict[str, Any],
        max_records: Optional[int] = 10000,
    ) -> List[Dict]:
        """
        Query a ServiceNow table with sysparm_offset pagination.

        max_records: safety cap — raises if exceeded.
        ServiceNow uses offset-based pagination (not cursor), so we step
        through sysparm_offset in sysparm_limit increments.
        """
        limit = min(params.get("sysparm_limit", 1000), 1000)  # SN max page = 1000
        offset = 0
        all_records: List[Dict] = []

        query_params = {**params, "sysparm_limit": limit}

        while True:
            query_params["sysparm_offset"] = offset
            records = self._request_page(table, query_params)
            if not records:
                break
            all_records.extend(records)
            if max_records is not None and len(all_records) >= max_records:
                raise ServiceNowIngestError(
                    f"ServiceNow table/{table} result exceeded {max_records} records. "
                    f"Narrow the query window."
                )
            if len(records) < limit:
                break  # last page
            offset += limit

        return all_records

    def aggregate_count(self, table: str, sysparm_query: str = "") -> int:
        """
        Use the ServiceNow Aggregate API to count records without fetching them.
        This is the correct way to get total_count — not a full table scan.

        API: GET /api/now/stats/{table}?sysparm_count=true&sysparm_query=...
        """
        session = self._get_session()
        url = f"{self.instance_url}/api/now/stats/{table}"
        params: Dict[str, Any] = {"sysparm_count": "true"}
        if sysparm_query:
            params["sysparm_query"] = sysparm_query

        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            # Response shape: {"result": {"stats": {"count": "500"}}}
            count_str = data.get("result", {}).get("stats", {}).get("count", "0")
            return int(count_str)
        except ServiceNowIngestError:
            raise
        except Exception as e:
            raise self._translate_http_error(table, e) from e


def _get_client() -> ServiceNowClient:
    # Credentials come from the per-run context (DB-sourced: vault Bearer token +
    # captured instance URL, isolated per org/run). With no per-run context
    # (CLI/standalone) or for a static user/password credential, the token
    # resolves per-org from the vault via the single credential path — never from
    # a process-global env credential (R17-D3 Addendum A, AC8/AC11). A static
    # credential also supplies its base_url; SERVICENOW_URL is instance config
    # (not a credential) and remains the env fallback.
    from . import get_live_connector, resolve_vault_connector

    cred = get_live_connector("servicenow") or resolve_vault_connector("servicenow")
    if cred:
        sn_url = (cred.get("url") or os.getenv("SERVICENOW_URL", "")).rstrip("/")
        token = cred.get("token") or ""
        # Present on a static credential only — selects Basic auth (user/password)
        # instead of an OAuth Bearer header.
        username = cred.get("username") or ""
    else:
        sn_url = os.getenv("SERVICENOW_URL", "").rstrip("/")
        token = ""
        username = ""

    if not sn_url:
        raise ServiceNowIngestError(
            "Live mode requires SERVICENOW_URL. "
            "Set INGEST_MODE=offline to run without credentials."
        )
    if not token:
        raise ServiceNowIngestError(
            "Live mode requires a ServiceNow credential (OAuth Bearer token or "
            "user/password) from the credential vault. Connect ServiceNow in the "
            "Integration Hub."
        )
    return ServiceNowClient(sn_url, token=token, username=username)


def _sn_scalar(value: Any) -> Any:
    """Return the display value from a ServiceNow ``display_value=all`` field."""
    if isinstance(value, dict):
        return (
            value.get("display_value")
            or value.get("displayValue")
            or value.get("value")
            or value.get("name")
        )
    return value


def normalize_cmdb_class_scope(value: Any) -> Tuple[str, ...]:
    """Validate and deterministically canonicalize a configured CI class list.

    ``None`` means the bounded product default.  A configured list replaces the
    default, which lets an organization either extend it (include extra valid
    classes) or narrow it (provide a subset, including an empty list to disable
    CMDB reads).  ServiceNow encoded-query control characters are impossible
    because only canonical ``cmdb_ci_*`` identifiers are accepted.
    """
    if value is None:
        return DEFAULT_CMDB_CLASSES
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("cmdb_class_scope must be a list of ServiceNow CI classes")
    if len(value) > 100:
        raise ValueError("cmdb_class_scope cannot contain more than 100 classes")

    classes = set()
    for raw_class in value:
        if not isinstance(raw_class, str):
            raise ValueError("every cmdb_class_scope entry must be a string")
        ci_class = raw_class.strip().lower()
        if not _CMDB_CLASS_RE.fullmatch(ci_class):
            raise ValueError(
                f"invalid ServiceNow CI class {raw_class!r}; expected cmdb_ci_*"
            )
        classes.add(ci_class)
    return tuple(sorted(classes))


def _load_org_cmdb_config(org_id: str) -> Optional[Any]:
    """Read only this org's ServiceNow override, never a shared catalog row."""
    try:
        from app import db

        connector = db.org_connector_get(org_id, "servicenow") or {}
    except Exception as exc:
        # Never turn an unavailable org-scoped config read into the broader
        # default scope.  That would be a fail-open data-boundary violation for
        # an organization that had deliberately narrowed its allowed classes.
        raise ServiceNowIngestError(
            f"ServiceNow CMDB class configuration unavailable for org {org_id!r}"
        ) from exc
    if connector.get("org_id") != org_id:
        return None
    return connector.get(CMDB_CLASS_SCOPE_CONFIG_KEY)


def resolve_cmdb_class_scope(org_id: Optional[str] = None) -> Tuple[str, ...]:
    """Resolve the effective class scope for one explicit or current org."""
    from . import get_ingest_org

    effective_org = org_id or get_ingest_org()
    return normalize_cmdb_class_scope(_load_org_cmdb_config(effective_org))


def _optional_sn_text(value: Any) -> Optional[str]:
    scalar = _sn_scalar(value)
    if scalar is None:
        return None
    text = str(scalar).strip()
    return text or None


def _sn_raw_scalar(value: Any) -> Any:
    """Return the RAW value from a ServiceNow ``display_value=all`` field.

    The mirror of :func:`_sn_scalar`.  Under ``sysparm_display_value=all`` every
    field arrives as ``{"value": <raw>, "display_value": <human>}``, and the two
    are NOT interchangeable:

    * ``sys_class_name`` — raw ``cmdb_ci_netgear`` vs display ``Network Gear``.
      The bounded class scope, the encoded server-side filter, and the graph all
      speak the raw identifier, so a display label must never be compared
      against them.
    * datetime fields (``sys_updated_on``, ``opened_at``, …) — the raw value is
      always ServiceNow's canonical ``YYYY-MM-DD HH:MM:SS`` UTC, while the
      display value is rendered in the *instance's* configured date format and
      the *user's* timezone. Checkpoint cursors, delta windows, and every
      time-to-resolve calculation depend on the canonical form.

    A plain scalar (offline fixtures, ``display_value=false`` reads) passes
    through unchanged.
    """
    if isinstance(value, dict):
        return value.get("value", value.get("display_value"))
    return value


def _optional_sn_raw_text(value: Any) -> Optional[str]:
    """``_optional_sn_text`` for fields whose RAW value is the meaningful one."""
    scalar = _sn_raw_scalar(value)
    if scalar is None:
        return None
    text = str(scalar).strip()
    return text or None


def _safe_servicenow_instance_url(value: Any) -> Optional[str]:
    """Return a credential-free ServiceNow instance origin for deep links."""
    if not value:
        return None
    try:
        parsed = urlsplit(str(value).strip())
    except ValueError:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    host = parsed.hostname
    if port:
        host = f"{host}:{port}"
    return f"{parsed.scheme}://{host}"


def _servicenow_record_url(
    instance_url: Optional[str], table: str, sys_id: str
) -> Optional[str]:
    """Build a resolvable, read-only classic UI link to one source record."""
    base = _safe_servicenow_instance_url(instance_url)
    if not base:
        return None
    return (
        f"{base}/nav_to.do?uri={quote(table, safe='')}.do%3Fsys_id%3D"
        f"{quote(sys_id, safe='')}"
    )


def _sn_reference_id(value: Any) -> Optional[str]:
    """Return the stable sys_id from a ServiceNow reference value.

    With ``sysparm_display_value=all`` a reference is an object whose display
    value is a human name and whose raw value is the sys_id.  Endpoint security
    and provenance must always use the raw value, never the display name.
    """
    if isinstance(value, dict):
        value = value.get("value") or value.get("sys_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalise_relationship_label(value: Any) -> str:
    """Canonicalize a ServiceNow relationship display label for rule lookup."""
    label = _optional_sn_text(value) or ""
    parts = []
    for part in label.split("::"):
        part = re.sub(r"[^a-z0-9]+", " ", part.casefold()).strip()
        if part:
            parts.append(part)
    return "::".join(parts)


def normalize_cmdb_relationship_type(
    value: Any,
    rules: Optional[Mapping[str, CMDBRelationshipRule]] = None,
) -> Optional[CMDBRelationshipRule]:
    """Resolve one explicit ServiceNow label to the bounded graph vocabulary.

    ``rules`` is an optional centralized extension point for deployments with
    additional known ServiceNow labels.  Unknown labels are excluded instead
    of guessed from CI names, classes, or any other record content.
    """
    label = _normalise_relationship_label(value)
    if not label:
        return None
    effective_rules = DEFAULT_CMDB_RELATIONSHIP_RULES if rules is None else rules
    rule = effective_rules.get(label)
    if rule is None and rules is not None:
        # Extension keys receive the same normalization as source labels.
        rule = next(
            (
                candidate
                for configured_label, candidate in rules.items()
                if _normalise_relationship_label(configured_label) == label
            ),
            None,
        )
    if rule is None:
        return None
    if rule.relationship_type not in CMDB_GRAPH_RELATIONSHIP_TYPES:
        raise ValueError(
            f"unsupported CMDB graph relationship type {rule.relationship_type!r}"
        )
    return rule


def _configuration_item_from_record(
    record: Dict[str, Any],
    allowed_classes: Tuple[str, ...],
    instance_url: Optional[str] = None,
) -> Optional[ServiceNowConfigurationItem]:
    sys_id = _optional_sn_text(record.get("sys_id"))
    # RAW value only: the bounded scope and the server-side ``sys_class_nameIN``
    # filter both speak canonical ``cmdb_ci_*`` identifiers, but under
    # ``display_value=all`` this field also carries a human label
    # ("Network Gear" for ``cmdb_ci_netgear``). Comparing the label against the
    # scope makes every in-scope CI look out of scope and fails the whole stream.
    ci_class = (_optional_sn_raw_text(record.get("sys_class_name")) or "").lower()
    if not sys_id or not ci_class:
        logger.warning("ServiceNow CMDB record missing sys_id or class; skipping")
        return None
    if ci_class not in allowed_classes:
        # The query is server-filtered.  Receiving anything else means the
        # upstream contract was violated; fail closed rather than leak it.
        raise ServiceNowIngestError(
            f"ServiceNow returned out-of-scope CI class {ci_class!r}"
        )

    return ServiceNowConfigurationItem(
        sys_id=sys_id,
        name=_optional_sn_text(record.get("name")) or sys_id,
        ci_class=ci_class,
        operational_status=_optional_sn_text(record.get("operational_status")),
        assignment_group=_optional_sn_text(record.get("assignment_group")),
        owned_by=_optional_sn_text(record.get("owned_by")),
        environment=_optional_sn_text(record.get("environment")),
        updated_at=_optional_sn_raw_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, "cmdb_ci", sys_id),
    )


def _parse_cmdb_timestamp(value: str) -> datetime:
    """Parse a ServiceNow UTC cursor without accepting query syntax."""
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, CMDB_CURSOR_FORMAT)
    except ValueError as exc:
        raise ServiceNowIngestError(
            f"invalid ServiceNow CMDB checkpoint {text!r}"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _validated_cmdb_cursor(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    parsed = _parse_cmdb_timestamp(value)
    return parsed.strftime(CMDB_CURSOR_FORMAT)


def _cmdb_watermark(clock: Optional[Callable[[], datetime]] = None) -> str:
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime(CMDB_CURSOR_FORMAT)


def _record_in_cursor_window(
    record: Any,
    updated_after: Optional[str],
    updated_through: Optional[str],
) -> bool:
    if not isinstance(record, dict):
        return False
    timestamp = _optional_sn_raw_text(record.get("sys_updated_on"))
    if not timestamp:
        return updated_after is None
    try:
        value = datetime.strptime(timestamp[:19].replace("T", " "), CMDB_CURSOR_FORMAT)
    except ValueError:
        logger.warning("ServiceNow CMDB record has invalid sys_updated_on; skipping")
        return False
    if updated_after and value <= _parse_cmdb_timestamp(updated_after).replace(tzinfo=None):
        return False
    if updated_through and value > _parse_cmdb_timestamp(updated_through).replace(tzinfo=None):
        return False
    return True


def _read_cmdb_configuration_items(
    class_scope: Tuple[str, ...],
    client: Optional[ServiceNowClient] = None,
    *,
    updated_after: Optional[str] = None,
    updated_through: Optional[str] = None,
) -> List[ServiceNowConfigurationItem]:
    """Read one already-resolved bounded ``cmdb_ci`` scope, without mutation.

    Live reads go through :class:`ServiceNowClient`, inheriting its vault-backed
    OAuth/Basic authentication, 30-second timeout, pagination, record cap, and
    error translation.  The encoded class filter and exact field projection are
    sent to ServiceNow so unrelated records and attributes are never fetched.
    """
    if not class_scope:
        return []

    if is_live():
        if client is None:
            client = _get_client()
        query_parts = [f"sys_class_nameIN{','.join(class_scope)}"]
        if updated_after:
            query_parts.append(f"sys_updated_on>{updated_after}")
        if updated_through:
            query_parts.append(f"sys_updated_on<={updated_through}")
        records = client.table_query(
            "cmdb_ci",
            {
                "sysparm_query": "^".join(query_parts) + (
                    "^ORDERBYsys_updated_on^ORDERBYsys_id"
                    if updated_after or updated_through
                    else "^ORDERBYsys_class_name^ORDERBYname^ORDERBYsys_id"
                ),
                "sysparm_fields": ",".join(CMDB_FIELDS),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
            max_records=CMDB_RECORD_CAP,
        )
        instance_url = getattr(client, "instance_url", None)
    else:
        fixture = _load_cmdb_fixture()
        records = fixture.get("result", [])
        instance_url = (fixture.get("_meta") or {}).get("instance_url")
        records = [
            record
            for record in records
            if _record_in_cursor_window(record, updated_after, updated_through)
        ]

    items: List[ServiceNowConfigurationItem] = []
    for record in records:
        if not isinstance(record, dict):
            logger.warning("ServiceNow CMDB returned a non-object record; skipping")
            continue
        item = _configuration_item_from_record(record, class_scope, instance_url)
        if item is not None:
            items.append(item)
    return sorted(
        items,
        key=lambda item: (item.ci_class, item.name.casefold(), item.sys_id),
    )


def _relationship_from_record(
    record: Dict[str, Any],
    admitted_ci_ids: frozenset[str],
    rules: Optional[Mapping[str, CMDBRelationshipRule]] = None,
    instance_url: Optional[str] = None,
) -> Optional[ServiceNowCIRelationship]:
    """Build one edge only when both raw endpoints are already admitted."""
    sys_id = _optional_sn_text(record.get("sys_id"))
    parent_id = _sn_reference_id(record.get("parent"))
    child_id = _sn_reference_id(record.get("child"))
    if not sys_id or not parent_id or not child_id:
        logger.warning("ServiceNow CMDB relationship missing identity; skipping")
        return None
    if parent_id not in admitted_ci_ids or child_id not in admitted_ci_ids:
        # The server query is class-bounded as a first barrier.  This exact-ID
        # check is the authoritative boundary and handles races or malformed
        # upstream data without allowing an edge to expose another CI.
        logger.warning(
            "ServiceNow CMDB relationship %s has an unadmitted endpoint; skipping",
            sys_id,
        )
        return None

    raw_name = _optional_sn_text(record.get("type")) or ""
    rule = normalize_cmdb_relationship_type(raw_name, rules)
    if rule is None:
        logger.info(
            "ServiceNow CMDB relationship %s has unsupported type %r; skipping",
            sys_id,
            raw_name,
        )
        return None

    source_id, target_id = parent_id, child_id
    if rule.reverse_endpoints:
        source_id, target_id = target_id, source_id

    return ServiceNowCIRelationship(
        sys_id=sys_id,
        relationship_type=rule.relationship_type,
        source_ci_id=source_id,
        target_ci_id=target_id,
        servicenow_parent_id=parent_id,
        servicenow_child_id=child_id,
        source_relationship_name=raw_name,
        source_type=CMDB_RELATIONSHIP_SOURCE_TYPE,
        source_timestamp=_optional_sn_raw_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, "cmdb_rel_ci", sys_id),
    )


def _read_cmdb_relationships(
    class_scope: Tuple[str, ...],
    configuration_items: List[ServiceNowConfigurationItem],
    client: Optional[ServiceNowClient] = None,
    rules: Optional[Mapping[str, CMDBRelationshipRule]] = None,
    *,
    updated_after: Optional[str] = None,
    updated_through: Optional[str] = None,
) -> List[ServiceNowCIRelationship]:
    """Read explicit ``cmdb_rel_ci`` edges within an admitted CI set.

    The Table API query applies the current org's class scope to both reference
    endpoints before ServiceNow returns any rows.  An exact admitted-sys-id
    membership check then fails closed against response drift and malformed
    records.  Only relationship identity, endpoints, type, and timestamp cross
    the connector boundary.
    """
    if not class_scope or not configuration_items:
        return []

    if is_live():
        if client is None:
            client = _get_client()
        class_csv = ",".join(class_scope)
        query_parts = [
            f"parent.sys_class_nameIN{class_csv}",
            f"child.sys_class_nameIN{class_csv}",
        ]
        if updated_after:
            query_parts.append(f"sys_updated_on>{updated_after}")
        if updated_through:
            query_parts.append(f"sys_updated_on<={updated_through}")
        records = client.table_query(
            "cmdb_rel_ci",
            {
                "sysparm_query": "^".join(query_parts) + (
                    "^ORDERBYsys_updated_on^ORDERBYsys_id"
                    if updated_after or updated_through
                    else "^ORDERBYsys_id"
                ),
                "sysparm_fields": ",".join(CMDB_RELATIONSHIP_FIELDS),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
            max_records=CMDB_RECORD_CAP,
        )
        instance_url = getattr(client, "instance_url", None)
    else:
        fixture = _load_cmdb_fixture()
        records = fixture.get("relationships", [])
        instance_url = (fixture.get("_meta") or {}).get("instance_url")
        records = [
            record
            for record in records
            if _record_in_cursor_window(record, updated_after, updated_through)
        ]

    admitted_ci_ids = frozenset(item.sys_id for item in configuration_items)
    relationships: List[ServiceNowCIRelationship] = []
    for record in records:
        if not isinstance(record, dict):
            logger.warning(
                "ServiceNow CMDB returned a non-object relationship; skipping"
            )
            continue
        relationship = _relationship_from_record(
            record,
            admitted_ci_ids,
            rules,
            instance_url,
        )
        if relationship is not None:
            relationships.append(relationship)
    return sorted(
        relationships,
        key=lambda edge: (
            edge.relationship_type,
            edge.source_ci_id,
            edge.target_ci_id,
            edge.sys_id,
        ),
    )


def get_cmdb_configuration_items(
    client: Optional[ServiceNowClient] = None,
) -> List[ServiceNowConfigurationItem]:
    """Read the bounded CMDB estate configured for the current organization."""
    return _read_cmdb_configuration_items(resolve_cmdb_class_scope(), client)


def get_cmdb_relationships(
    client: Optional[ServiceNowClient] = None,
    rules: Optional[Mapping[str, CMDBRelationshipRule]] = None,
) -> List[ServiceNowCIRelationship]:
    """Read bounded observed edges for the current organization."""
    class_scope = resolve_cmdb_class_scope()
    if is_live() and client is None and class_scope:
        client = _get_client()
    items = _read_cmdb_configuration_items(class_scope, client)
    return _read_cmdb_relationships(class_scope, items, client, rules)


def ingest_cmdb(
    client: Optional[ServiceNowClient] = None,
    *,
    class_scope: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    """Return the bounded CMDB payload consumed by graph persistence."""
    from . import get_ingest_org

    class_scope = (
        resolve_cmdb_class_scope()
        if class_scope is None
        else normalize_cmdb_class_scope(class_scope)
    )
    if is_live() and client is None and class_scope:
        client = _get_client()
    items = _read_cmdb_configuration_items(class_scope, client)
    relationships = _read_cmdb_relationships(class_scope, items, client)
    return {
        "org_id": get_ingest_org(),
        "class_scope": list(class_scope),
        "configuration_items": [item.as_dict() for item in items],
        "relationships": [relationship.as_dict() for relationship in relationships],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion functions
# ─────────────────────────────────────────────────────────────────────────────


class ServiceNowCMDBCIChangeIngestor(ChangeBasedIngestor):
    """Incremental, bounded ``cmdb_ci`` reader using ``sys_updated_on``."""

    connector_id = CMDB_CI_CHECKPOINT_ID
    reports_deletes = False

    def __init__(self, *, org_id: str, class_scope: Tuple[str, ...],
                 client: Optional[ServiceNowClient] = None,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.org_id = org_id
        self.class_scope = normalize_cmdb_class_scope(class_scope)
        self.client = client
        self.clock = clock

    def ingest_changes(self, org_id: str, since: Optional[Checkpoint]) -> Iterator[DeltaBatch]:
        if org_id != self.org_id:
            raise ServiceNowIngestError("CMDB CI stream organization mismatch")
        if since is not None and (since.org_id != org_id or since.connector_id != self.connector_id):
            raise ServiceNowIngestError("CMDB CI checkpoint scope mismatch")
        updated_after = _validated_cmdb_cursor(since.value if since else None)
        watermark = _cmdb_watermark(self.clock)
        items = _read_cmdb_configuration_items(
            self.class_scope, self.client,
            updated_after=updated_after, updated_through=watermark,
        )
        records: List[Dict[str, Any]] = []
        for item in items:
            record = item.as_dict()
            record.update(artifact_id=item.sys_id, change_kind=ChangeKind.UPDATED)
            records.append(record)
        yield DeltaBatch(records=records, next_checkpoint=watermark, is_complete=True)


class ServiceNowCMDBRelationshipChangeIngestor(ChangeBasedIngestor):
    """Incremental explicit ``cmdb_rel_ci`` stream with native tombstones."""

    connector_id = CMDB_RELATIONSHIP_CHECKPOINT_ID
    reports_deletes = True

    def __init__(self, *, org_id: str, class_scope: Tuple[str, ...],
                 admitted_ci_ids: frozenset[str],
                 known_relationship_ids: frozenset[str] = frozenset(),
                 client: Optional[ServiceNowClient] = None,
                 rules: Optional[Mapping[str, CMDBRelationshipRule]] = None,
                 clock: Optional[Callable[[], datetime]] = None) -> None:
        self.org_id = org_id
        self.class_scope = normalize_cmdb_class_scope(class_scope)
        self.admitted_ci_ids = admitted_ci_ids
        self.known_relationship_ids = known_relationship_ids
        self.client = client
        self.rules = rules
        self.clock = clock

    def _changed_records(self, updated_after: Optional[str], watermark: str) -> tuple[List[Dict[str, Any]], Optional[str]]:
        if not self.class_scope or not self.admitted_ci_ids:
            return [], None
        if is_live():
            client = self.client or _get_client()
            class_csv = ",".join(self.class_scope)
            query_parts = [f"parent.sys_class_nameIN{class_csv}", f"child.sys_class_nameIN{class_csv}"]
            if updated_after:
                query_parts.append(f"sys_updated_on>{updated_after}")
            query_parts.append(f"sys_updated_on<={watermark}")
            records = client.table_query(
                "cmdb_rel_ci",
                {
                    "sysparm_query": "^".join(query_parts) + "^ORDERBYsys_updated_on^ORDERBYsys_id",
                    "sysparm_fields": ",".join(CMDB_RELATIONSHIP_FIELDS),
                    "sysparm_display_value": "all",
                    "sysparm_exclude_reference_link": "true",
                },
                max_records=CMDB_RECORD_CAP,
            )
            return records, getattr(client, "instance_url", None)
        fixture = _load_cmdb_fixture()
        records = [record for record in fixture.get("relationships", [])
                   if _record_in_cursor_window(record, updated_after, watermark)]
        return records, (fixture.get("_meta") or {}).get("instance_url")

    def _deleted_records(self, updated_after: Optional[str], watermark: str) -> List[Dict[str, Any]]:
        if not updated_after or not self.known_relationship_ids:
            return []
        if is_live():
            client = self.client or _get_client()
            known_ids = ",".join(sorted(self.known_relationship_ids))
            return client.table_query(
                "sys_audit_delete",
                {
                    "sysparm_query": (
                        "tablename=cmdb_rel_ci^" f"documentkeyIN{known_ids}^"
                        f"sys_updated_on>{updated_after}^sys_updated_on<={watermark}^"
                        "ORDERBYsys_updated_on^ORDERBYsys_id"
                    ),
                    "sysparm_fields": ",".join(CMDB_DELETION_FIELDS),
                    "sysparm_display_value": "false",
                    "sysparm_exclude_reference_link": "true",
                },
                max_records=CMDB_RECORD_CAP,
            )
        fixture = _load_cmdb_fixture()
        return [record for record in fixture.get("relationship_deletions", [])
                if _record_in_cursor_window(record, updated_after, watermark)
                and _optional_sn_text(record.get("documentkey")) in self.known_relationship_ids]

    def ingest_changes(self, org_id: str, since: Optional[Checkpoint]) -> Iterator[DeltaBatch]:
        if org_id != self.org_id:
            raise ServiceNowIngestError("CMDB relationship stream organization mismatch")
        if since is not None and (since.org_id != org_id or since.connector_id != self.connector_id):
            raise ServiceNowIngestError("CMDB relationship checkpoint scope mismatch")
        updated_after = _validated_cmdb_cursor(since.value if since else None)
        watermark = _cmdb_watermark(self.clock)
        raw_records, instance_url = self._changed_records(updated_after, watermark)
        changed: Dict[str, Dict[str, Any]] = {}
        for record in raw_records:
            if not isinstance(record, dict):
                continue
            relationship_id = _optional_sn_text(record.get("sys_id"))
            if not relationship_id:
                continue
            active = str(_optional_sn_text(record.get("active")) or "true").casefold()
            if active in {"false", "0", "no", "retired", "inactive"}:
                changed[relationship_id] = tombstone(
                    relationship_id, sys_id=relationship_id,
                    source_type=CMDB_RELATIONSHIP_SOURCE_TYPE,
                    source_timestamp=_optional_sn_raw_text(record.get("sys_updated_on")),
                    source_url=_servicenow_record_url(instance_url, "cmdb_rel_ci", relationship_id),
                )
                continue
            relationship = _relationship_from_record(
                record, self.admitted_ci_ids, self.rules, instance_url
            )
            if relationship is not None:
                item = relationship.as_dict()
                item.update(artifact_id=relationship.sys_id, change_kind=ChangeKind.UPDATED)
                changed[relationship.sys_id] = item
        for deletion in self._deleted_records(updated_after, watermark):
            relationship_id = _optional_sn_text(deletion.get("documentkey"))
            if relationship_id in self.known_relationship_ids:
                changed[relationship_id] = tombstone(
                    relationship_id, sys_id=relationship_id,
                    source_type=CMDB_RELATIONSHIP_SOURCE_TYPE,
                    source_timestamp=_optional_sn_raw_text(deletion.get("sys_updated_on")),
                )
        yield DeltaBatch(
            records=[changed[key] for key in sorted(changed)],
            next_checkpoint=watermark,
            is_complete=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# MSP-B11 T1 — Security Incident Response (SIR) ingestion
#
# Extends the SAME production ServiceNow connector (auth, org scoping,
# pagination, timeout, record cap, error handling, and the sys_updated_on
# checkpoint discipline used by the CMDB streams above) to read the Security
# Operations SIR table `sn_si_incident` as WORKFLOW SIGNAL — the operational
# effort of security-incident triage and resolution, never the customer's
# security exposure ("workload, not weakness").
#
# The field scope IS the security posture: only workflow scalars and
# classifications are ever requested (sysparm_fields), and the internal
# representation is built solely from those named fields, so exploit detail,
# scanner payload, credentials, IOCs, and free-text notes cannot cross this
# boundary even if a response carries them (MSP-B11 AC2). Free-text/notes fields
# are deliberately NOT fetched here at all; the redaction-before-indexing path
# for security notes is a separate task (T5).
# ─────────────────────────────────────────────────────────────────────────────

SECOPS_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "servicenow_secops_sample.json"
# The ServiceNow field-level change-audit table backs the state/assignment
# history for BOTH the SIR (T1) and Vulnerability Response (T2) streams.
SECOPS_AUDIT_TABLE = "sys_audit"
SIR_TABLE = "sn_si_incident"
SIR_AUDIT_TABLE = SECOPS_AUDIT_TABLE  # retained name; shared audit table
SIR_CHECKPOINT_ID = "servicenow:sn_si_incident"
# SecOps transport reads are not truncated by the CMDB connector's unrelated
# 10k cap. The shared MSP-B7 ``SecOpsVolumeStream`` below owns the per-run
# processing budget, loud deferral, and safe resume checkpoint.
SIR_RECORD_CAP: Optional[int] = None
SIR_SOURCE_TYPE = "servicenow_security_incident"

# Workflow field scope — workload, not weakness.  Scalars, assignment details,
# timestamps, and workflow classifications only.
SIR_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "number",
    "state",
    "category",
    "subcategory",
    "severity",
    "priority",
    "assigned_to",
    "assignment_group",
    "opened_at",
    "sys_created_on",
    "sys_updated_on",
    "resolved_at",
    "closed_at",
    "close_code",
    "resolution_code",
)

# Notes are fetched through a second, isolated projection only after the
# corresponding workflow records have been admitted. They never enter
# ``ServiceNowSecurityIncident`` or the detector-visible SIR payload.
SIR_SECURITY_NOTE_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "number",
    "category",
    "sys_updated_on",
    "work_notes",
    "comments",
    "additional_comments",
    "close_notes",
)

# Field-scope guard (MSP-B11 AC2).  These are the sensitive-content fields a SIR
# record can carry — exploit detail, scanner output, credentials/IOCs, and
# free-text notes.  They are NEVER requested and, as belt-and-braces against a
# response that includes them anyway, never read into the emitted signal.  The
# internal dataclass is built only from SIR_FIELDS, so this set documents the
# exclusion and backs the data-layer test rather than performing the exclusion.
SIR_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "description",
        "short_description",
        "work_notes",
        "comments",
        "close_notes",
        "additional_comments",
        "exploit_details",
        "exploit_detail",
        "scanner_output",
        "scan_output",
        "attack_vector",
        "ioc",
        "iocs",
        "u_ioc",
        "u_iocs",
        "credentials",
        "u_credentials",
        "proof_of_concept",
        "u_poc",
        "payload",
        "cve_details",
        "vulnerability_detail",
    }
)

# Fields whose transitions are captured as workflow history so downstream
# detectors (MSP-B12) can identify repeated handoffs, triage loops, long
# resolution paths, and concentrated workloads.
SIR_HISTORY_FIELDS: Tuple[str, ...] = ("state", "assignment_group")
# Audit projection shared by every SecOps stream's history read.
SECOPS_AUDIT_FIELDS: Tuple[str, ...] = (
    "documentkey",
    "fieldname",
    "oldvalue",
    "newvalue",
    "sys_created_on",
    "sys_updated_on",
)
SIR_AUDIT_FIELDS: Tuple[str, ...] = SECOPS_AUDIT_FIELDS  # retained name


@dataclass(frozen=True)
class ServiceNowWorkflowTransition:
    """One audited workflow transition (a state change or an assignment handoff).

    Shared by the SIR (T1) and Vulnerability Response (T2) streams — the
    transition shape is identical: which field changed, from what to what, when.
    """

    field: str
    from_value: Optional[str]
    to_value: Optional[str]
    changed_at: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Backwards-compatible alias (the SIR dataclass and helpers reference this name).
ServiceNowSecurityIncidentTransition = ServiceNowWorkflowTransition


@dataclass(frozen=True)
class ServiceNowSecurityIncident:
    """Stable, bounded workflow representation of one SIR record.

    Contains ONLY workflow fields (states, assignment details, timestamps, and
    classifications) plus the state/assignment transition history.  No raw
    ServiceNow payload, exploit detail, scanner output, or free-text note can
    cross this boundary.  Every instance retains its organization, source record,
    source timestamp, and observed provenance.
    """

    sys_id: str
    number: Optional[str]
    state: Optional[str]
    category: Optional[str]
    subcategory: Optional[str]
    severity: Optional[str]
    priority: Optional[str]
    assigned_to: Optional[str]
    assignment_group: Optional[str]
    opened_at: Optional[str]
    created_at: Optional[str]
    resolved_at: Optional[str]
    closed_at: Optional[str]
    close_code: Optional[str]
    resolution_code: Optional[str]
    state_history: Tuple[ServiceNowSecurityIncidentTransition, ...]
    assignment_history: Tuple[ServiceNowSecurityIncidentTransition, ...]
    org_id: str
    source_timestamp: Optional[str]
    source_url: Optional[str]
    source_type: str = SIR_SOURCE_TYPE
    origin: str = "observed"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_secops_fixture() -> Dict[str, Any]:
    if not SECOPS_FIXTURE_PATH.exists():
        raise ServiceNowIngestError(
            f"ServiceNow SecOps fixture not found: {SECOPS_FIXTURE_PATH}"
        )
    with open(SECOPS_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _parse_secops_timestamp(value: str) -> datetime:
    """Parse a ServiceNow UTC cursor without accepting query syntax."""
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, CMDB_CURSOR_FORMAT)
    except ValueError as exc:
        raise ServiceNowIngestError(
            f"invalid ServiceNow SecOps checkpoint {text!r}"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _validated_secops_cursor(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _parse_secops_timestamp(value).strftime(CMDB_CURSOR_FORMAT)


def _read_workflow_field_history(
    table: str,
    admitted_ids: Tuple[str, ...],
    history_fields: Tuple[str, ...],
    *,
    client: Optional[ServiceNowClient] = None,
    audit_records: Optional[List[Dict[str, Any]]] = None,
    record_cap: Optional[int] = CMDB_RECORD_CAP,
) -> Dict[str, Dict[str, List[ServiceNowWorkflowTransition]]]:
    """Read audited transitions of ``history_fields`` for the supplied records.

    Shared by every SecOps workflow stream (SIR, Vulnerability Response).  The
    live query is bounded to the exact source sys_ids admitted by the current
    delta and to the named audited workflow fields — no free-text, finding, or
    scanner-payload field is ever read.  Returned transitions are checked against
    the admitted set, so a malformed response cannot attach another record's
    history.  In offline mode the caller supplies the fixture ``audit_records``.
    """
    grouped: Dict[str, Dict[str, List[ServiceNowWorkflowTransition]]] = {
        sys_id: {field: [] for field in history_fields} for sys_id in admitted_ids
    }
    if not admitted_ids:
        return grouped

    field_set = set(history_fields)
    if is_live():
        if client is None:
            client = _get_client()
        ordered_ids = sorted(admitted_ids)
        remaining = record_cap
        records: List[Dict[str, Any]] = []
        for offset in range(0, len(ordered_ids), 100):
            if remaining is not None and remaining <= 0:
                break
            batch = ordered_ids[offset : offset + 100]
            page = client.table_query(
                SECOPS_AUDIT_TABLE,
                {
                    "sysparm_query": (
                        f"tablename={table}^"
                        f"documentkeyIN{','.join(batch)}^"
                        f"fieldnameIN{','.join(history_fields)}^"
                        "ORDERBYsys_created_on^ORDERBYsys_id"
                    ),
                    "sysparm_fields": ",".join(SECOPS_AUDIT_FIELDS),
                    "sysparm_display_value": "all",
                    "sysparm_exclude_reference_link": "true",
                },
                max_records=remaining,
            )
            if remaining is not None:
                remaining -= len(page)
            records.extend(page)
    else:
        records = audit_records or []

    for record in records:
        if not isinstance(record, dict):
            continue
        document_key = _optional_sn_text(record.get("documentkey"))
        field_name = _optional_sn_text(record.get("fieldname"))
        if document_key not in grouped or field_name not in field_set:
            continue
        grouped[document_key][field_name].append(
            ServiceNowWorkflowTransition(
                field=field_name,
                from_value=_optional_sn_text(record.get("oldvalue")),
                to_value=_optional_sn_text(record.get("newvalue")),
                changed_at=_optional_sn_raw_text(record.get("sys_created_on")),
            )
        )

    for transitions in grouped.values():
        for entries in transitions.values():
            entries.sort(key=lambda t: (t.changed_at or "", t.from_value or "", t.to_value or ""))
    return grouped


def _read_security_incident_history(
    admitted_ids: Tuple[str, ...],
    client: Optional[ServiceNowClient] = None,
) -> Dict[str, Dict[str, List[ServiceNowWorkflowTransition]]]:
    """Read state and assignment transitions for the supplied SIR records."""
    audit_records = None
    if not is_live():
        audit_records = _load_secops_fixture().get("sn_si_incident_audit", [])
    return _read_workflow_field_history(
        SIR_TABLE,
        admitted_ids,
        SIR_HISTORY_FIELDS,
        client=client,
        audit_records=audit_records,
        record_cap=SIR_RECORD_CAP,
    )


def _security_incident_from_record(
    record: Dict[str, Any],
    org_id: str,
    history: Dict[str, List[ServiceNowSecurityIncidentTransition]],
    instance_url: Optional[str] = None,
) -> Optional[ServiceNowSecurityIncident]:
    """Build one bounded SIR workflow signal from a raw record.

    Only the named workflow fields are read; any other attribute on the record
    (exploit detail, scanner payload, notes) is ignored by construction.
    """
    sys_id = _optional_sn_text(record.get("sys_id"))
    if not sys_id:
        logger.warning("ServiceNow SIR record missing sys_id; skipping")
        return None
    return ServiceNowSecurityIncident(
        sys_id=sys_id,
        number=_optional_sn_text(record.get("number")),
        state=_optional_sn_text(record.get("state")),
        category=_optional_sn_text(record.get("category")),
        subcategory=_optional_sn_text(record.get("subcategory")),
        severity=_optional_sn_text(record.get("severity")),
        priority=_optional_sn_text(record.get("priority")),
        assigned_to=_optional_sn_text(record.get("assigned_to")),
        assignment_group=_optional_sn_text(record.get("assignment_group")),
        opened_at=_optional_sn_raw_text(record.get("opened_at")),
        created_at=_optional_sn_raw_text(record.get("sys_created_on")),
        resolved_at=_optional_sn_raw_text(record.get("resolved_at")),
        closed_at=_optional_sn_raw_text(record.get("closed_at")),
        close_code=_optional_sn_text(record.get("close_code")),
        resolution_code=_optional_sn_text(record.get("resolution_code")),
        state_history=tuple(history.get("state", [])),
        assignment_history=tuple(history.get("assignment_group", [])),
        org_id=org_id,
        source_timestamp=_optional_sn_raw_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, SIR_TABLE, sys_id),
    )


def _read_security_incidents(
    org_id: str,
    client: Optional[ServiceNowClient] = None,
    *,
    updated_after: Optional[str] = None,
    updated_through: Optional[str] = None,
) -> List[ServiceNowSecurityIncident]:
    """Read the bounded SIR workflow scope, incremental on ``sys_updated_on``.

    Live reads go through :class:`ServiceNowClient`, inheriting its vault-backed
    authentication, 30-second timeout, pagination, record cap, and error
    translation.  The exact workflow field projection is sent to ServiceNow so
    out-of-scope attributes are never fetched.
    """
    if is_live():
        if client is None:
            client = _get_client()
        query_parts: List[str] = []
        if updated_after:
            query_parts.append(f"sys_updated_on>{updated_after}")
        if updated_through:
            query_parts.append(f"sys_updated_on<={updated_through}")
        prefix = ("^".join(query_parts) + "^") if query_parts else ""
        records = client.table_query(
            SIR_TABLE,
            {
                "sysparm_query": prefix + "ORDERBYsys_updated_on^ORDERBYsys_id",
                "sysparm_fields": ",".join(SIR_FIELDS),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
            max_records=SIR_RECORD_CAP,
        )
        instance_url = getattr(client, "instance_url", None)
    else:
        fixture = _load_secops_fixture()
        instance_url = (fixture.get("_meta") or {}).get("instance_url")
        records = [
            record
            for record in fixture.get("sn_si_incident", [])
            if _record_in_cursor_window(record, updated_after, updated_through)
        ]

    admitted_ids = tuple(
        sys_id
        for sys_id in (
            _optional_sn_text(record.get("sys_id"))
            for record in records
            if isinstance(record, dict)
        )
        if sys_id
    )
    history = _read_security_incident_history(admitted_ids, client)

    incidents: List[ServiceNowSecurityIncident] = []
    for record in records:
        if not isinstance(record, dict):
            logger.warning("ServiceNow SIR returned a non-object record; skipping")
            continue
        sys_id = _optional_sn_text(record.get("sys_id"))
        incident = _security_incident_from_record(
            record,
            org_id,
            history.get(sys_id, {}) if sys_id else {},
            instance_url,
        )
        if incident is not None:
            incidents.append(incident)
    return sorted(
        incidents,
        key=lambda item: (item.source_timestamp or "", item.sys_id),
    )


def get_security_incidents(
    org_id: str,
    client: Optional[ServiceNowClient] = None,
) -> List[ServiceNowSecurityIncident]:
    """Read the full bounded SIR workflow scope for one organization."""
    if is_live() and client is None:
        client = _get_client()
    return _read_security_incidents(org_id, client)


def _read_security_incident_notes(
    incident_records: List[Dict[str, Any]],
    client: Optional[ServiceNowClient] = None,
) -> List[Dict[str, Any]]:
    """Fetch notes for the already-admitted SIR records through a separate scope.

    The normal SIR projection remains workflow-only. This second query is bounded
    to exact admitted ``sys_id`` values and returns note-bearing records solely to
    ``servicenow_security_notes_handoff``, where redaction happens before a
    retrieval artifact is constructed.
    """
    admitted = {
        str(record.get("sys_id")): record
        for record in incident_records
        if isinstance(record, dict) and record.get("sys_id")
    }
    if not admitted:
        return []

    if is_live():
        if client is None:
            client = _get_client()
        rows: List[Dict[str, Any]] = []
        ordered_ids = sorted(admitted)
        for offset in range(0, len(ordered_ids), 100):
            batch = ordered_ids[offset : offset + 100]
            rows.extend(
                client.table_query(
                    SIR_TABLE,
                    {
                        "sysparm_query": (
                            f"sys_idIN{','.join(batch)}^ORDERBYsys_updated_on^ORDERBYsys_id"
                        ),
                        "sysparm_fields": ",".join(SIR_SECURITY_NOTE_FIELDS),
                        "sysparm_display_value": "all",
                        "sysparm_exclude_reference_link": "true",
                    },
                    # Exact-id query: +1 makes hitting the cap a duplicate/contract
                    # error instead of rejecting a complete expected response.
                    max_records=len(batch) + 1,
                )
            )
        instance_url = getattr(client, "instance_url", None)
    else:
        fixture = _load_secops_fixture()
        instance_url = (fixture.get("_meta") or {}).get("instance_url")
        rows = [
            row
            for row in fixture.get(SIR_TABLE, [])
            if isinstance(row, dict) and str(row.get("sys_id") or "") in admitted
        ]

    notes: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sys_id = _optional_sn_text(row.get("sys_id"))
        if not sys_id or sys_id not in admitted:
            continue
        workflow = admitted[sys_id]
        note_record: Dict[str, Any] = {
            field: row.get(field)
            for field in SIR_SECURITY_NOTE_FIELDS
            if field in row
        }
        note_record.update(
            {
                "sys_id": sys_id,
                "number": note_record.get("number") or workflow.get("number"),
                "category": note_record.get("category") or workflow.get("category"),
                "source_timestamp": (
                    _optional_sn_text(note_record.get("sys_updated_on"))
                    or workflow.get("source_timestamp")
                ),
                "source_url": workflow.get("source_url")
                or _servicenow_record_url(instance_url, SIR_TABLE, sys_id),
            }
        )
        notes.append(note_record)
    notes.sort(
        key=lambda row: (
            str(row.get("source_timestamp") or ""),
            str(row.get("sys_id") or ""),
        )
    )
    return notes


def _budget_secops_delta(
    objects: List[Any],
    *,
    volume_stream: Any,
    table_family: str,
    org_id: str,
    updated_after: Optional[str],
    watermark: str,
) -> DeltaBatch:
    """Apply the shared B7 SecOps budget and return the checkpoint-safe delta."""
    candidates: List[Dict[str, Any]] = []
    for obj in objects:
        record = obj.as_dict()
        record.update(artifact_id=obj.sys_id, change_kind=ChangeKind.UPDATED)
        candidates.append(record)

    admissions = volume_stream.admit_records(
        candidates, table_family=table_family, org_id=org_id
    )
    records = [
        record
        for record, admission in zip(candidates, admissions)
        if admission.is_processed
    ]
    deferred = any(admission.is_deferred for admission in admissions)
    if not deferred:
        return DeltaBatch(
            records=records, next_checkpoint=watermark, is_complete=True
        )

    safe = volume_stream.measurements(org_id).safe_checkpoints.get(table_family)
    if safe:
        # Complete this budgeted window and resume strictly after the last whole
        # timestamp group that was processed.
        return DeltaBatch(records=records, next_checkpoint=safe, is_complete=True)

    # This table received no budget (an earlier SecOps family consumed it). Keep
    # the prior position intact. On a first run the empty value is rejected by
    # change_runner; on an incremental run ``is_complete=False`` prevents a write.
    return DeltaBatch(
        records=records,
        next_checkpoint=updated_after or "",
        is_complete=False,
    )


class ServiceNowSecurityIncidentChangeIngestor(ChangeBasedIngestor):
    """Incremental, bounded ``sn_si_incident`` reader using ``sys_updated_on``.

    Reuses the CMDB streams' checkpoint discipline (``change_runner`` writes the
    cursor only after a batch is fully processed; a failed or partial run leaves
    the prior checkpoint intact).

    ``reports_deletes = False``: SIR records are not hard-deleted in the normal
    Security Operations workflow — closure is a workflow STATE, captured as
    state history, not a source deletion — so there is no tombstone signal to
    emit and none is fabricated.
    """

    connector_id = SIR_CHECKPOINT_ID
    reports_deletes = False

    def __init__(
        self,
        *,
        org_id: str,
        client: Optional[ServiceNowClient] = None,
        clock: Optional[Callable[[], datetime]] = None,
        volume_stream: Optional[Any] = None,
    ) -> None:
        self.org_id = org_id
        self.client = client
        self.clock = clock
        if volume_stream is None:
            from discovery.signals.secops_volume import SecOpsVolumeStream

            volume_stream = SecOpsVolumeStream()
        self.volume_stream = volume_stream

    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        if org_id != self.org_id:
            raise ServiceNowIngestError("SIR stream organization mismatch")
        if since is not None and (
            since.org_id != org_id or since.connector_id != self.connector_id
        ):
            raise ServiceNowIngestError("SIR checkpoint scope mismatch")
        updated_after = _validated_secops_cursor(since.value if since else None)
        watermark = _cmdb_watermark(self.clock)
        incidents = _read_security_incidents(
            self.org_id,
            self.client,
            updated_after=updated_after,
            updated_through=watermark,
        )
        yield _budget_secops_delta(
            incidents,
            volume_stream=self.volume_stream,
            table_family=SIR_TABLE,
            org_id=org_id,
            updated_after=updated_after,
            watermark=watermark,
        )


# ─────────────────────────────────────────────────────────────────────────────
# MSP-B11 T2 — Vulnerability Response (VR) ingestion
#
# Extends the SAME connector rails to the Security Operations VR module family:
# vulnerable items, vulnerability groups, and remediation tasks — how security
# teams ORGANIZE and COMPLETE remediation work.  Every discipline from T1
# applies: workload not weakness, strict field projection at the request, the
# observed-provenance spine, and a per-table sys_updated_on checkpoint on the
# shared change-ingestion rails.
#
# AC6 (no host×vulnerability enumeration) is enforced structurally by the field
# scope: the specific vulnerability/CVE identity is NEVER projected — only the
# vulnerability CLASS/family and severity BAND are kept.  A vulnerable item
# therefore references its CI (for the T3 join and CI-class concentration) and a
# vulnerability CLASS, never a (host, CVE) pair.  Scanner payloads, exploit
# descriptions, proof-of-concept content, and raw findings are excluded at the
# request and can never enter AgentIQ, even temporarily (AC2).
# ─────────────────────────────────────────────────────────────────────────────

VR_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "servicenow_vr_sample.json"
VR_RECORD_CAP: Optional[int] = None
VR_HISTORY_FIELDS: Tuple[str, ...] = ("state", "assignment_group")

VR_VULN_ITEM_TABLE = "sn_vul_vulnerable_item"
VR_GROUP_TABLE = "sn_vul_vulnerability_group"
VR_REMEDIATION_TASK_TABLE = "sn_vul_remediation_task"

VR_VULN_ITEM_CHECKPOINT_ID = "servicenow:sn_vul_vulnerable_item"
VR_GROUP_CHECKPOINT_ID = "servicenow:sn_vul_vulnerability_group"
VR_REMEDIATION_TASK_CHECKPOINT_ID = "servicenow:sn_vul_remediation_task"

VR_VULN_ITEM_SOURCE_TYPE = "servicenow_vulnerable_item"
VR_GROUP_SOURCE_TYPE = "servicenow_vulnerability_group"
VR_REMEDIATION_TASK_SOURCE_TYPE = "servicenow_remediation_task"

# Workflow field scope — workload, not weakness.  Lifecycle state, operational
# classifications (family/severity band), assignment, timestamps, CI reference
# (opaque sys_id, for the T3 join), and deferral/exception classification only.
# The specific vulnerability/CVE identity is deliberately NOT projected (AC6).
VR_VULN_ITEM_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "number",
    "state",
    "substate",
    "vulnerability_class",
    "severity",
    "risk_rating",
    "cmdb_ci",
    "vulnerability_group",
    "assigned_to",
    "assignment_group",
    "first_found",
    "last_found",
    "opened_at",
    "sys_created_on",
    "sys_updated_on",
    "resolved_at",
    "closed_at",
    "close_code",
    "resolution_status",
    "deferral_category",
    "exception_category",
    "justification_class",
)
VR_GROUP_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "number",
    "name",
    "state",
    "vulnerability_class",
    "severity",
    "assigned_to",
    "assignment_group",
    "opened_at",
    "sys_created_on",
    "sys_updated_on",
    "resolved_at",
    "closed_at",
    "remediation_status",
)
VR_REMEDIATION_TASK_FIELDS: Tuple[str, ...] = (
    "sys_id",
    "number",
    "state",
    "assigned_to",
    "assignment_group",
    "vulnerability_group",
    "opened_at",
    "sys_created_on",
    "sys_updated_on",
    "due_date",
    "resolved_at",
    "closed_at",
    "close_code",
)

# Field-scope guard (MSP-B11 AC2 / AC6).  Scanner payloads, exploit detail,
# proof-of-concept content, raw technical findings, and the specific
# vulnerability/CVE identity that would enable host×CVE enumeration.  NEVER
# requested; the dataclasses are built only from the field scopes above, so
# these can never enter a normalized record even if a response carries them.
VR_FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        # Scanner / exploit / PoC / raw finding content.
        "description",
        "short_description",
        "proof",
        "proof_of_concept",
        "u_poc",
        "exploit",
        "exploit_code",
        "exploit_details",
        "exploit_detail",
        "scanner_output",
        "scan_output",
        "scanner_result",
        "raw_finding",
        "raw_findings",
        "finding_detail",
        "technical_details",
        "technical_detail",
        "solution",
        "remediation_steps",
        "references",
        "external_references",
        "payload",
        "attack_vector",
        "source_data",
        "raw",
        # Free-text notes / credentials / IOCs.
        "work_notes",
        "comments",
        "additional_comments",
        "close_notes",
        "credentials",
        "u_credentials",
        "ioc",
        "u_ioc",
        # Specific vulnerability / CVE identity (AC6: no host×vulnerability pairs).
        "vulnerability",
        "vulnerability_id",
        "cve",
        "cve_id",
        "cvss_vector",
        "third_party_id",
    }
)


@dataclass(frozen=True)
class ServiceNowVulnerableItem:
    """Bounded workflow representation of one ``sn_vul_vulnerable_item``.

    Lifecycle state + operational classification + assignment + timestamps +
    deferral/exception class, plus the CI reference (opaque sys_id) and the
    vulnerability CLASS — never the specific CVE, scanner payload, or finding
    detail.  Carries org, source record, source timestamp, and observed origin.
    """

    sys_id: str
    number: Optional[str]
    state: Optional[str]
    substate: Optional[str]
    vulnerability_class: Optional[str]
    severity: Optional[str]
    risk_rating: Optional[str]
    cmdb_ci: Optional[str]
    vulnerability_group: Optional[str]
    assigned_to: Optional[str]
    assignment_group: Optional[str]
    first_found: Optional[str]
    last_found: Optional[str]
    opened_at: Optional[str]
    created_at: Optional[str]
    resolved_at: Optional[str]
    closed_at: Optional[str]
    close_code: Optional[str]
    resolution_status: Optional[str]
    deferral_category: Optional[str]
    exception_category: Optional[str]
    justification_class: Optional[str]
    state_history: Tuple[ServiceNowWorkflowTransition, ...]
    assignment_history: Tuple[ServiceNowWorkflowTransition, ...]
    org_id: str
    source_timestamp: Optional[str]
    source_url: Optional[str]
    source_type: str = VR_VULN_ITEM_SOURCE_TYPE
    origin: str = "observed"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceNowVulnerabilityGroup:
    """Bounded workflow representation of one ``sn_vul_vulnerability_group``.

    A vulnerability group is a unit security teams actually work, so its
    group-level state, assignment, and timestamps measure effort concentration.
    """

    sys_id: str
    number: Optional[str]
    name: Optional[str]
    state: Optional[str]
    vulnerability_class: Optional[str]
    severity: Optional[str]
    assigned_to: Optional[str]
    assignment_group: Optional[str]
    opened_at: Optional[str]
    created_at: Optional[str]
    resolved_at: Optional[str]
    closed_at: Optional[str]
    remediation_status: Optional[str]
    state_history: Tuple[ServiceNowWorkflowTransition, ...]
    assignment_history: Tuple[ServiceNowWorkflowTransition, ...]
    org_id: str
    source_timestamp: Optional[str]
    source_url: Optional[str]
    source_type: str = VR_GROUP_SOURCE_TYPE
    origin: str = "observed"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceNowRemediationTask:
    """Bounded workflow representation of one ``sn_vul_remediation_task``."""

    sys_id: str
    number: Optional[str]
    state: Optional[str]
    assigned_to: Optional[str]
    assignment_group: Optional[str]
    vulnerability_group: Optional[str]
    opened_at: Optional[str]
    created_at: Optional[str]
    due_date: Optional[str]
    resolved_at: Optional[str]
    closed_at: Optional[str]
    close_code: Optional[str]
    state_history: Tuple[ServiceNowWorkflowTransition, ...]
    assignment_history: Tuple[ServiceNowWorkflowTransition, ...]
    org_id: str
    source_timestamp: Optional[str]
    source_url: Optional[str]
    source_type: str = VR_REMEDIATION_TASK_SOURCE_TYPE
    origin: str = "observed"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_vr_fixture() -> Dict[str, Any]:
    if not VR_FIXTURE_PATH.exists():
        raise ServiceNowIngestError(
            f"ServiceNow Vulnerability Response fixture not found: {VR_FIXTURE_PATH}"
        )
    with open(VR_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _vulnerable_item_from_record(
    record: Dict[str, Any],
    org_id: str,
    history: Dict[str, List[ServiceNowWorkflowTransition]],
    instance_url: Optional[str] = None,
) -> Optional[ServiceNowVulnerableItem]:
    sys_id = _optional_sn_text(record.get("sys_id"))
    if not sys_id:
        logger.warning("ServiceNow VR vulnerable item missing sys_id; skipping")
        return None
    return ServiceNowVulnerableItem(
        sys_id=sys_id,
        number=_optional_sn_text(record.get("number")),
        state=_optional_sn_text(record.get("state")),
        substate=_optional_sn_text(record.get("substate")),
        vulnerability_class=_optional_sn_text(record.get("vulnerability_class")),
        severity=_optional_sn_text(record.get("severity")),
        risk_rating=_optional_sn_text(record.get("risk_rating")),
        cmdb_ci=_sn_reference_id(record.get("cmdb_ci")),
        vulnerability_group=_sn_reference_id(record.get("vulnerability_group")),
        assigned_to=_optional_sn_text(record.get("assigned_to")),
        assignment_group=_optional_sn_text(record.get("assignment_group")),
        first_found=_optional_sn_raw_text(record.get("first_found")),
        last_found=_optional_sn_raw_text(record.get("last_found")),
        opened_at=_optional_sn_raw_text(record.get("opened_at")),
        created_at=_optional_sn_raw_text(record.get("sys_created_on")),
        resolved_at=_optional_sn_raw_text(record.get("resolved_at")),
        closed_at=_optional_sn_raw_text(record.get("closed_at")),
        close_code=_optional_sn_text(record.get("close_code")),
        resolution_status=_optional_sn_text(record.get("resolution_status")),
        deferral_category=_optional_sn_text(record.get("deferral_category")),
        exception_category=_optional_sn_text(record.get("exception_category")),
        justification_class=_optional_sn_text(record.get("justification_class")),
        state_history=tuple(history.get("state", [])),
        assignment_history=tuple(history.get("assignment_group", [])),
        org_id=org_id,
        source_timestamp=_optional_sn_raw_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, VR_VULN_ITEM_TABLE, sys_id),
    )


def _vulnerability_group_from_record(
    record: Dict[str, Any],
    org_id: str,
    history: Dict[str, List[ServiceNowWorkflowTransition]],
    instance_url: Optional[str] = None,
) -> Optional[ServiceNowVulnerabilityGroup]:
    sys_id = _optional_sn_text(record.get("sys_id"))
    if not sys_id:
        logger.warning("ServiceNow VR group missing sys_id; skipping")
        return None
    return ServiceNowVulnerabilityGroup(
        sys_id=sys_id,
        number=_optional_sn_text(record.get("number")),
        name=_optional_sn_text(record.get("name")),
        state=_optional_sn_text(record.get("state")),
        vulnerability_class=_optional_sn_text(record.get("vulnerability_class")),
        severity=_optional_sn_text(record.get("severity")),
        assigned_to=_optional_sn_text(record.get("assigned_to")),
        assignment_group=_optional_sn_text(record.get("assignment_group")),
        opened_at=_optional_sn_raw_text(record.get("opened_at")),
        created_at=_optional_sn_raw_text(record.get("sys_created_on")),
        resolved_at=_optional_sn_raw_text(record.get("resolved_at")),
        closed_at=_optional_sn_raw_text(record.get("closed_at")),
        remediation_status=_optional_sn_text(record.get("remediation_status")),
        state_history=tuple(history.get("state", [])),
        assignment_history=tuple(history.get("assignment_group", [])),
        org_id=org_id,
        source_timestamp=_optional_sn_raw_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, VR_GROUP_TABLE, sys_id),
    )


def _remediation_task_from_record(
    record: Dict[str, Any],
    org_id: str,
    history: Dict[str, List[ServiceNowWorkflowTransition]],
    instance_url: Optional[str] = None,
) -> Optional[ServiceNowRemediationTask]:
    sys_id = _optional_sn_text(record.get("sys_id"))
    if not sys_id:
        logger.warning("ServiceNow VR remediation task missing sys_id; skipping")
        return None
    return ServiceNowRemediationTask(
        sys_id=sys_id,
        number=_optional_sn_text(record.get("number")),
        state=_optional_sn_text(record.get("state")),
        assigned_to=_optional_sn_text(record.get("assigned_to")),
        assignment_group=_optional_sn_text(record.get("assignment_group")),
        vulnerability_group=_sn_reference_id(record.get("vulnerability_group")),
        opened_at=_optional_sn_raw_text(record.get("opened_at")),
        created_at=_optional_sn_raw_text(record.get("sys_created_on")),
        due_date=_optional_sn_raw_text(record.get("due_date")),
        resolved_at=_optional_sn_raw_text(record.get("resolved_at")),
        closed_at=_optional_sn_raw_text(record.get("closed_at")),
        close_code=_optional_sn_text(record.get("close_code")),
        state_history=tuple(history.get("state", [])),
        assignment_history=tuple(history.get("assignment_group", [])),
        org_id=org_id,
        source_timestamp=_optional_sn_raw_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, VR_REMEDIATION_TASK_TABLE, sys_id),
    )


def _read_vr_stream(
    *,
    table: str,
    fields: Tuple[str, ...],
    fixture_key: str,
    audit_key: str,
    builder: Callable[..., Any],
    org_id: str,
    client: Optional[ServiceNowClient] = None,
    updated_after: Optional[str] = None,
    updated_through: Optional[str] = None,
) -> List[Any]:
    """Read one bounded VR table's delta, incremental on ``sys_updated_on``.

    Live reads go through :class:`ServiceNowClient` (vault auth, 30-second
    timeout, offset pagination, record cap, error translation).  The exact
    workflow field projection is sent to ServiceNow so scanner payloads, exploit
    detail, and finding content are never fetched.  Deterministic ordering
    (``sys_updated_on``, ``sys_id``) over the full paginated result means a large
    scan-cycle delta is read completely, never skipped.
    """
    if is_live():
        if client is None:
            client = _get_client()
        query_parts: List[str] = []
        if updated_after:
            query_parts.append(f"sys_updated_on>{updated_after}")
        if updated_through:
            query_parts.append(f"sys_updated_on<={updated_through}")
        prefix = ("^".join(query_parts) + "^") if query_parts else ""
        records = client.table_query(
            table,
            {
                "sysparm_query": prefix + "ORDERBYsys_updated_on^ORDERBYsys_id",
                "sysparm_fields": ",".join(fields),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
            max_records=VR_RECORD_CAP,
        )
        instance_url = getattr(client, "instance_url", None)
        audit_records: Optional[List[Dict[str, Any]]] = None
    else:
        fixture = _load_vr_fixture()
        instance_url = (fixture.get("_meta") or {}).get("instance_url")
        records = [
            record
            for record in fixture.get(fixture_key, [])
            if _record_in_cursor_window(record, updated_after, updated_through)
        ]
        audit_records = fixture.get(audit_key, [])

    admitted_ids = tuple(
        sys_id
        for sys_id in (
            _optional_sn_text(record.get("sys_id"))
            for record in records
            if isinstance(record, dict)
        )
        if sys_id
    )
    history = _read_workflow_field_history(
        table,
        admitted_ids,
        VR_HISTORY_FIELDS,
        client=client,
        audit_records=audit_records,
        record_cap=VR_RECORD_CAP,
    )

    out: List[Any] = []
    for record in records:
        if not isinstance(record, dict):
            logger.warning("ServiceNow VR %s returned a non-object record; skipping", table)
            continue
        sys_id = _optional_sn_text(record.get("sys_id"))
        obj = builder(record, org_id, history.get(sys_id, {}) if sys_id else {}, instance_url)
        if obj is not None:
            out.append(obj)
    return sorted(out, key=lambda item: (item.source_timestamp or "", item.sys_id))


def _read_vulnerable_items(
    org_id: str,
    client: Optional[ServiceNowClient] = None,
    *,
    updated_after: Optional[str] = None,
    updated_through: Optional[str] = None,
) -> List[ServiceNowVulnerableItem]:
    return _read_vr_stream(
        table=VR_VULN_ITEM_TABLE,
        fields=VR_VULN_ITEM_FIELDS,
        fixture_key="sn_vul_vulnerable_item",
        audit_key="sn_vul_vulnerable_item_audit",
        builder=_vulnerable_item_from_record,
        org_id=org_id,
        client=client,
        updated_after=updated_after,
        updated_through=updated_through,
    )


def _read_vulnerability_groups(
    org_id: str,
    client: Optional[ServiceNowClient] = None,
    *,
    updated_after: Optional[str] = None,
    updated_through: Optional[str] = None,
) -> List[ServiceNowVulnerabilityGroup]:
    return _read_vr_stream(
        table=VR_GROUP_TABLE,
        fields=VR_GROUP_FIELDS,
        fixture_key="sn_vul_vulnerability_group",
        audit_key="sn_vul_vulnerability_group_audit",
        builder=_vulnerability_group_from_record,
        org_id=org_id,
        client=client,
        updated_after=updated_after,
        updated_through=updated_through,
    )


def _read_remediation_tasks(
    org_id: str,
    client: Optional[ServiceNowClient] = None,
    *,
    updated_after: Optional[str] = None,
    updated_through: Optional[str] = None,
) -> List[ServiceNowRemediationTask]:
    return _read_vr_stream(
        table=VR_REMEDIATION_TASK_TABLE,
        fields=VR_REMEDIATION_TASK_FIELDS,
        fixture_key="sn_vul_remediation_task",
        audit_key="sn_vul_remediation_task_audit",
        builder=_remediation_task_from_record,
        org_id=org_id,
        client=client,
        updated_after=updated_after,
        updated_through=updated_through,
    )


class _ServiceNowVRStreamIngestor(ChangeBasedIngestor):
    """Shared incremental base for the three VR table streams.

    Each concrete stream sets its own ``connector_id`` (its independent,
    org-scoped ``sys_updated_on`` checkpoint) and supplies ``_read``.  The
    checkpoint discipline is inherited from ``change_runner``: the cursor
    advances only after the batch is fully processed; a failed or partial run
    leaves the prior checkpoint intact.

    ``reports_deletes = False``: VR records are not hard-deleted in the normal
    workflow — closure/deferral/exception are workflow STATES, captured as state
    history — so no tombstone is fabricated.
    """

    reports_deletes = False
    _stream_label = "VR"
    _table_family = ""

    def __init__(
        self,
        *,
        org_id: str,
        client: Optional[ServiceNowClient] = None,
        clock: Optional[Callable[[], datetime]] = None,
        volume_stream: Optional[Any] = None,
    ) -> None:
        self.org_id = org_id
        self.client = client
        self.clock = clock
        if volume_stream is None:
            from discovery.signals.secops_volume import SecOpsVolumeStream

            volume_stream = SecOpsVolumeStream()
        self.volume_stream = volume_stream

    def _read(self, updated_after: Optional[str], updated_through: Optional[str]) -> List[Any]:
        raise NotImplementedError

    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        if org_id != self.org_id:
            raise ServiceNowIngestError(f"{self._stream_label} stream organization mismatch")
        if since is not None and (
            since.org_id != org_id or since.connector_id != self.connector_id
        ):
            raise ServiceNowIngestError(f"{self._stream_label} checkpoint scope mismatch")
        updated_after = _validated_secops_cursor(since.value if since else None)
        watermark = _cmdb_watermark(self.clock)
        yield _budget_secops_delta(
            self._read(updated_after, watermark),
            volume_stream=self.volume_stream,
            table_family=self._table_family,
            org_id=org_id,
            updated_after=updated_after,
            watermark=watermark,
        )


class ServiceNowVulnerableItemChangeIngestor(_ServiceNowVRStreamIngestor):
    """Incremental ``sn_vul_vulnerable_item`` stream."""

    connector_id = VR_VULN_ITEM_CHECKPOINT_ID
    _stream_label = "vulnerable item"
    _table_family = VR_VULN_ITEM_TABLE

    def _read(self, updated_after, updated_through):
        return _read_vulnerable_items(
            self.org_id, self.client,
            updated_after=updated_after, updated_through=updated_through,
        )


class ServiceNowVulnerabilityGroupChangeIngestor(_ServiceNowVRStreamIngestor):
    """Incremental ``sn_vul_vulnerability_group`` stream."""

    connector_id = VR_GROUP_CHECKPOINT_ID
    _stream_label = "vulnerability group"
    _table_family = VR_GROUP_TABLE

    def _read(self, updated_after, updated_through):
        return _read_vulnerability_groups(
            self.org_id, self.client,
            updated_after=updated_after, updated_through=updated_through,
        )


class ServiceNowRemediationTaskChangeIngestor(_ServiceNowVRStreamIngestor):
    """Incremental ``sn_vul_remediation_task`` stream."""

    connector_id = VR_REMEDIATION_TASK_CHECKPOINT_ID
    _stream_label = "remediation task"
    _table_family = VR_REMEDIATION_TASK_TABLE

    def _read(self, updated_after, updated_through):
        return _read_remediation_tasks(
            self.org_id, self.client,
            updated_after=updated_after, updated_through=updated_through,
        )


def summarize_vulnerability_workload(
    vulnerable_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Aggregate VR effort by CLASSIFICATION only — never host×vulnerability.

    Rolls vulnerable-item workflow records up by vulnerability class, severity
    band, and assignment group so effort concentration is measured at the level
    humans experience it (MSP-B11 AC6).  Deliberately produces no per-host,
    per-vulnerability breakdown; combined with the CVE-free field scope, no
    emitted signal can enumerate a (host, vulnerability) pair.
    """
    by_class: Dict[str, int] = {}
    by_severity: Dict[str, int] = {}
    by_assignment_group: Dict[str, int] = {}
    for item in vulnerable_items:
        cls = item.get("vulnerability_class") or "unclassified"
        by_class[cls] = by_class.get(cls, 0) + 1
        sev = item.get("severity") or "unclassified"
        by_severity[sev] = by_severity.get(sev, 0) + 1
        grp = item.get("assignment_group") or "unassigned"
        by_assignment_group[grp] = by_assignment_group.get(grp, 0) + 1
    return {
        "total_items": len(vulnerable_items),
        "by_vulnerability_class": by_class,
        "by_severity_band": by_severity,
        "by_assignment_group": by_assignment_group,
    }


def _ingestion_result_payload(result: Any) -> Dict[str, Any]:
    """Shape one stream's outcome, separating UNAVAILABLE from FAILED.

    A :class:`ServiceNowTableUnavailable` is a deployment fact (plugin not
    activated / role not granted), not an ingestion failure, so it is reported
    as ``status='unavailable'`` with a named reason and NO ``error``. The
    checkpoint still does not advance, so the stream picks up cleanly from its
    last known-good position the moment the table becomes readable.
    """
    error = result.error
    if isinstance(error, ServiceNowTableUnavailable):
        return {
            "connector_id": result.connector_id,
            "records": result.records,
            "complete": result.complete,
            "checkpoint_advanced": result.checkpoint_advanced,
            "checkpoint": result.new_checkpoint.value if result.new_checkpoint else None,
            "error": None,
            "status": "unavailable",
            "table": error.table,
            "unavailable_reason": error.reason,
        }
    return {
        "connector_id": result.connector_id,
        "records": result.records,
        "complete": result.complete,
        "checkpoint_advanced": result.checkpoint_advanced,
        "checkpoint": result.new_checkpoint.value if result.new_checkpoint else None,
        "error": str(error) if error else None,
        "status": "error" if error else "ok",
    }


def _unavailable_stream_payload(
    connector_id: str, table: str, reason: str
) -> Dict[str, Any]:
    """Payload for a stream that was never driven because its table is absent."""
    return {
        "connector_id": connector_id,
        "records": 0,
        "complete": False,
        "checkpoint_advanced": False,
        "checkpoint": None,
        "error": None,
        "status": "unavailable",
        "table": table,
        "unavailable_reason": reason,
    }


def _secops_table_available(
    client: Optional[ServiceNowClient], table: str
) -> Tuple[bool, str]:
    """Pre-flight one SecOps table so an absent module is a clean no-op.

    Offline runs always read the fixture, so availability is never in question
    there. Live runs probe once per table per client (memoized): an instance
    without the Security Operations plugins would otherwise fail all four
    streams on every run, flooding run health with a transport error for
    something no re-run can fix.
    """
    if not is_live() or client is None:
        return True, ""
    return client.table_available(table)


def ingest_cmdb_changes(
    *, org_id: str, run_id: str,
    client: Optional[ServiceNowClient] = None,
    class_scope: Optional[Tuple[str, ...]] = None,
    clock: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    """Apply both CMDB streams; persist a cursor only after graph processing."""
    from app.entity_extractor import _extract_servicenow_cmdb_entities
    from app.entity_resolution import list_source_entities
    from app.relationship_mapper import (
        apply_servicenow_cmdb_relationship_delta,
        list_servicenow_relationship_source_ids,
    )
    from discovery.ingest.change_runner import ingest_with_checkpoint

    scope = resolve_cmdb_class_scope(org_id) if class_scope is None else normalize_cmdb_class_scope(class_scope)
    payload: Dict[str, Any] = {
        "org_id": org_id,
        "class_scope": list(scope),
        "configuration_items": [],
        "relationships": [],
        "relationship_deletions": [],
        "streams": {},
    }
    if not scope:
        return payload
    if is_live() and client is None:
        client = _get_client()
    # Both tables share one closed upper bound. A relationship written after
    # that boundary cannot advance past a CI endpoint that this run has not read.
    fixed_now = (clock or (lambda: datetime.now(timezone.utc)))()

    def stream_clock() -> datetime:
        return fixed_now

    def process_ci(batch: DeltaBatch) -> None:
        items = [
            {key: value for key, value in record.items()
             if key not in {"artifact_id", "change_kind"}}
            for record in batch.records
        ]
        _extract_servicenow_cmdb_entities(
            org_id=org_id,
            run_id=run_id,
            cmdb_data={
                "org_id": org_id,
                "class_scope": list(scope),
                "configuration_items": items,
            },
        )
        payload["configuration_items"].extend(items)

    ci_result = ingest_with_checkpoint(
        ServiceNowCMDBCIChangeIngestor(
            org_id=org_id, class_scope=scope, client=client, clock=stream_clock
        ),
        org_id,
        process_batch=process_ci,
    )
    payload["streams"]["cmdb_ci"] = _ingestion_result_payload(ci_result)
    if not ci_result.ok:
        return payload

    all_entities = list_source_entities(
        org_id=org_id, entity_type="system", source_system="servicenow"
    )
    scoped_entities = [
        entity for entity in all_entities
        if str((entity.metadata or {}).get("ci_class") or "").casefold() in scope
    ]
    admitted_ids = frozenset(
        str(entity.source_record_id) for entity in scoped_entities
        if entity.source_record_id
    )

    def process_relationships(batch: DeltaBatch) -> None:
        apply_servicenow_cmdb_relationship_delta(
            org_id=org_id,
            run_id=run_id,
            relationships=batch.records,
            entities=scoped_entities,
        )
        for record in batch.records:
            if record.get("change_kind") == ChangeKind.DELETED:
                payload["relationship_deletions"].append(dict(record))
            else:
                payload["relationships"].append({
                    key: value for key, value in record.items()
                    if key not in {"artifact_id", "change_kind"}
                })

    relationship_result = ingest_with_checkpoint(
        ServiceNowCMDBRelationshipChangeIngestor(
            org_id=org_id,
            class_scope=scope,
            admitted_ci_ids=admitted_ids,
            known_relationship_ids=frozenset(
                list_servicenow_relationship_source_ids(org_id)
            ),
            client=client,
            clock=stream_clock,
        ),
        org_id,
        process_batch=process_relationships,
    )
    payload["streams"]["cmdb_rel_ci"] = _ingestion_result_payload(relationship_result)
    return payload


def ingest_sir_changes(
    *,
    org_id: str,
    run_id: str,
    client: Optional[ServiceNowClient] = None,
    clock: Optional[Callable[[], datetime]] = None,
    read_checkpoint: Optional[Callable[[str, str], Optional[Checkpoint]]] = None,
    save_checkpoint: Optional[Callable[[Checkpoint], None]] = None,
    volume_stream: Optional[Any] = None,
    handoff_security_notes: bool = False,
    security_note_ingest_fn: Optional[Callable[..., Any]] = None,
    security_note_record_event_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Apply the SIR workflow-signal stream on the shared change-ingestion rails.

    Drives :class:`ServiceNowSecurityIncidentChangeIngestor` through
    :func:`ingest_with_checkpoint`, so the ``sys_updated_on`` checkpoint advances
    only after the delta is fully processed and a failed or partial run leaves
    the last valid checkpoint intact.  Returns the bounded workflow signal for
    this run (consumed by MSP-B12); ``run_id`` is retained for traceability and
    the later CI-join task (T3).  ``read_checkpoint`` / ``save_checkpoint`` default
    to the AT-378 repository and are injectable for tests.
    """
    from discovery.ingest.change_runner import ingest_with_checkpoint
    from discovery.ingest import checkpoint_repository as _repo

    if is_live() and client is None:
        client = _get_client()

    collected: List[Dict[str, Any]] = []
    note_handoff: Dict[str, Any] = {
        "enabled": bool(handoff_security_notes),
        "notes_seen": 0,
        "artifacts_handed_off": 0,
        "redacted": 0,
        "secrets_redacted": 0,
        "pattern_types": [],
    }

    ingestor = ServiceNowSecurityIncidentChangeIngestor(
        org_id=org_id,
        client=client,
        clock=clock,
        volume_stream=volume_stream,
    )

    available, unavailable_reason = _secops_table_available(client, SIR_TABLE)
    if not available:
        logger.info(
            "ServiceNow SIR: %s not ingested — %s", SIR_TABLE, unavailable_reason
        )
        return {
            "org_id": org_id,
            "run_id": run_id,
            "security_incidents": [],
            "security_note_handoff": note_handoff,
            "volume": ingestor.volume_stream.measurements(org_id).to_dict(),
            "streams": {
                SIR_TABLE: _unavailable_stream_payload(
                    SIR_CHECKPOINT_ID, SIR_TABLE, unavailable_reason
                )
            },
        }

    def process(batch: DeltaBatch) -> None:
        normalized = [
            {
                key: value
                for key, value in record.items()
                if key not in {"artifact_id", "change_kind"}
            }
            for record in batch.records
        ]
        if handoff_security_notes and normalized:
            from discovery.ingest.servicenow_security_notes_handoff import (
                ingest_security_notes,
            )

            notes = _read_security_incident_notes(normalized, client)
            outcome = ingest_security_notes(
                org_id,
                notes,
                ingest_fn=security_note_ingest_fn,
                record_event_fn=security_note_record_event_fn,
            )
            note_handoff.update(
                {
                    "notes_seen": outcome.notes_seen,
                    "artifacts_handed_off": outcome.artifacts_handed_off,
                    "redacted": outcome.redacted,
                    "secrets_redacted": outcome.secrets_redacted,
                    "pattern_types": sorted(set(outcome.pattern_types)),
                }
            )
            substrate_result = outcome.ingest_result
            failed = int(getattr(substrate_result, "artifacts_failed", 0) or 0)
            if failed:
                raise ServiceNowIngestError(
                    f"security-note retrieval handoff failed for {failed} artifact(s)"
                )
        collected.extend(normalized)

    result = ingest_with_checkpoint(
        ingestor,
        org_id,
        process_batch=process,
        read_checkpoint=read_checkpoint or _repo.read_checkpoint,
        save_checkpoint=save_checkpoint or _repo.save_checkpoint,
    )
    return {
        "org_id": org_id,
        "run_id": run_id,
        "security_incidents": collected,
        "security_note_handoff": note_handoff,
        "volume": ingestor.volume_stream.measurements(org_id).to_dict(),
        "streams": {"sn_si_incident": _ingestion_result_payload(result)},
    }


def ingest_vr_changes(
    *,
    org_id: str,
    run_id: str,
    client: Optional[ServiceNowClient] = None,
    clock: Optional[Callable[[], datetime]] = None,
    read_checkpoint: Optional[Callable[[str, str], Optional[Checkpoint]]] = None,
    save_checkpoint: Optional[Callable[[Checkpoint], None]] = None,
    volume_stream: Optional[Any] = None,
) -> Dict[str, Any]:
    """Apply the three Vulnerability Response streams on the shared rails.

    Each VR table (vulnerable items, vulnerability groups, remediation tasks)
    runs through :func:`ingest_with_checkpoint` under its OWN org-scoped
    ``sys_updated_on`` checkpoint, so a large scan-cycle delta on one table
    neither blocks nor skips the others, and each cursor advances only after its
    batch is fully processed.  Returns the bounded workflow signal plus a
    classification-only workload summary (MSP-B11 AC6); ``run_id`` is retained
    for traceability and the T3 CI join.  ``read_checkpoint`` / ``save_checkpoint``
    default to the AT-378 repository and are injectable for tests.
    """
    from discovery.ingest.change_runner import ingest_with_checkpoint
    from discovery.ingest import checkpoint_repository as _repo

    if is_live() and client is None:
        client = _get_client()
    if volume_stream is None:
        from discovery.signals.secops_volume import SecOpsVolumeStream

        volume_stream = SecOpsVolumeStream()

    read_cp = read_checkpoint or _repo.read_checkpoint
    save_cp = save_checkpoint or _repo.save_checkpoint

    streams: Dict[str, Any] = {}
    collected: Dict[str, List[Dict[str, Any]]] = {
        "vulnerable_items": [],
        "vulnerability_groups": [],
        "remediation_tasks": [],
    }

    plan = (
        ("sn_vul_vulnerable_item", "vulnerable_items", ServiceNowVulnerableItemChangeIngestor),
        ("sn_vul_vulnerability_group", "vulnerability_groups", ServiceNowVulnerabilityGroupChangeIngestor),
        ("sn_vul_remediation_task", "remediation_tasks", ServiceNowRemediationTaskChangeIngestor),
    )
    for stream_key, collection_key, ingestor_cls in plan:
        bucket = collected[collection_key]

        available, unavailable_reason = _secops_table_available(client, stream_key)
        if not available:
            logger.info(
                "ServiceNow VR: %s not ingested — %s", stream_key, unavailable_reason
            )
            streams[stream_key] = _unavailable_stream_payload(
                ingestor_cls.connector_id, stream_key, unavailable_reason
            )
            continue

        def process(batch: DeltaBatch, _bucket=bucket) -> None:
            for record in batch.records:
                _bucket.append(
                    {
                        key: value
                        for key, value in record.items()
                        if key not in {"artifact_id", "change_kind"}
                    }
                )

        result = ingest_with_checkpoint(
            ingestor_cls(
                org_id=org_id,
                client=client,
                clock=clock,
                volume_stream=volume_stream,
            ),
            org_id,
            process_batch=process,
            read_checkpoint=read_cp,
            save_checkpoint=save_cp,
        )
        streams[stream_key] = _ingestion_result_payload(result)

    return {
        "org_id": org_id,
        "run_id": run_id,
        "vulnerable_items": collected["vulnerable_items"],
        "vulnerability_groups": collected["vulnerability_groups"],
        "remediation_tasks": collected["remediation_tasks"],
        "workload_summary": summarize_vulnerability_workload(collected["vulnerable_items"]),
        "volume": volume_stream.measurements(org_id).to_dict(),
        "streams": streams,
    }
def _assignment_group_name(value: Any) -> Optional[str]:
    """Return a ServiceNow assignment group's display name (a queue, not a person).

    Handles both the ``display_value=all`` reference object and a plain scalar
    (the offline fixture shape). Returns ``None`` when no group is set.
    """
    if isinstance(value, dict):
        name = (
            value.get("display_value")
            or value.get("displayName")
            or value.get("name")
            or value.get("value")
        )
    else:
        name = value
    if name is None:
        return None
    text = str(name).strip()
    return text or None


def _parse_sn_datetime(value: Optional[str]) -> Optional[datetime]:
    """Parse a ServiceNow datetime string; ``None`` when absent or malformed."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Tolerate an ISO 'T' separator and a trailing fractional/zone suffix.
    text = text[:19].replace("T", " ")
    try:
        return datetime.strptime(text, _SN_DATETIME_FORMAT)
    except ValueError:
        logger.warning("ServiceNow resolution timestamp %r is unparseable; ignoring", value)
        return None


def _time_to_resolve_seconds(
    *, created_at: Optional[str], first_assigned_at: Optional[str],
    resolved_at: Optional[str], closed_at: Optional[str],
) -> Optional[int]:
    """Compute a stable time-to-resolve in whole seconds.

    Start = first-assignment time when the deployment tracks it, else creation.
    End = resolved time, else closed time. Returns ``None`` unless both ends
    parse and the interval is non-negative — a negative interval means dirty
    source data and must not become a spurious TTR.
    """
    start = _parse_sn_datetime(first_assigned_at) or _parse_sn_datetime(created_at)
    end = _parse_sn_datetime(resolved_at) or _parse_sn_datetime(closed_at)
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return None
    return int(seconds)


def _extract_runbook_references(notes_text: Optional[str]) -> Tuple[str, ...]:
    """Extract deterministic runbook / KB identifiers explicitly cited in notes.

    The ONLY thing B4 reads out of resolution-note text: a structured reference,
    never semantic content. Returns a deterministic (uppercased, de-duplicated,
    sorted) tuple.
    """
    if not notes_text:
        return ()
    found = {match.group(0).upper() for match in _RUNBOOK_REF_RE.finditer(str(notes_text))}
    return tuple(sorted(found))


def _build_incident_resolution(
    source: Mapping[str, Any],
    *,
    incident_sys_id: Optional[str],
    incident_number: Optional[str],
    instance_url: Optional[str],
    first_assigned_field: str = "",
    ci_class: Optional[str] = None,
) -> Dict[str, Any]:
    """Build one incident's structured resolution payload from a source record.

    ``source`` is either a raw ServiceNow record (``display_value=all`` objects)
    or an offline fixture incident (plain scalars); ``_sn_scalar`` /
    ``_optional_sn_text`` handle both. Read-only: it inspects fields only.

    ``ci_class`` is the CMDB class of the incident's CI when a B3 CMDB join has
    resolved it; ``None`` (the T2 default) yields an unlocated CI component that
    the signatures still compute over deterministically (MSP-B4 AC5).
    """
    close_code = _optional_sn_text(source.get("close_code"))
    resolution_category = _optional_sn_text(source.get("category"))
    resolution_subcategory = _optional_sn_text(source.get("subcategory"))
    resolved_by_group = _assignment_group_name(source.get("assignment_group"))

    created_at = _optional_sn_text(source.get("opened_at")) or _optional_sn_text(
        source.get("sys_created_on")
    )
    first_assigned_at = (
        _optional_sn_text(source.get(first_assigned_field))
        if first_assigned_field
        else None
    )
    resolved_at = _optional_sn_text(source.get("resolved_at"))
    closed_at = _optional_sn_text(source.get("closed_at"))

    notes_text = _optional_sn_text(source.get("close_notes"))
    runbook_references = _extract_runbook_references(notes_text)
    is_resolved = bool(resolved_at or closed_at or close_code)

    # MSP-B4 T2 — deterministic structured signatures. The CI is keyed on its
    # STABLE sys_id (never the drifting display name); short-description tokens
    # are structural, never fuzzy. ``ci_class`` sharpens the CI component when a
    # B3 join supplies it (T5); until then the stable CI id is used, and an
    # incident with no CI stays unlocated but still signable (AC5).
    ci_id = _sn_reference_id(source.get("cmdb_ci"))
    short_description = _optional_sn_text(source.get("short_description"))
    incident_identity_signature = compute_incident_identity_signature(
        category=resolution_category,
        short_description=short_description,
        ci_class=ci_class,
        ci_id=ci_id,
    )
    # HOW it was resolved is only meaningful once the incident has been resolved.
    resolution_signature = (
        compute_resolution_signature(
            category=resolution_category,
            close_code=close_code,
            resolved_by_group=resolved_by_group,
            ci_class=ci_class,
            ci_id=ci_id,
        )
        if is_resolved
        else None
    )

    # The evidence artifact is the incident itself. Prefer the stable sys_id;
    # fall back to the incident number when a fixture omits it.
    artifact = incident_sys_id or incident_number or ""
    artifact_type = "record_id" if incident_sys_id else None
    pointer_timestamp = resolved_at or closed_at or created_at
    evidence = EvidencePointer.observed(
        source_system="servicenow",
        source_artifact=artifact,
        source_timestamp=pointer_timestamp,
        source_artifact_type=artifact_type,
    ).to_dict()

    resolution = IncidentResolution(
        incident_sys_id=incident_sys_id or "",
        is_resolved=is_resolved,
        close_code=close_code,
        resolution_category=resolution_category,
        resolution_subcategory=resolution_subcategory,
        resolved_by_group=resolved_by_group,
        created_at=created_at,
        first_assigned_at=first_assigned_at,
        resolved_at=resolved_at,
        closed_at=closed_at,
        time_to_resolve_seconds=_time_to_resolve_seconds(
            created_at=created_at,
            first_assigned_at=first_assigned_at,
            resolved_at=resolved_at,
            closed_at=closed_at,
        ),
        has_resolution_notes=bool(notes_text),
        runbook_references=runbook_references,
        # The note is stored AS EVIDENCE (a pointer back to the incident), never
        # as text on this payload. Present only when a note exists.
        notes_evidence=dict(evidence) if notes_text else None,
        evidence=evidence,
        incident_identity_signature=incident_identity_signature,
        resolution_signature=resolution_signature,
    )
    return resolution.as_dict()


def _fixture_instance_url(fixture: Dict[str, Any]) -> Optional[str]:
    """Derive a credential-free instance origin from the incident fixture meta."""
    meta = fixture.get("_meta") or {}
    instance_url = meta.get("instance_url")
    if not instance_url:
        instance = meta.get("instance")
        if instance:
            instance_url = f"https://{instance}"
    return _safe_servicenow_instance_url(instance_url) if instance_url else None


def _attach_incident_resolutions(
    incident_metrics: Dict[str, Any],
    instance_url: Optional[str],
    *,
    first_assigned_field: str = "",
) -> None:
    """Attach a ``resolution`` block to each incident in place (offline path).

    Idempotent: an incident that already carries a resolution block is left
    untouched, so re-attaching is safe.
    """
    for incident in incident_metrics.get("incidents") or []:
        if not isinstance(incident, dict) or "resolution" in incident:
            continue
        incident["resolution"] = _build_incident_resolution(
            incident,
            incident_sys_id=_optional_sn_text(incident.get("sys_id")),
            incident_number=_optional_sn_text(incident.get("number"))
            or _optional_sn_text(incident.get("id")),
            instance_url=instance_url,
            first_assigned_field=first_assigned_field,
        )


def get_incident_metrics(client: Optional[ServiceNowClient] = None) -> Dict[str, Any]:
    """
    Pull ServiceNow incident volume, category breakdown, and avg resolution time.

    Live API calls:
        -- Total incidents in 90-day window (aggregate — no record fetch)
        GET /api/now/stats/incident
            ?sysparm_count=true
            &sysparm_query=sys_created_on>=javascript:gs.daysAgo(90)

        -- Incidents by category with avg resolution
        GET /api/now/table/incident
            ?sysparm_query=sys_created_on>=javascript:gs.daysAgo(90)
            &sysparm_fields=sys_id,number,category,state,assigned_to,
                            assignment_group,caller_id,resolved_at,sys_created_on,
                            sys_updated_on,cmdb_ci
            &sysparm_limit=1000

    Returns: incident_metrics dict matching servicenow_sample.json shape
    """
    if not is_live():
        fixture = _load_fixture()
        metrics = fixture["incident_metrics"]
        _attach_incident_resolutions(
            metrics,
            _fixture_instance_url(fixture),
            first_assigned_field=os.getenv(FIRST_ASSIGNED_FIELD_ENV, "").strip(),
        )
        return metrics

    window_query = f"sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"

    # Total count — aggregate API, not a full record fetch
    total = client.aggregate_count("incident", window_query)

    # Incident details for category breakdown
    fields = list(INCIDENT_CI_FIELDS)
    # MSP-B4 T1: read resolution-depth fields on this SAME query (no new scan).
    for resolution_field in INCIDENT_RESOLUTION_FIELDS:
        if resolution_field not in fields:
            fields.append(resolution_field)
    # MSP-B4 T2: short_description feeds the incident-identity signature only —
    # its normalised token set, never its raw text — so it is read but not
    # surfaced on the incident payload.
    if "short_description" not in fields:
        fields.append("short_description")
    first_assigned_field = os.getenv(FIRST_ASSIGNED_FIELD_ENV, "").strip()
    if first_assigned_field and first_assigned_field not in fields:
        fields.append(first_assigned_field)
    escalation_field = os.getenv("SERVICENOW_ESCALATION_FIELD", "").strip()
    if escalation_field:
        fields.append(escalation_field)

    records = client.table_query(
        "incident",
        {
            "sysparm_query": window_query,
            "sysparm_fields": ",".join(fields),
            "sysparm_display_value": "all",
        },
    )

    # Category breakdown
    category_map: Dict[str, Dict] = {}
    total_resolution_hours = 0.0
    resolved_count = 0

    for r in records:
        cat = _sn_scalar(r.get("category")) or "uncategorized"
        if cat not in category_map:
            category_map[cat] = {
                "category": cat,
                "volume": 0,
                "avg_resolution_hours": 0.0,
            }
        category_map[cat]["volume"] += 1

        resolved_at = _sn_scalar(r.get("resolved_at")) or ""
        created = _sn_scalar(r.get("sys_created_on")) or ""
        if resolved_at and created:
            try:
                from datetime import datetime

                # SN format: "2026-01-15 09:22:31"
                fmt = "%Y-%m-%d %H:%M:%S"
                delta = datetime.strptime(resolved_at, fmt) - datetime.strptime(
                    created, fmt
                )
                hours = delta.total_seconds() / 3600
                category_map[cat]["avg_resolution_hours"] = round(
                    (category_map[cat].get("_total_hours", 0.0) + hours)
                    / (category_map[cat]["volume"]),
                    1,
                )
                category_map[cat]["_total_hours"] = (
                    category_map[cat].get("_total_hours", 0.0) + hours
                )
                total_resolution_hours += hours
                resolved_count += 1
            except Exception:
                pass

    avg_resolution = (
        round(total_resolution_hours / resolved_count, 1) if resolved_count > 0 else 0.0
    )

    # Remove internal tracking key
    for v in category_map.values():
        v.pop("_total_hours", None)

    incidents: List[Dict[str, Any]] = []
    for record in records:
        incident_sys_id = _sn_reference_id(record.get("sys_id"))
        incident = {
            "id": incident_sys_id or _sn_scalar(record.get("number")),
            "sys_id": incident_sys_id,
            "number": _sn_scalar(record.get("number")) or _sn_scalar(record.get("sys_id")),
            "category": _sn_scalar(record.get("category")),
            "state": _sn_scalar(record.get("state")),
            "assigned_to": record.get("assigned_to"),
            "assignment_group": record.get("assignment_group"),
            "caller_id": record.get("caller_id"),
            # Preserve the raw explicit reference.  Resolution deliberately uses
            # only its stable value/sys_id, never the display name.
            "cmdb_ci": record.get("cmdb_ci"),
            "source_timestamp": _optional_sn_raw_text(record.get("sys_updated_on")),
            "source_url": (
                _servicenow_record_url(
                    getattr(client, "instance_url", None),
                    "incident",
                    incident_sys_id or "",
                )
                if incident_sys_id
                else None
            ),
            "affected_ci_references": [],
        }
        # MSP-B4 T1: structured resolution payload built from the SAME record.
        incident["resolution"] = _build_incident_resolution(
            record,
            incident_sys_id=incident_sys_id,
            incident_number=_sn_scalar(record.get("number")),
            instance_url=getattr(client, "instance_url", None),
            first_assigned_field=first_assigned_field,
        )
        if escalation_field and record.get(escalation_field):
            incident["escalated_to"] = record.get(escalation_field)
        incidents.append(incident)

    assignment_groups: Dict[str, Dict[str, Any]] = {}
    for incident in incidents:
        group = incident.get("assignment_group")
        if isinstance(group, dict):
            group_name = (
                group.get("display_value")
                or group.get("displayName")
                or group.get("name")
                or group.get("value")
            )
        else:
            group_name = group
        if not group_name:
            continue
        key = str(group_name).strip()
        summary = assignment_groups.setdefault(
            key,
            {"group_name": key, "incident_count": 0},
        )
        summary["incident_count"] += 1

    return {
        "total_incidents_90d": total,
        "avg_resolution_hours": avg_resolution,
        "avg_reassignment_count": 0.0,  # Extended in SF-3.2 using reassignment_count field
        "category_breakdown": list(category_map.values()),
        "incidents": incidents,
        "assignment_groups": list(assignment_groups.values()),
    }


def _assignment_group_display_names(
    client: ServiceNowClient,
    group_values: List[str],
) -> Dict[str, str]:
    """Resolve raw ServiceNow group sys_ids to queue names, read-only.

    ``sys_audit.oldvalue/newvalue`` are string columns and therefore commonly
    contain the referenced group's raw sys_id even with display values enabled.
    Resolve only values that have the canonical sys_id shape and request only
    ``sys_id,name`` from ``sys_user_group``.  Values that are already display
    names never trigger this extra query.
    """
    group_ids = sorted(
        {
            value.strip()
            for value in group_values
            if value and _SN_SYS_ID_RE.fullmatch(value.strip())
        }
    )
    if not group_ids:
        return {}

    resolved: Dict[str, str] = {}
    for offset in range(0, len(group_ids), 100):
        batch = group_ids[offset : offset + 100]
        records = client.table_query(
            "sys_user_group",
            {
                "sysparm_query": f"sys_idIN{','.join(batch)}^ORDERBYsys_id",
                "sysparm_fields": "sys_id,name",
                "sysparm_display_value": "false",
                "sysparm_exclude_reference_link": "true",
            },
            max_records=len(batch) + 1,
        )
        admitted = set(batch)
        for record in records:
            if not isinstance(record, dict):
                continue
            group_id = _sn_reference_id(record.get("sys_id"))
            group_name = _optional_sn_text(record.get("name"))
            if group_id in admitted and group_name:
                resolved[group_id] = group_name
    return resolved


def get_assignment_group_history(
    client: ServiceNowClient,
    incident_sys_ids: List[str],
    *,
    org_id: Optional[str] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Read ordered assignment-group history for admitted incidents only.

    ServiceNow's ``sys_audit`` rows explicitly record each assignment-group
    change.  This function reconstructs group states from ``oldvalue`` and
    ``newvalue`` without reading assignees or the user who made the change.
    Results are deterministic (timestamp + audit sys_id ordering), carry
    observed provenance, and are bounded to the supplied incident set.
    """
    from . import get_ingest_org

    admitted_incidents = {
        str(sys_id).strip() for sys_id in incident_sys_ids if str(sys_id).strip()
    }
    if not admitted_incidents:
        return {}

    effective_org = org_id or get_ingest_org()
    grouped_audits: Dict[str, List[Dict[str, Any]]] = {
        sys_id: [] for sys_id in admitted_incidents
    }
    instance_url = getattr(client, "instance_url", None)
    ordered_ids = sorted(admitted_incidents)
    remaining_records = ASSIGNMENT_HISTORY_RECORD_CAP

    for offset in range(0, len(ordered_ids), 100):
        if remaining_records <= 0:
            raise ServiceNowIngestError(
                "ServiceNow assignment history exceeded the bounded record cap"
            )
        batch = ordered_ids[offset : offset + 100]
        records = client.table_query(
            ASSIGNMENT_HISTORY_TABLE,
            {
                "sysparm_query": (
                    "tablename=incident^fieldname=assignment_group^"
                    f"documentkeyIN{','.join(batch)}^"
                    "ORDERBYsys_created_on^ORDERBYsys_id"
                ),
                "sysparm_fields": ",".join(ASSIGNMENT_HISTORY_FIELDS),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
            max_records=remaining_records,
        )
        remaining_records -= len(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            incident_sys_id = _sn_reference_id(record.get("documentkey"))
            history_sys_id = _sn_reference_id(record.get("sys_id"))
            if incident_sys_id not in admitted_incidents or not history_sys_id:
                logger.warning(
                    "ServiceNow assignment-history row is incomplete or outside "
                    "the admitted incident set; skipping"
                )
                continue
            grouped_audits[incident_sys_id].append(
                {
                    "history_sys_id": history_sys_id,
                    "old_group": _assignment_group_name(record.get("oldvalue")),
                    "new_group": _assignment_group_name(record.get("newvalue")),
                    "changed_at": _optional_sn_raw_text(record.get("sys_created_on")),
                }
            )

    raw_group_values = [
        value
        for rows in grouped_audits.values()
        for row in rows
        for value in (row.get("old_group"), row.get("new_group"))
        if value
    ]
    display_names = _assignment_group_display_names(client, raw_group_values)

    histories: Dict[str, List[Dict[str, Any]]] = {
        sys_id: [] for sys_id in admitted_incidents
    }
    for incident_sys_id, audit_rows in grouped_audits.items():
        audit_rows.sort(
            key=lambda row: (
                row.get("changed_at") or "",
                row.get("history_sys_id") or "",
            )
        )
        for index, row in enumerate(audit_rows):
            # The first audit row supplies the state before the first observed
            # transition.  Later rows add only their new state; the detector
            # collapses duplicate consecutive group entries defensively.
            states = []
            if index == 0 and row.get("old_group"):
                states.append(row["old_group"])
            if row.get("new_group"):
                states.append(row["new_group"])
            for raw_group in states:
                group_name = display_names.get(raw_group, raw_group)
                pointer = EvidencePointer.observed(
                    source_system="servicenow",
                    source_artifact=row["history_sys_id"],
                    source_timestamp=row.get("changed_at") or "unknown",
                    source_artifact_type="record_id",
                ).to_dict()
                histories[incident_sys_id].append(
                    {
                        "assignment_group": group_name,
                        "assignment_group_id": (
                            raw_group if _SN_SYS_ID_RE.fullmatch(raw_group) else None
                        ),
                        "changed_at": row.get("changed_at"),
                        "history_sys_id": row["history_sys_id"],
                        "org_id": effective_org,
                        "source_url": _servicenow_record_url(
                            instance_url,
                            ASSIGNMENT_HISTORY_TABLE,
                            row["history_sys_id"],
                        ),
                        "evidence": pointer,
                    }
                )
    return histories


def _attach_assignment_group_histories(
    incident_metrics: Dict[str, Any],
    client: ServiceNowClient,
) -> None:
    """Attach bounded group-level assignment histories to incident records."""
    incidents = incident_metrics.get("incidents") or []
    incident_ids = [
        str(incident.get("sys_id") or "").strip()
        for incident in incidents
        if isinstance(incident, dict) and incident.get("sys_id")
    ]
    try:
        grouped = get_assignment_group_history(client, incident_ids)
    except ServiceNowIngestError as exc:
        # Assignment history is detector enrichment.  If an instance denies
        # sys_audit access, keep incident ingestion usable and report the
        # unavailable state instead of inventing an empty/healthy history.
        logger.warning("ServiceNow assignment-history read unavailable: %s", exc)
        incident_metrics["assignment_history_lookup"] = {"status": "unavailable"}
        return

    incident_metrics["assignment_history_lookup"] = {"status": "available"}
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        incident_sys_id = str(incident.get("sys_id") or "").strip()
        incident["assignment_history"] = list(grouped.get(incident_sys_id, []))


def get_affected_ci_task_references(
    client: ServiceNowClient,
    incident_sys_ids: List[str],
) -> Dict[str, List[Dict[str, Any]]]:
    """Read explicit ``task_ci`` links for the supplied incident records.

    The query is bounded to the incident IDs already admitted by the current
    incident read.  Returned task and CI references are checked against that
    exact set, so a malformed response cannot attach another task's CI to an
    incident.  No names or free-text fields are requested or inspected.
    """
    admitted_incidents = {
        str(sys_id).strip() for sys_id in incident_sys_ids if str(sys_id).strip()
    }
    if not admitted_incidents:
        return {}

    instance_url = getattr(client, "instance_url", None)
    grouped: Dict[str, List[Dict[str, Any]]] = {
        sys_id: [] for sys_id in admitted_incidents
    }
    # Keep encoded queries comfortably below common proxy URL limits while
    # retaining deterministic ordering and the connector's normal record cap.
    ordered_ids = sorted(admitted_incidents)
    remaining_records = CMDB_RECORD_CAP
    for offset in range(0, len(ordered_ids), 100):
        if remaining_records <= 0:
            break
        batch = ordered_ids[offset : offset + 100]
        records = client.table_query(
            "task_ci",
            {
                "sysparm_query": (
                    f"taskIN{','.join(batch)}^ci_itemISNOTEMPTY^ORDERBYsys_id"
                ),
                "sysparm_fields": ",".join(AFFECTED_CI_TASK_FIELDS),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
            max_records=remaining_records,
        )
        remaining_records -= len(records)
        for record in records:
            if not isinstance(record, dict):
                continue
            relationship_sys_id = _sn_reference_id(record.get("sys_id"))
            incident_sys_id = _sn_reference_id(record.get("task"))
            ci_sys_id = _sn_reference_id(record.get("ci_item"))
            if (
                not relationship_sys_id
                or incident_sys_id not in admitted_incidents
                or not ci_sys_id
            ):
                logger.warning(
                    "ServiceNow affected-CI relationship is incomplete or outside "
                    "the admitted incident set; skipping"
                )
                continue
            grouped[incident_sys_id].append(
                {
                    "relationship_sys_id": relationship_sys_id,
                    "incident_sys_id": incident_sys_id,
                    "ci_sys_id": ci_sys_id,
                    "source_type": AFFECTED_CI_TASK_SOURCE_TYPE,
                    "source_timestamp": _optional_sn_text(
                        record.get("sys_updated_on")
                    ),
                    "source_url": _servicenow_record_url(
                        instance_url, "task_ci", relationship_sys_id
                    ),
                    "origin": "observed",
                }
            )

    for references in grouped.values():
        references.sort(
            key=lambda ref: (ref["ci_sys_id"], ref["relationship_sys_id"])
        )
    return grouped


def _attach_affected_ci_task_references(
    incident_metrics: Dict[str, Any],
    client: ServiceNowClient,
) -> None:
    """Attach bounded secondary CI references and their read status in place."""
    incidents = incident_metrics.get("incidents") or []
    incident_ids = [
        str(incident.get("sys_id") or "").strip()
        for incident in incidents
        if isinstance(incident, dict) and incident.get("sys_id")
    ]
    try:
        grouped = get_affected_ci_task_references(client, incident_ids)
    except ServiceNowIngestError as exc:
        # Primary cmdb_ci resolution remains useful when the optional secondary
        # table is unavailable.  Preserve that partial state instead of turning
        # the whole ServiceNow ingest into a failure.
        logger.warning("ServiceNow affected-CI read unavailable: %s", exc)
        incident_metrics["affected_ci_lookup"] = {"status": "unavailable"}
        return

    incident_metrics["affected_ci_lookup"] = {"status": "available"}
    for incident in incidents:
        if not isinstance(incident, dict):
            continue
        incident_sys_id = str(incident.get("sys_id") or "").strip()
        incident["affected_ci_references"] = list(
            grouped.get(incident_sys_id, [])
        )


def get_cross_system_references(
    client: Optional[ServiceNowClient] = None,
    patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Detect ServiceNow incidents that reference Salesforce case IDs (D7 signal).

    The echo_score = match_count / total_incidents_in_window.

    IMPORTANT — total_count derivation:
        This uses the Aggregate API to get total incident count, NOT a hardcoded
        value and NOT a count of the matched records only. This was the known bug
        in the earlier stub where total_count was hardcoded as 0, making
        echo_score always 0.0.

    Search fields inspected:
        short_description — case reference in summary
        description       — full incident description
        work_notes        — agent work log (most common location for CS- refs)
        comments          — customer-visible comments

    Live API calls:
        -- Total incidents (aggregate)
        GET /api/now/stats/incident?sysparm_count=true&sysparm_query=window

        -- Incidents matching pattern in any of the four fields
        GET /api/now/table/incident
            ?sysparm_query=short_descriptionCONTAINS{pattern}^ORdescriptionCONTAINS{pattern}...
            &sysparm_fields=sys_id,short_description,description,work_notes

    Returns: cross_system_references dict matching servicenow_sample.json shape
    """
    if not is_live():
        return _load_fixture()["cross_system_references"]

    if patterns is None:
        patterns = ["CS-"]  # Salesforce Case ID prefix

    window_query = f"sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"

    # Total incident count — aggregate, not a table scan
    total = client.aggregate_count("incident", window_query)

    if total == 0:
        logger.warning("ServiceNow: no incidents in window — echo_score will be 0.0")
        return {
            "sn_match_count": 0,
            "sn_total_incidents": 0,
            "sn_echo_score": 0.0,
            "matched_pattern": patterns[0] if patterns else "",
            "sample_matches": [],
        }

    match_count = 0
    sample_matches: List[Dict] = []
    matched_pattern = patterns[0] if patterns else ""

    for pattern in patterns:
        # Build OR query across all four description fields
        field_conditions = "^OR".join(
            [
                f"short_descriptionCONTAINS{pattern}",
                f"descriptionCONTAINS{pattern}",
                f"work_notesCONTAINS{pattern}",
                f"commentsCONTAINS{pattern}",
            ]
        )
        full_query = f"{window_query}^({field_conditions})"

        # Count matches — aggregate first, then fetch samples
        pattern_count = client.aggregate_count("incident", full_query)
        match_count += pattern_count

        if pattern_count > 0 and len(sample_matches) < 5:
            sample_recs = client.table_query(
                "incident",
                {
                    "sysparm_query": full_query,
                    "sysparm_fields": "number,short_description,description,work_notes",
                    "sysparm_limit": 5,
                },
            )
            for r in sample_recs[:5]:
                # Determine which field contained the match
                match_field = "short_description"
                for fld in (
                    "short_description",
                    "description",
                    "work_notes",
                    "comments",
                ):
                    if pattern in (r.get(fld) or ""):
                        match_field = fld
                        break
                sample_matches.append(
                    {
                        "incident_id": r.get("number", ""),
                        "pattern": pattern,
                        "field": match_field,
                        "short_description": (r.get("short_description") or "")[:120],
                    }
                )

    # echo_score: correctly derived from real total_count
    sn_echo_score = round(match_count / total, 4) if total > 0 else 0.0

    return {
        "sn_match_count": match_count,
        "sn_total_incidents": total,
        "sn_echo_score": sn_echo_score,
        "matched_pattern": matched_pattern,
        "sample_matches": sample_matches,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ENT-5 — Enterprise Operations cross-system blocks (LIVE)
#
# These three functions build the ServiceNow blocks the enterprise_ops detectors
# read. They are implemented and ready, but the CALLS that merge them into
# ingest()'s return dict are COMMENTED OUT below — uncomment them once the SME
# team confirms field names and loads the data. See
# docs/ENT5_enterprise_ops_live_data_requirements.md.
#
# Org-specific field names (override via env; defaults are the common choice):
#   SERVICENOW_JIRA_KEY_FIELD  incident field holding the Jira key  (default: correlation_id)
#   SERVICENOW_SLA_FIELD       incident SLA-attainment boolean      (default: made_sla)
#
# Each function fails safe: on any query error it returns a degraded block so an
# unconfirmed field name never crashes the run — the detector simply won't fire.
# ─────────────────────────────────────────────────────────────────────────────


def get_incident_resolution(client: Optional[ServiceNowClient] = None) -> Dict[str, Any]:
    """Build the ``incident_resolution`` block for ENT_INCIDENT_RESOLUTION_LAG.

    Reads CLOSED/RESOLVED incidents (state IN 6,7) created in the window that
    carry a Jira key in SERVICENOW_JIRA_KEY_FIELD, with their close date.
    """
    if not is_live():
        return {"closed_incidents": [], "degraded_signal": False}
    jira_field = os.getenv("SERVICENOW_JIRA_KEY_FIELD", "correlation_id").strip()
    query = (
        f"sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"
        f"^stateIN6,7^{jira_field}ISNOTEMPTY"
    )
    try:
        records = client.table_query(
            "incident",
            {
                "sysparm_query": query,
                "sysparm_fields": f"number,state,opened_at,closed_at,resolved_at,{jira_field}",
                "sysparm_display_value": "all",
            },
        )
    except Exception as e:  # noqa: BLE001 — never abort the run on a bad field name
        logger.warning("ENT-5 incident_resolution query failed (degraded): %s", e)
        return {"closed_incidents": [], "degraded_signal": True}

    closed_incidents: List[Dict[str, Any]] = []
    for r in records:
        key = _sn_scalar(r.get(jira_field))
        closed_at = _sn_scalar(r.get("closed_at")) or _sn_scalar(r.get("resolved_at"))
        if key and closed_at:
            closed_incidents.append(
                {
                    "number": _sn_scalar(r.get("number")),
                    "jira_issue_key": str(key),
                    "closed_at": closed_at,
                }
            )
    return {"closed_incidents": closed_incidents, "degraded_signal": False}


def get_change_correlation(client: Optional[ServiceNowClient] = None) -> Dict[str, Any]:
    """Build the ``change_correlation`` block for ENT_CHANGE_INCIDENT_CORRELATION.

    Reads implemented change_request records (with close date) and incidents
    (with open date) in the window; the detector does the 72h correlation math.
    """
    if not is_live():
        return {"changes": [], "incidents": [], "degraded_signal": False}
    try:
        changes_raw = client.table_query(
            "change_request",
            {
                "sysparm_query": (
                    f"state=Implemented^sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"
                ),
                "sysparm_fields": "number,state,closed_at",
                "sysparm_display_value": "all",
            },
        )
        incidents_raw = client.table_query(
            "incident",
            {
                "sysparm_query": f"opened_at>=javascript:gs.daysAgo({WINDOW_DAYS})",
                "sysparm_fields": "number,opened_at",
                "sysparm_display_value": "all",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ENT-5 change_correlation query failed (degraded): %s", e)
        return {"changes": [], "incidents": [], "degraded_signal": True}

    changes = [
        {
            "number": _sn_scalar(c.get("number")),
            "state": _sn_scalar(c.get("state")),
            "closed_at": _sn_scalar(c.get("closed_at")),
        }
        for c in changes_raw
    ]
    incidents = [
        {"number": _sn_scalar(i.get("number")), "opened_at": _sn_scalar(i.get("opened_at"))}
        for i in incidents_raw
    ]
    return {"changes": changes, "incidents": incidents, "degraded_signal": False}


def get_sla_breach_by_team(client: Optional[ServiceNowClient] = None) -> Dict[str, Any]:
    """Build the ``sla_breach_by_team`` block for ENT_SLA_BREACH_BY_TEAM.

    Reads incidents in the window with their assignment_group and SLA-attainment
    flag (SERVICENOW_SLA_FIELD, default made_sla where False == breached). Returns
    raw incidents; the detector groups by team and computes concentration.
    """
    if not is_live():
        return {"incidents": [], "degraded_signal": False}
    sla_field = os.getenv("SERVICENOW_SLA_FIELD", "made_sla").strip()
    try:
        records = client.table_query(
            "incident",
            {
                "sysparm_query": (
                    f"sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"
                    f"^assignment_groupISNOTEMPTY"
                ),
                "sysparm_fields": f"number,assignment_group,{sla_field}",
                "sysparm_display_value": "all",
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ENT-5 sla_breach_by_team query failed (degraded): %s", e)
        return {"incidents": [], "degraded_signal": True}

    incidents: List[Dict[str, Any]] = []
    for r in records:
        group = _sn_scalar(r.get("assignment_group"))
        made_sla_raw = _sn_scalar(r.get(sla_field))
        # made_sla == false → the incident breached its SLA.
        breached = str(made_sla_raw).strip().lower() in ("false", "0", "no")
        incidents.append({"assignment_group": group, "made_sla": (not breached)})
    return {"incidents": incidents, "degraded_signal": False}


# ─────────────────────────────────────────────────────────────────────────────
# Main ingest()
# ─────────────────────────────────────────────────────────────────────────────


def ingest(
    sn_client: Optional[ServiceNowClient] = None,
    *,
    include_cmdb: bool = True,
) -> Dict[str, Any]:
    """
    Orchestrate ServiceNow ingestion. Returns combined payload.

    Offline: reads fixture. Live: calls both functions.
    If SERVICENOW_URL is not set in live mode, logs warning and returns {}.
    The runner treats empty SN data as graceful skip — D7 will still fire
    from the Salesforce sf_echo_score side if that threshold is met.
    """
    from . import get_ingest_org

    if not is_live():
        logger.info("ServiceNow ingestion: offline mode (fixture)")
        fixture = _load_fixture()
        fixture["org_id"] = get_ingest_org()
        raw_incidents = fixture.get("incident_metrics", {}).get(
            "incidents", fixture.get("incident_metrics", {}).get("recent_incidents", [])
        )
        # MSP-B4 T1: attach the structured resolution payload to each incident.
        # ingest() returns the fixture's incident_metrics verbatim (it does not
        # route through get_incident_metrics), so the attach must happen here too.
        if isinstance(fixture.get("incident_metrics"), dict):
            _attach_incident_resolutions(
                fixture["incident_metrics"],
                _fixture_instance_url(fixture),
                first_assigned_field=os.getenv(FIRST_ASSIGNED_FIELD_ENV, "").strip(),
            )
        fixture["lending_correlation"] = get_lending_correlation(
            fixture_incidents=raw_incidents
        )
        # Offline fixtures contain no customer data, so use the bounded product
        # default without requiring an organization connector-config database.
        if include_cmdb:
            fixture["cmdb"] = ingest_cmdb(class_scope=DEFAULT_CMDB_CLASSES)
        return fixture

    # OAuth-first: the live instance URL comes from the per-run context (DB-sourced
    # vault token + captured/derived URL, set by resolve_live_systems); the
    # SERVICENOW_URL env var is only a CLI/standalone fallback. Gating on the env
    # var alone wrongly skipped ServiceNow even when it was authenticated via the
    # Integration Hub OAuth flow.
    from . import get_live_connector, resolve_vault_connector

    cred = get_live_connector("servicenow") or resolve_vault_connector("servicenow")
    sn_url = (cred.get("url") if cred else None) or os.getenv("SERVICENOW_URL", "")
    if not sn_url:
        logger.warning(
            "ServiceNow is not connected (no OAuth credentials for this run, and "
            "SERVICENOW_URL is unset) — skipping ServiceNow ingestion. "
            "D7 will rely on Salesforce-side echo score only."
        )
        return {}

    logger.info("ServiceNow ingestion: live mode")
    if sn_client is None:
        sn_client = _get_client()

    try:
        incident_metrics = get_incident_metrics(sn_client)
        _attach_assignment_group_histories(incident_metrics, sn_client)
        _attach_affected_ci_task_references(incident_metrics, sn_client)
        cross_system_references = get_cross_system_references(sn_client)

        lending_correlation = get_lending_correlation(sn_client)
        cmdb = ingest_cmdb(sn_client) if include_cmdb else None

        return {
            "org_id": get_ingest_org(),
            "incident_metrics": incident_metrics,
            "cross_system_references": cross_system_references,
            "lending_correlation": lending_correlation,
            **({"cmdb": cmdb} if include_cmdb else {}),
            "assignment_groups": incident_metrics.get("assignment_groups", []),
            # ── ENT-5 enterprise_ops cross-system blocks (LIVE) ───────────────
            # UNCOMMENT the three lines below once the SME team confirms the
            # field names and loads the data into ServiceNow. Set, if different
            # from the defaults:
            #   SERVICENOW_JIRA_KEY_FIELD  (default: correlation_id)
            #   SERVICENOW_SLA_FIELD       (default: made_sla)
            # See docs/ENT5_enterprise_ops_live_data_requirements.md.
            # "incident_resolution": get_incident_resolution(sn_client),
            # "change_correlation":  get_change_correlation(sn_client),
            # "sla_breach_by_team":  get_sla_breach_by_team(sn_client),
        }
    except ServiceNowIngestError:
        raise
    except Exception as e:
        raise ServiceNowIngestError(f"ServiceNow ingestion failed: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# ENG-AIQ-NC-3 — ServiceNow Lending Correlation
# ─────────────────────────────────────────────────────────────────────────────

SN_LENDING_KEYWORD_MAP = [
    (
        ["covenant", "compliance", "breach", "covenant status"],
        "COVENANT_TRACKING_GAP",
        "Covenant compliance",
    ),
    (
        ["checklist", "document exception", "closing", "pre-close"],
        "CHECKLIST_BOTTLENECK",
        "Document checklist",
    ),
    (
        ["routing", "origination", "reassignment", "underwriting assignment"],
        "LOAN_ORIGINATION_ROUTING_FRICTION",
        "Loan origination routing",
    ),
    (
        ["spreading", "spread", "analyst", "credit analyst"],
        "SPREADING_BOTTLENECK",
        "Financial spreading",
    ),
    (
        ["approval", "credit committee", "loan approval", "approval notification"],
        "APPROVAL_BOTTLENECK",
        "Loan approval",
    ),
]

SN_ALL_LENDING_KEYWORDS = [
    kw for entry in SN_LENDING_KEYWORD_MAP for kw in entry[0]
] + ["loan", "nCino", "ncino", "lending", "borrower"]


def _sn_incident_matches(incident: Dict[str, Any], keywords: List[str]) -> bool:
    """
    Weighted keyword match to reduce false positives.

    Scoring:
      category/subcategory match = 2 points  (explicit classification)
      short_description match    = 1 point   (title-level signal)
      description match          = 0.5 pts   (body text)

    Threshold: score >= 1.5 to fire.
    Single keyword in description only does NOT fire.
    Generic terms like "loan" or "routing" without category or
    short_description match will not reach threshold.
    """
    score = 0.0
    cat_text = " ".join(
        [
            incident.get("category", ""),
            incident.get("subcategory", "") or "",
        ]
    ).lower()
    short_text = incident.get("short_description", "").lower()
    desc_text = (incident.get("description", "") or "").lower()

    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in cat_text:
            score += 2.0
        elif kw_lower in short_text:
            score += 1.0
        elif kw_lower in desc_text:
            score += 0.5

    return score >= 1.0  # Lowered from 1.5 — single keyword in title is sufficient signal


def _sn_detector_for_incident(incident: Dict[str, Any]) -> Optional[tuple]:
    """Return (detector_id, banking_label) for best-matching detector, or None."""
    for keywords, detector_id, label in SN_LENDING_KEYWORD_MAP:
        if _sn_incident_matches(incident, keywords):
            return detector_id, label
    return None


def _sn_build_lending_snippet(incident: Dict[str, Any], label: str) -> str:
    """Build a banking-language evidence snippet from a ServiceNow incident."""
    short_desc = incident.get("short_description", "ServiceNow incident")
    priority = incident.get("priority", "")
    state = incident.get("state", "")
    parts = [f"{label}: {short_desc}"]
    if priority:
        parts.append(f"Priority: {priority}")
    if state:
        parts.append(f"State: {state}")
    return ". ".join(parts) + "."


def get_lending_correlation(
    client: Optional["ServiceNowClient"] = None,
    fixture_incidents: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    ENG-AIQ-NC-3: Detect lending-related ServiceNow incidents and map them
    to nCino detector IDs for use as corroborating evidence in S4.

    Returns:
      lending_incidents: list of matched incidents with detector_id and snippet
      by_detector:       dict mapping detector_id → list of snippets
      total_matched:     int
    """
    incidents: List[Dict[str, Any]] = []

    if fixture_incidents is not None:
        incidents = fixture_incidents
    elif not is_live():
        try:
            fixture = _load_fixture()
            raw = fixture.get("incident_metrics", {})
            incidents = raw.get("incidents", raw.get("recent_incidents", []))
        except Exception:
            incidents = []
    else:
        if client is None:
            try:
                client = _get_client()
            except Exception:
                return {"lending_incidents": [], "by_detector": {}, "total_matched": 0}
        try:
            # Fetch recent incidents with lending keywords
            # Build query using ^NQ (new query) to OR between keyword groups
            # ^NQ is ServiceNow's separator for OR between filter groups
            # Using ^ (AND) between groups returned 0 results
            kw_groups = [
                f"short_descriptionLIKE{kw}^ORdescriptionLIKE{kw}"
                for kw in SN_ALL_LENDING_KEYWORDS[:8]
            ]
            kw_filter = "^NQ".join(kw_groups)
            query = f"active=true^{kw_filter}"
            result = client.table_query(
                "incident",
                params={
                    "sysparm_query": query,
                    "sysparm_limit": 50,
                    "sysparm_fields": (
                        "sys_id,number,short_description,description,"
                        "category,subcategory,priority,state,sys_created_on"
                    ),
                },
            )
            for inc in result:
                incidents.append(
                    {
                        "id": inc.get("sys_id", ""),
                        "number": inc.get("number", ""),
                        "short_description": inc.get("short_description", ""),
                        "description": inc.get("description", "") or "",
                        "category": inc.get("category", ""),
                        "subcategory": inc.get("subcategory", "") or "",
                        "priority": inc.get("priority", ""),
                        "state": inc.get("state", ""),
                        "sys_created_on": inc.get("sys_created_on", ""),
                    }
                )
        except Exception as e:
            logger.warning("ServiceNow lending correlation fetch failed: %s", e)
            return {"lending_incidents": [], "by_detector": {}, "total_matched": 0}

    # Match incidents to detectors
    lending_incidents: List[Dict[str, Any]] = []
    by_detector: Dict[str, List[str]] = {}

    for incident in incidents:
        match = _sn_detector_for_incident(incident)
        if match is None:
            continue
        detector_id, label = match
        snippet = _sn_build_lending_snippet(incident, label)
        lending_incidents.append(
            {
                "incident_id": incident.get("number") or incident.get("id", ""),
                "detector_id": detector_id,
                "label": label,
                "snippet": snippet,
                "source": "ServiceNow",
                "detectorId": detector_id,
                "state": incident.get("state", ""),
                "sys_created_on": incident.get("sys_created_on", ""),
            }
        )
        by_detector.setdefault(detector_id, []).append(snippet)

    logger.info("SN lending correlation: %d incidents matched", len(lending_incidents))
    return {
        "lending_incidents": lending_incidents,
        "by_detector": by_detector,
        "total_matched": len(lending_incidents),
    }
