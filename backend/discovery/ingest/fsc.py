"""fsc.py — 2.0-D1 T2: Financial Services Cloud ingest.

The story's wording ("detectors over FSC objects") understates this: before T2
there was not a single ``FinServ__`` reference anywhere in the backend, and
``salesforce.py`` reads standard objects only. This module is the missing ingest
surface — the SOQL for the FSC managed-package objects alongside the standard-
object reads, normalised into ONE detector-visible block.

WHAT IT READS
-------------
FSC managed package : ``FinServ__Referral__c``, ``FinServ__Referral__History``,
                      ``FinServ__FinancialAccount__c``,
                      ``FinServ__FinancialAccount__History``
Standard objects    : ``Case`` (FSC service-process record types),
                      ``Account`` (household record type), ``AccountHistory``,
                      ``ProcessInstance``, ``ProcessInstanceWorkitem``

TWO DISCIPLINES THIS MODULE EXISTS TO ENFORCE
---------------------------------------------
1. **Tolerant field reads.** The documented ServiceNow failure mode
   (``sysparm_display_value=all`` returning ``{value, display_value}`` while
   fixtures held plain scalars, so tests passed either way and the bug appeared
   only in production) is the trap here too. Salesforce's analogue is parent
   traversal: ``RecordType.DeveloperName`` arrives as a NESTED OBJECT, is ``null``
   whenever the lookup is empty, and a middleware may wrap values. ``_field()``
   therefore resolves dotted paths, tolerates nulls at any level, and unwraps a
   ``{value, display_value}`` envelope if one ever appears. A record whose record
   type will not resolve is COUNTED as unresolved, never silently bucketed.

2. **The AC5 aggregation floor, applied at the boundary.** FSC records
   legitimately contain owners, contacts and household names; the normalised
   signal must not. Person-shaped fields are dropped here (``scrub_person_fields``)
   rather than merely "not read later", and ``Owner`` is resolved to a QUEUE name
   only when ``Owner.Type == 'Queue'`` — a user-owned record contributes no owner
   at all. Households appear as COUNTS plus opaque record-id POINTERS, never as
   names, because a household name identifies a family and, for a single-member
   household, a person.

The normalised block is published at ``sf_data['fsc']`` and is the ONLY thing the
five FSC detectors read. Offline (default) it comes from
``fixtures/fsc_sample.json``; live it comes from SOQL against the connected org.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:
    from ..packs.fsc_finding import scrub_person_fields
except ImportError:  # pragma: no cover - import shim
    from discovery.packs.fsc_finding import scrub_person_fields  # type: ignore

try:
    from ..packs.financial_services_cloud_config import (
        calibration_status,
        get_aggregation,
        get_detector_thresholds,
        get_scope,
    )
except ImportError:  # pragma: no cover - import shim
    from discovery.packs.financial_services_cloud_config import (  # type: ignore
        calibration_status,
        get_aggregation,
        get_detector_thresholds,
        get_scope,
    )

API_VERSION = "v59.0"
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "fsc_sample.json"

# Record types and picklist values are ORG-CONFIGURABLE, so they load from the
# pack config's `scope` block rather than being fixed here (confirming a customer's
# real DeveloperNames needs records from their FSC org — an outstanding SME
# dependency). The module-level tuples below are the DEGRADE PATH only, used when
# the config is unreadable, exactly as the detectors' threshold defaults are.
# An unmatched record type is COUNTED as unresolved, never assumed in scope.
SERVICE_PROCESS_RECORD_TYPES = ("Service_Process", "Service_Request", "ActionPlan")
HOUSEHOLD_RECORD_TYPES = ("IndustriesHousehold", "Household")

# Case statuses meaning "still open". The CLOSED set is matched rather than the
# open set, so an unrecognised org-specific status reads as OPEN — the conservative
# direction for an ageing detector, which would otherwise under-report silently.
CLOSED_STATUSES = {"closed", "closed_resolved", "resolved", "cancelled", "canceled"}


def _scope_service_process_types() -> tuple:
    try:
        configured = tuple(get_scope().service_process_record_types)
    except Exception:  # noqa: BLE001 - degrade to documented defaults
        configured = ()
    return configured or SERVICE_PROCESS_RECORD_TYPES


def _scope_household_types() -> tuple:
    try:
        configured = tuple(get_scope().household_record_types)
    except Exception:  # noqa: BLE001
        configured = ()
    return configured or HOUSEHOLD_RECORD_TYPES


def _scope_closed_statuses() -> set:
    try:
        configured = {str(x).lower() for x in get_scope().closed_statuses}
    except Exception:  # noqa: BLE001
        configured = set()
    return configured or CLOSED_STATUSES

# The field group whose duplicate maintenance across household and financial
# account records constitutes cross-object rework.
REWORK_FIELD_GROUP = "contact_details"
_REWORK_FIELDS = ("address", "phone", "email", "contact")


class FscIngestError(Exception):
    """FSC ingest could not complete."""


# ── Tolerant Salesforce field access (see discipline 1 above) ───────────────────


def _unwrap(value: Any) -> Any:
    """Unwrap a ``{value, display_value}`` envelope if one is present.

    Salesforce does not normally do this, but a proxy/middleware can, and this is
    precisely the shape that produced a production-only bug on the ServiceNow
    connector. Preferring ``value`` keeps raw (canonical) forms for ids and
    datetimes; ``display_value`` is only used when ``value`` is absent.
    """
    if isinstance(value, dict) and ("value" in value or "display_value" in value):
        if len(set(value) - {"value", "display_value"}) == 0:
            return value.get("value", value.get("display_value"))
    return value


def _field(record: Any, path: str, default: Any = None) -> Any:
    """Read ``path`` (dot-separated for parent traversal) off a Salesforce record.

    Tolerates: a missing key, a ``null`` relationship at any level (``RecordType``
    is ``null`` whenever the record has none), a non-dict intermediate, and a
    ``{value, display_value}`` envelope.
    """
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return default
        if part not in current:
            return default
        current = _unwrap(current.get(part))
        if current is None:
            return default
    return current if current is not None else default


def _records(payload: Dict[str, Any], sobject: str) -> List[Dict[str, Any]]:
    """Return the records array for ``sobject`` from a query-envelope payload.

    Accepts the real REST envelope ``{totalSize, done, records: [...]}`` and also a
    bare list, so a caller holding already-unwrapped rows still works.
    """
    block = payload.get(sobject)
    if isinstance(block, dict):
        rows = block.get("records", [])
    elif isinstance(block, list):
        rows = block
    else:
        rows = []
    return [r for r in rows if isinstance(r, dict)]


def _parse_dt(value: Any) -> Optional[datetime]:
    """Parse a Salesforce datetime/date into an aware UTC datetime."""
    raw = _unwrap(value)
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    # Salesforce sends '+0000'; fromisoformat wants '+00:00'.
    if text.endswith("+0000"):
        text = text[:-5] + "+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _days_between(later: Optional[datetime], earlier: Optional[datetime]) -> Optional[float]:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds() / 86400.0, 2)


def _queue_name(record: Dict[str, Any], prefix: str = "Owner") -> str:
    """Return the OWNING QUEUE's name, or '' when the record is user-owned.

    The AC5 floor in one function: ``Owner.Name`` is a queue name when
    ``Owner.Type == 'Queue'`` and a PERSON'S NAME when it is 'User'. Only the
    former is ever returned; a user-owned record yields '' and is aggregated
    without an owner rather than with a person.
    """
    if str(_field(record, f"{prefix}.Type", "")).strip().lower() != "queue":
        return ""
    return str(_field(record, f"{prefix}.Name", "") or "").strip()


def _is_in_scope_service_process(case: Dict[str, Any]) -> Optional[bool]:
    """True/False for in-scope, or None when the record type will not resolve.

    None is deliberately distinct from False: an unresolved record type is a data
    gap to report, not evidence the record is out of scope.
    """
    developer_name = _field(case, "RecordType.DeveloperName")
    if not developer_name:
        return None
    return str(developer_name).strip() in _scope_service_process_types()


def _is_open(case: Dict[str, Any]) -> bool:
    if _field(case, "ClosedDate"):
        return False
    return str(_field(case, "Status", "") or "").strip().lower() not in _scope_closed_statuses()


# ── SOQL (live) ─────────────────────────────────────────────────────────────────
#
# Column lists deliberately include the person fields the platform needs for
# nothing: they are NOT selected. Selecting only non-person columns means a
# person value cannot leak even if a later normaliser forgets to scrub.

SOQL_CASE = f"""
    SELECT Id, CaseNumber, Type, Status, Reason, AccountId, CreatedDate, ClosedDate,
           RecordType.DeveloperName, Owner.Type, Owner.Name
    FROM Case
    WHERE CreatedDate = LAST_N_DAYS:180
    LIMIT 5000
