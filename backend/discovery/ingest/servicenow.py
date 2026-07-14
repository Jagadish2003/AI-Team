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
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, urlsplit

from . import is_live

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
    "sys_updated_on",
)
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

    def __init__(self, instance_url: str, token: str = "", username: str = ""):
        self.instance_url = instance_url.rstrip("/")
        self.token = token
        self.username = username
        self._session = None

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

    def table_query(
        self,
        table: str,
        params: Dict[str, Any],
        max_records: int = 10000,
    ) -> List[Dict]:
        """
        Query a ServiceNow table with sysparm_offset pagination.

        max_records: safety cap — raises if exceeded.
        ServiceNow uses offset-based pagination (not cursor), so we step
        through sysparm_offset in sysparm_limit increments.
        """
        session = self._get_session()
        limit = min(params.get("sysparm_limit", 1000), 1000)  # SN max page = 1000
        offset = 0
        all_records: List[Dict] = []

        base_url = f"{self.instance_url}/api/now/table/{table}"
        query_params = {**params, "sysparm_limit": limit}

        while True:
            query_params["sysparm_offset"] = offset
            try:
                resp = session.get(base_url, params=query_params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                records = data.get("result", [])
                if not records:
                    break
                all_records.extend(records)
                if len(all_records) >= max_records:
                    raise ServiceNowIngestError(
                        f"ServiceNow table/{table} result exceeded {max_records} records. "
                        f"Narrow the query window."
                    )
                if len(records) < limit:
                    break  # last page
                offset += limit
            except ServiceNowIngestError:
                raise
            except Exception as e:
                raise ServiceNowIngestError(
                    f"ServiceNow table/{table} query failed: {e}"
                )

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
            raise ServiceNowIngestError(
                f"ServiceNow aggregate count on {table} failed: {e}"
            )


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


def resolve_cmdb_class_scope() -> Tuple[str, ...]:
    """Resolve the effective class scope from the current ingest organization."""
    from . import get_ingest_org

    org_id = get_ingest_org()
    return normalize_cmdb_class_scope(_load_org_cmdb_config(org_id))


def _optional_sn_text(value: Any) -> Optional[str]:
    scalar = _sn_scalar(value)
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
    ci_class = (_optional_sn_text(record.get("sys_class_name")) or "").lower()
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
        updated_at=_optional_sn_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, "cmdb_ci", sys_id),
    )


def _read_cmdb_configuration_items(
    class_scope: Tuple[str, ...],
    client: Optional[ServiceNowClient] = None,
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
        class_filter = f"sys_class_nameIN{','.join(class_scope)}"
        records = client.table_query(
            "cmdb_ci",
            {
                "sysparm_query": (
                    f"{class_filter}^ORDERBYsys_class_name^ORDERBYname^ORDERBYsys_id"
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
        source_timestamp=_optional_sn_text(record.get("sys_updated_on")),
        source_url=_servicenow_record_url(instance_url, "cmdb_rel_ci", sys_id),
    )


def _read_cmdb_relationships(
    class_scope: Tuple[str, ...],
    configuration_items: List[ServiceNowConfigurationItem],
    client: Optional[ServiceNowClient] = None,
    rules: Optional[Mapping[str, CMDBRelationshipRule]] = None,
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
        records = client.table_query(
            "cmdb_rel_ci",
            {
                "sysparm_query": (
                    f"parent.sys_class_nameIN{class_csv}^"
                    f"child.sys_class_nameIN{class_csv}^ORDERBYsys_id"
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
                            assignment_group,caller_id,resolved_at,sys_created_on
            &sysparm_limit=1000

    Returns: incident_metrics dict matching servicenow_sample.json shape
    """
    if not is_live():
        return _load_fixture()["incident_metrics"]

    window_query = f"sys_created_on>=javascript:gs.daysAgo({WINDOW_DAYS})"

    # Total count — aggregate API, not a full record fetch
    total = client.aggregate_count("incident", window_query)

    # Incident details for category breakdown
    fields = [
        "sys_id",
        "number",
        "category",
        "state",
        "assigned_to",
        "assignment_group",
        "caller_id",
        "resolved_at",
        "sys_created_on",
    ]
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
        incident = {
            "id": _sn_scalar(record.get("sys_id")) or _sn_scalar(record.get("number")),
            "number": _sn_scalar(record.get("number")) or _sn_scalar(record.get("sys_id")),
            "category": _sn_scalar(record.get("category")),
            "state": _sn_scalar(record.get("state")),
            "assigned_to": record.get("assigned_to"),
            "assignment_group": record.get("assignment_group"),
            "caller_id": record.get("caller_id"),
        }
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


def ingest(sn_client: Optional[ServiceNowClient] = None) -> Dict[str, Any]:
    """
    Orchestrate ServiceNow ingestion. Returns combined payload.

    Offline: reads fixture. Live: calls both functions.
    If SERVICENOW_URL is not set in live mode, logs warning and returns {}.
    The runner treats empty SN data as graceful skip — D7 will still fire
    from the Salesforce sf_echo_score side if that threshold is met.
    """
    if not is_live():
        logger.info("ServiceNow ingestion: offline mode (fixture)")
        fixture = _load_fixture()
        raw_incidents = fixture.get("incident_metrics", {}).get(
            "incidents", fixture.get("incident_metrics", {}).get("recent_incidents", [])
        )
        fixture["lending_correlation"] = get_lending_correlation(
            fixture_incidents=raw_incidents
        )
        # Offline fixtures contain no customer data, so use the bounded product
        # default without requiring an organization connector-config database.
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
        cross_system_references = get_cross_system_references(sn_client)

        lending_correlation = get_lending_correlation(sn_client)
        cmdb = ingest_cmdb(sn_client)

        return {
            "incident_metrics": incident_metrics,
            "cross_system_references": cross_system_references,
            "lending_correlation": lending_correlation,
            "cmdb": cmdb,
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