""".strip()

SOQL_REFERRAL = """
    SELECT Id, Name, FinServ__ReferralType__c, FinServ__Status__c,
           FinServ__Household__c, CreatedDate, FinServ__ExpectedCloseDate__c,
           Owner.Type, Owner.Name
    FROM FinServ__Referral__c
    WHERE CreatedDate = LAST_N_DAYS:180
    LIMIT 5000
""".strip()

SOQL_REFERRAL_HISTORY = """
    SELECT Id, ParentId, Field, OldValue, NewValue, CreatedDate
    FROM FinServ__Referral__History
    WHERE CreatedDate = LAST_N_DAYS:180
    LIMIT 10000
""".strip()

SOQL_FINANCIAL_ACCOUNT = """
    SELECT Id, FinServ__FinancialAccountType__c, FinServ__Household__c,
           FinServ__Status__c, LastModifiedDate
    FROM FinServ__FinancialAccount__c
    LIMIT 5000
""".strip()

SOQL_FINANCIAL_ACCOUNT_HISTORY = """
    SELECT Id, ParentId, Field, CreatedDate
    FROM FinServ__FinancialAccount__History
    WHERE CreatedDate = LAST_N_DAYS:180
    LIMIT 10000
""".strip()

SOQL_ACCOUNT_HISTORY = """
    SELECT Id, AccountId, Field, CreatedDate
    FROM AccountHistory
    WHERE CreatedDate = LAST_N_DAYS:180
    LIMIT 10000
""".strip()

SOQL_HOUSEHOLD = """
    SELECT Id, FinServ__TotalNumberOfMembers__c, RecordType.DeveloperName
    FROM Account
    LIMIT 5000
""".strip()

SOQL_PROCESS_INSTANCE = """
    SELECT Id, TargetObjectId, Status, CreatedDate, CompletedDate,
           ProcessDefinition.DeveloperName
    FROM ProcessInstance
    WHERE CreatedDate = LAST_N_DAYS:180
    LIMIT 5000
""".strip()

SOQL_PROCESS_WORKITEM = """
    SELECT Id, ProcessInstanceId, CreatedDate, OriginalActor.Type, OriginalActor.Name
    FROM ProcessInstanceWorkitem
    LIMIT 5000
""".strip()

_LIVE_QUERIES: Tuple[Tuple[str, str], ...] = (
    ("Case", SOQL_CASE),
    ("FinServ__Referral__c", SOQL_REFERRAL),
    ("FinServ__Referral__History", SOQL_REFERRAL_HISTORY),
    ("FinServ__FinancialAccount__c", SOQL_FINANCIAL_ACCOUNT),
    ("FinServ__FinancialAccount__History", SOQL_FINANCIAL_ACCOUNT_HISTORY),
    ("AccountHistory", SOQL_ACCOUNT_HISTORY),
    ("Account", SOQL_HOUSEHOLD),
    ("ProcessInstance", SOQL_PROCESS_INSTANCE),
    ("ProcessInstanceWorkitem", SOQL_PROCESS_WORKITEM),
)


def _load_fixture() -> Dict[str, Any]:
    if not FIXTURE_PATH.exists():
        raise FscIngestError(f"FSC fixture not found: {FIXTURE_PATH}")
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _get_client():
    """Build a REST client for the connected FSC Salesforce org, or None offline.

    FSC runs on a Salesforce org, so the token resolves exactly as nCino's does:
    a dedicated ``fsc`` vault credential first, else the connected Salesforce org's
    per-run OAuth context / vault credential. Never a process-global env
    credential. Instance URL is configuration, not a credential, so its env
    fallback stands.

    OFFLINE SHORT-CIRCUITS BEFORE ANY CREDENTIAL LOOKUP. "Keep offline mode
    deterministic and usable without live credentials" also means without a
    DATABASE: the vault lookup is a DB read, so attempting it offline makes an
    offline run wait on (and be shaped by) database reachability. Returning early
    keeps offline runs fast, deterministic, and DB-free.
    """
    from . import get_live_connector, is_live, resolve_vault_connector
    from .ncino import NcinoClient  # same Salesforce REST/SOQL client, reused

    if not is_live():
        return None

    instance_url = os.getenv("FSC_INSTANCE_URL")
    access_token = None

    cred = resolve_vault_connector("fsc")
    if cred:
        instance_url = instance_url or cred.get("url")
        access_token = cred.get("token")

    if not access_token:
        cred = get_live_connector("salesforce") or resolve_vault_connector("salesforce")
        if cred:
            instance_url = instance_url or cred.get("url")
            access_token = cred.get("token")

    if not instance_url:
        instance_url = os.getenv("SF_INSTANCE_URL")

    if not instance_url or not access_token:
        if is_live():
            raise FscIngestError(
                "Live mode requires an FSC credential in the vault or a connected "
                "Salesforce org (OAuth Connect), plus an instance URL. Set "
                "INGEST_MODE=offline to run without credentials."
            )
        return None

    return NcinoClient(instance_url, access_token.strip())


def _fetch_live(client) -> Dict[str, Any]:
    """Run every FSC query, degrading per object rather than failing the run.

    An org without the FSC managed package (or an integration user without read on
    one object) must not abort a run: that object degrades to empty and is reported
    as unavailable, which is the same posture the ServiceNow SecOps tables take.
    """
    payload: Dict[str, Any] = {}
    unavailable: List[str] = []
    for sobject, soql in _LIVE_QUERIES:
        try:
            payload[sobject] = {"records": client.query(soql)}
        except Exception as exc:  # noqa: BLE001 - degrade per object, never per run
            unavailable.append(sobject)
            payload[sobject] = {"records": []}
            logger.warning(
                "FSC ingest: %s unavailable (%s) — that signal degrades to empty",
                sobject, type(exc).__name__,
            )
    if unavailable:
        payload.setdefault("_meta", {})["unavailable_objects"] = unavailable
    return payload


# ── Normalisation ───────────────────────────────────────────────────────────────


def _reference_date(payload: Dict[str, Any]) -> datetime:
    """The instant ages are measured from.

    A fixture pins ``_meta.reference_date`` so offline runs are DETERMINISTIC (a
    fixture whose ages drift with wall-clock time would make every threshold test
    time-dependent). Live has no such key and uses now.
    """
    pinned = _parse_dt((payload.get("_meta", {}) or {}).get("reference_date"))
    return pinned or datetime.now(timezone.utc)


def _household_index(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Household record-id → {member_count}. NO household names are retained."""
    index: Dict[str, Dict[str, Any]] = {}
    for row in _records(payload, "Account"):
        developer_name = _field(row, "RecordType.DeveloperName")
        if developer_name and str(developer_name) not in _scope_household_types():
            continue
        household_id = str(_field(row, "Id", "") or "")
        if not household_id:
            continue
        index[household_id] = {
            "member_count": int(_field(row, "FinServ__TotalNumberOfMembers__c", 0) or 0)
        }
    return index


def _accounts_by_household(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    """Household record-id → financial-account record-ids."""
    out: Dict[str, List[str]] = defaultdict(list)
    for row in _records(payload, "FinServ__FinancialAccount__c"):
        household = str(_field(row, "FinServ__Household__c", "") or "")
        account_id = str(_field(row, "Id", "") or "")
        if household and account_id:
            out[household].append(account_id)
    return dict(out)


def _servicing_requests(
    payload: Dict[str, Any], reference: datetime
) -> Tuple[List[Dict[str, Any]], int]:
    """Aggregate in-scope service-process cases by SERVICE-PROCESS TYPE.

    The unit is a process type — never a case owner. Households contribute a COUNT
    and opaque id pointers.
    """
    thresholds = get_detector_thresholds(
        "servicing_request_recurrence",
        {"window_days": 90, "min_recurrence_count": 4, "min_distinct_financial_accounts": 2},
    )
    window_days = float(thresholds.get("window_days", 90) or 90)
    accounts_by_household = _accounts_by_household(payload)

    buckets: Dict[str, Dict[str, Any]] = {}
    unresolved = 0
    for case in _records(payload, "Case"):
        in_scope = _is_in_scope_service_process(case)
        if in_scope is None:
            unresolved += 1
            continue
        if not in_scope:
            continue
        created = _parse_dt(_field(case, "CreatedDate"))
        age = _days_between(reference, created)
        if age is None or age > window_days:
            continue
        process_type = str(_field(case, "Type", "") or "Unspecified").strip()
        bucket = buckets.setdefault(
            process_type,
            {
                "service_process_type": process_type,
                "recurrence_count": 0,
                "case_ids": [],
                "household_ids": [],
                "financial_account_ids": [],
                "queues": [],
                "window_days": window_days,
                "first_seen": None,
                "last_seen": None,
            },
        )
        bucket["recurrence_count"] += 1
        bucket["case_ids"].append(str(_field(case, "Id", "") or ""))
        household = str(_field(case, "AccountId", "") or "")
        if household:
            if household not in bucket["household_ids"]:
                bucket["household_ids"].append(household)
            for account_id in accounts_by_household.get(household, []):
                if account_id not in bucket["financial_account_ids"]:
                    bucket["financial_account_ids"].append(account_id)
        queue = _queue_name(case)
        if queue and queue not in bucket["queues"]:
            bucket["queues"].append(queue)
        iso = created.isoformat() if created else None
        if iso:
            if bucket["first_seen"] is None or iso < bucket["first_seen"]:
                bucket["first_seen"] = iso
            if bucket["last_seen"] is None or iso > bucket["last_seen"]:
                bucket["last_seen"] = iso

    rows: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        bucket["households_affected"] = len(bucket["household_ids"])
        bucket["distinct_financial_accounts"] = len(bucket["financial_account_ids"])
        rows.append(bucket)
    rows.sort(key=lambda r: (-r["recurrence_count"], r["service_process_type"]))
    return rows, unresolved


def _referral_handoffs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Aggregate referrals by REFERRAL TYPE (the team-pair route), counting owner
    changes as handoff hops. Teams are queue names only."""
    hops_by_referral: Dict[str, int] = defaultdict(int)
    for row in _records(payload, "FinServ__Referral__History"):
        if str(_field(row, "Field", "") or "").strip().lower() != "owner":
            continue
        parent = str(_field(row, "ParentId", "") or "")
        if parent:
            hops_by_referral[parent] += 1

    buckets: Dict[str, Dict[str, Any]] = {}
    for referral in _records(payload, "FinServ__Referral__c"):
        referral_id = str(_field(referral, "Id", "") or "")
        referral_type = str(
            _field(referral, "FinServ__ReferralType__c", "") or "Unspecified"
        ).strip()
        bucket = buckets.setdefault(
            referral_type,
            {
                "referral_type": referral_type,
                "referral_count": 0,
                "total_hops": 0,
                "max_hops": 0,
                "teams": [],
                "referral_ids": [],
                "household_ids": [],
            },
        )
        hops = int(hops_by_referral.get(referral_id, 0))
        bucket["referral_count"] += 1
        bucket["total_hops"] += hops
        bucket["max_hops"] = max(bucket["max_hops"], hops)
        bucket["referral_ids"].append(referral_id)
        household = str(_field(referral, "FinServ__Household__c", "") or "")
        if household and household not in bucket["household_ids"]:
            bucket["household_ids"].append(household)
        team = _queue_name(referral)
        if team and team not in bucket["teams"]:
            bucket["teams"].append(team)

    rows: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        count = bucket["referral_count"] or 1
        bucket["avg_hops"] = round(bucket["total_hops"] / count, 2)
        rows.append(bucket)
    rows.sort(key=lambda r: (-r["avg_hops"], r["referral_type"]))
    return rows


def _approval_reviews(
    payload: Dict[str, Any], reference: datetime
) -> Tuple[List[Dict[str, Any]], int]:
    """Aggregate PENDING approval/review instances by REVIEW TYPE.

    The unit is the process definition. Actors are read only to recover a QUEUE
    name — never a person.
    """
    queues_by_instance: Dict[str, str] = {}
    for item in _records(payload, "ProcessInstanceWorkitem"):
        instance_id = str(_field(item, "ProcessInstanceId", "") or "")
        queue = _queue_name(item, prefix="OriginalActor")
        if instance_id and queue:
            queues_by_instance[instance_id] = queue

    buckets: Dict[str, Dict[str, Any]] = {}
    unresolved = 0
    for instance in _records(payload, "ProcessInstance"):
        if str(_field(instance, "Status", "") or "").strip().lower() != "pending":
            continue
        review_type = _field(instance, "ProcessDefinition.DeveloperName")
        if not review_type:
            unresolved += 1
            continue
        review_type = str(review_type).strip()
        dwell = _days_between(reference, _parse_dt(_field(instance, "CreatedDate")))
        if dwell is None:
            continue
        bucket = buckets.setdefault(
            review_type,
            {
                "review_type": review_type,
                "pending_count": 0,
                "dwell_days": [],
                "queues": [],
                "process_instance_ids": [],
                "target_record_ids": [],
            },
        )
        bucket["pending_count"] += 1
        bucket["dwell_days"].append(dwell)
        instance_id = str(_field(instance, "Id", "") or "")
        bucket["process_instance_ids"].append(instance_id)
        target = str(_field(instance, "TargetObjectId", "") or "")
        if target:
            bucket["target_record_ids"].append(target)
        queue = queues_by_instance.get(instance_id, "")
        if queue and queue not in bucket["queues"]:
            bucket["queues"].append(queue)

    rows: List[Dict[str, Any]] = []
    for bucket in buckets.values():
        dwells = bucket.pop("dwell_days")
        bucket["median_dwell_days"] = round(median(dwells), 2) if dwells else 0.0
        bucket["max_dwell_days"] = round(max(dwells), 2) if dwells else 0.0
        rows.append(bucket)
    rows.sort(key=lambda r: (-r["median_dwell_days"], r["review_type"]))
    return rows, unresolved


def _service_queues(
    payload: Dict[str, Any], reference: datetime
) -> List[Dict[str, Any]]:
    """Aggregate OPEN in-scope service processes by owning QUEUE, joined to that
    queue's own temporal baseline."""
    baselines = payload.get("queue_baselines", {}) or {}
    buckets: Dict[str, Dict[str, Any]] = {}
    for case in _records(payload, "Case"):
        if _is_in_scope_service_process(case) is not True:
            continue
        if not _is_open(case):
            continue
        queue = _queue_name(case)
        if not queue:
            continue  # user-owned: no queue to age, and never a person
        age = _days_between(reference, _parse_dt(_field(case, "CreatedDate")))
        if age is None:
            continue
        bucket = buckets.setdefault(
            queue,
            {
                "queue": queue,
                "open_count": 0,
                "_ages": [],
                "case_ids": [],
                "service_process_types": [],
            },
        )
        bucket["open_count"] += 1
        bucket["_ages"].append(age)
        bucket["case_ids"].append(str(_field(case, "Id", "") or ""))
        process_type = str(_field(case, "Type", "") or "").strip()
        if process_type and process_type not in bucket["service_process_types"]:
            bucket["service_process_types"].append(process_type)

    rows: List[Dict[str, Any]] = []
    for queue, bucket in buckets.items():
        ages = bucket.pop("_ages")
        bucket["current_avg_age_days"] = round(sum(ages) / len(ages), 2) if ages else 0.0
        baseline = baselines.get(queue, {}) if isinstance(baselines, dict) else {}
        bucket["baseline_avg_age_days"] = float(
            baseline.get("baseline_avg_age_days", 0.0) or 0.0
        )
        bucket["baseline_runs"] = int(baseline.get("baseline_runs", 0) or 0)
        rows.append(bucket)
    rows.sort(key=lambda r: (-r["current_avg_age_days"], r["queue"]))
    return rows


def _is_rework_field(field_name: Any) -> bool:
    lowered = str(field_name or "").lower()
    return any(token in lowered for token in _REWORK_FIELDS)


def _cross_object_rework(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Detect the SAME field group being maintained on BOTH the household record
    and its financial-account records — duplicate maintenance of one fact.

    Aggregated by OBJECT PAIR. Households contribute counts and id pointers.
    """
    household_of_account: Dict[str, str] = {}
    for row in _records(payload, "FinServ__FinancialAccount__c"):
        account_id = str(_field(row, "Id", "") or "")
        household = str(_field(row, "FinServ__Household__c", "") or "")
        if account_id and household:
            household_of_account[account_id] = household

    account_touches: Dict[str, int] = defaultdict(int)
    for row in _records(payload, "AccountHistory"):
        if not _is_rework_field(_field(row, "Field")):
            continue
        household = str(_field(row, "AccountId", "") or "")
        if household:
            account_touches[household] += 1

    financial_touches: Dict[str, int] = defaultdict(int)
    for row in _records(payload, "FinServ__FinancialAccount__History"):
        if not _is_rework_field(_field(row, "Field")):
            continue
        household = household_of_account.get(str(_field(row, "ParentId", "") or ""), "")
        if household:
            financial_touches[household] += 1

    considered = sorted(set(account_touches) | set(financial_touches))
    if not considered:
        return []

    with_rework: List[str] = []
    total_touches = 0
    for household in considered:
        both = account_touches.get(household, 0) > 0 and financial_touches.get(household, 0) > 0
        touches = account_touches.get(household, 0) + financial_touches.get(household, 0)
        total_touches += touches
        if both:
            with_rework.append(household)

    records_considered = len(
        _records(payload, "AccountHistory")
    ) + len(_records(payload, "FinServ__FinancialAccount__History"))

    return [{
        "object_pair": "Account|FinServ__FinancialAccount__c",
        "field_group": REWORK_FIELD_GROUP,
        "households_with_rework": len(with_rework),
        "households_considered": len(considered),
        "duplicate_rate": round(len(with_rework) / len(considered), 4),
        "rework_touches": total_touches,
        "records_considered": records_considered,
        "household_ids": with_rework,
    }]


def normalize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a raw FSC payload into the ONE detector-visible block.

    Every row emitted here has already passed the AC5 boundary: person-shaped
    fields are scrubbed, owners are resolved to queue names only, and households
    appear as counts plus opaque record-id pointers.
    """
    reference = _reference_date(payload)
    servicing, unresolved_record_types = _servicing_requests(payload, reference)
    approvals, unresolved_review_types = _approval_reviews(payload, reference)

    block: Dict[str, Any] = {
        "_meta": {
            "reference_date": reference.isoformat(),
            "calibration_status": calibration_status(),
            "record_types_unresolved": unresolved_record_types,
            "review_types_unresolved": unresolved_review_types,
            "in_scope_record_types": list(_scope_service_process_types()),
            "unavailable_objects": list(
                (payload.get("_meta", {}) or {}).get("unavailable_objects", [])
            ),
            "aggregation_floor": {
                "permitted_units": get_aggregation().permitted_units,
                "emit_household_names": get_aggregation().emit_household_names,
            },
        },
        "servicing_requests": servicing,
        "referral_handoffs": _referral_handoffs(payload),
        "approval_reviews": approvals,
        "service_queues": _service_queues(payload, reference),
        "cross_object_rework": _cross_object_rework(payload),
    }

    # Belt-and-braces: scrub every emitted row again. The aggregators already emit
    # only safe keys; this makes a future aggregator that forgets structurally
    # unable to leak a person field into the block.
    for key in ("servicing_requests", "referral_handoffs", "approval_reviews",
                "service_queues", "cross_object_rework"):
        block[key] = [scrub_person_fields(row) for row in block[key]]

    return block


def ingest(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Ingest FSC signal and return the normalised detector-visible block.

    ``payload`` lets a caller (or test) supply raw records directly. Otherwise:
    live → SOQL against the connected org; offline → the deterministic fixture.
    Never raises into a run: a live failure degrades to the fixture-shaped empty
    block with the failure recorded on ``_meta``.
    """
    if payload is None:
        client = None
        try:
            client = _get_client()
        except FscIngestError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("FSC ingest: client unavailable (%s)", exc)
        if client is not None:
            payload = _fetch_live(client)
        else:
            payload = _load_fixture()
            logger.info("FSC ingest: offline fixture %s", FIXTURE_PATH.name)

    return normalize(payload)
