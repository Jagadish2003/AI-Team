"""fsc_finding.py — 2.0-D1 T2: the four-part finding contract for the Financial
Services Cloud pack.

Every finding this pack emits carries all four parts (D1 T2 definition of done):

  1. evidence            — the observed numbers/facts behind the finding.
  2. confidence          — LOW/MEDIUM/HIGH plus whether it is capped and why.
  3. corroboration       — which independent sources agree, or an explicit
                           "single-source, confidence capped accordingly".
  4. source_trace        — originating systems and artifact pointers (case ids,
                           referral ids, queue names, service-process types).

This is the `cloud_ops_finding.py` / `security_ops_finding.py` contract applied to
a third pack — deliberately the same shape, so the platform-wide four-part
guarantee (Release 2.0 DoD #2) has one definition and not three dialects. It holds
no detector or scorer logic.

WHY THIS MODULE EXISTS SEPARATELY FROM cloud_ops_finding
--------------------------------------------------------
Only one thing genuinely differs, and it is the reason a shared import would be
wrong: **the aggregation floor**. `cloud_ops`' denylist is ITSM-shaped (assignee,
caller, opened_by…). FSC data is dense with an entirely different set of
person-level fields — relationship managers, advisors, household members, case
owners, `OwnedById`, `FinServ__PrimaryOwner__c` — none of which appear in the ITSM
list. Importing the ITSM denylist would have produced a test that passes while
leaking every FSC person field that matters.

AC5 IS AN ABSOLUTE
------------------
No detector output names an individual — groups, queues and processes only — and
it applies to NARRATIVE TEXT as much as to structured fields. Three defences,
because a code-review assumption is explicitly not sufficient here:

  * ``INDIVIDUAL_FIELD_DENYLIST`` — key-level, extended for FSC and Salesforce
    (any ``*Id`` owner field, ``FinServ__`` person roles).
  * value-level scanning — emails, phone numbers, and Person-Shaped Names in
    narrative strings, plus household NAMES (a household name identifies a family
    and, for a single-member household, a person).
  * ``build_finding_contract`` refuses to construct a contract that trips either,
    so a detector cannot emit one by accident, and ``enforce_pack_findings`` fails
    the run at the pack boundary if one somehow gets through.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

try:  # shared confidence vocabulary — keep in lockstep with the corroboration engine
    from backend.discovery.packs.corroboration_rules import (
        CONFIDENCE_HIGH,
        CONFIDENCE_LOW,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )
except ModuleNotFoundError:  # pragma: no cover - import shim
    from discovery.packs.corroboration_rules import (
        CONFIDENCE_HIGH,
        CONFIDENCE_LOW,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )

# The four contract parts, in order — identical to the other operational packs so
# the platform-wide guarantee has one definition.
FOUR_PART_CONTRACT_FIELDS = ("evidence", "confidence", "corroboration", "source_trace")

# Corroboration status vocabulary.
STATUS_CORROBORATED = "corroborated"
STATUS_SINGLE_SOURCE = "single_source"

SINGLE_SOURCE_LABEL = "Single-source, confidence capped accordingly"

# ── The FSC aggregation floor (AC5) ─────────────────────────────────────────────
#
# Person-level field names that must never reach a finding. The first block is the
# platform-generic set; the second is FSC/Salesforce-specific and is the reason
# this module does not simply import the cloud_ops denylist.
INDIVIDUAL_FIELD_DENYLIST = frozenset({
    # generic
    "assignee",
    "assigned_to",
    "assigned_user",
    "user",
    "user_id",
    "username",
    "user_name",
    "person",
    "individual",
    "email",
    "user_email",
    "full_name",
    "display_name",
    "first_name",
    "last_name",
    "phone",
    "mobile_phone",
    # Salesforce ownership / actor fields
    "owner",
    "ownerid",
    "owner_id",
    "ownedbyid",
    "owned_by_id",
    "createdbyid",
    "created_by_id",
    "lastmodifiedbyid",
    "last_modified_by_id",
    "submittedbyid",
    "submitted_by_id",
    "actorid",
    "actor_id",
    "contactid",
    "contact_id",
    "case_owner",
    "opened_by",
    "closed_by",
    "resolved_by",
    "updated_by",
    # FSC person roles
    "relationship_manager",
    "relationshipmanager",
    "advisor",
    "adviser",
    "banker",
    "primary_owner",
    "primaryowner",
    "household_member",
    "householdmember",
    "member_name",
    "client_name",
    "contact_name",
    "primary_contact",
    # a household NAME identifies a family (and a single-member household, a person)
    "household_name",
    "householdname",
    "account_name",
})

# Salesforce person-reference field-name SHAPES. A managed package can introduce a
# person field this module has never seen, so the floor also rejects by shape:
# any FinServ__*Owner*/Advisor/Banker/Contact/Member field, and the standard
# *ById actor pattern.
_PERSON_FIELD_PATTERNS = (
    re.compile(r"^finserv__.*(owner|advisor|adviser|banker|contact|member|person)", re.I),
    re.compile(r"by_?id$", re.I),
    re.compile(r"^(owner|assignee|contact)_?(id|name)?$", re.I),
)

_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE_RE = re.compile(r"(?:\+?\d[\s\-().]{0,2}){9,}\d")

# "Jane Advisor", "Smith Household" — two or more consecutive Title-Case words.
#
# IMPORTANT — where this may and may not be applied. A PERMITTED unit is often
# legitimately Title-Case too ("Client Servicing Tier 1", "Compliance Review",
# "Wealth Operations" are queues, which AC5 explicitly allows). Shape alone cannot
# separate those from "Priya Raman", so this heuristic is applied ONLY to free
# NARRATIVE text, and callers pass the permitted unit names they legitimately
# mention via ``allow=``. Applying it to every string value would flag queue names
# and make the sweep useless.
_PERSON_NAME_RE = re.compile(r"\b[A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20})+\b")

# Domain phrases that are legitimately Title-Case in narrative text.
_NAME_ALLOWLIST = (
    "Financial Services Cloud",
    "Service Cloud",
    "Salesforce Financial",
    "Financial Account",
    "Financial Accounts",
    "Service Process",
    "Service Processes",
    "Relationship Group",
    "Relationship Groups",
    "Action Plan",
    "Action Plans",
    "Service Queue",
    "Service Queues",
    "Review Cycle",
    "Wealth Management",
    "Retail Banking",
    "Single-source",
)


# Tokens that make a field an AGGREGATE OVER people rather than a person.
# `FinServ__TotalNumberOfMembers__c` is a household member COUNT — it matches the
# "...member..." person shape below, but a count is precisely what AC5 permits
# (households as counts, never as names), so it must not be treated as a person
# field. Checked AFTER the explicit denylist, so an explicitly-named person field
# can never be exempted by containing one of these.
_AGGREGATE_TOKENS = ("count", "numberof", "number_of", "total")


def is_person_field(name: Any) -> bool:
    """True when a field name denotes an individual — by name or by shape.

    False for an aggregate over individuals (a member count), which is a permitted
    form.
    """
    key = str(name).strip()
    lowered = key.lower()
    if lowered in INDIVIDUAL_FIELD_DENYLIST:
        return True
    if any(token in lowered for token in _AGGREGATE_TOKENS):
        return False
    return any(p.search(key) for p in _PERSON_FIELD_PATTERNS)


# ── Contract-part builders ──────────────────────────────────────────────────────


def build_confidence(
    level: str,
    *,
    capped: bool,
    eligible_for_high: bool,
    cap_reason: str = "",
    note: str = "",
) -> Dict[str, Any]:
    """Build the confidence part (LOW/MEDIUM/HIGH + honest cap reason)."""
    level = str(level).upper()
    if level not in CONFIDENCE_ORDER:
        raise ValueError(
            f"confidence level must be one of {sorted(CONFIDENCE_ORDER)}, got {level!r}"
        )
    return {
        "level": level,
        "capped": bool(capped),
        "eligible_for_high": bool(eligible_for_high),
        "cap_reason": cap_reason,
        "note": note,
    }


def build_corroboration(
    status: str,
    *,
    sources: Sequence[str],
    label: str,
    rule_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Build the corroboration part."""
    if status not in (STATUS_CORROBORATED, STATUS_SINGLE_SOURCE):
        raise ValueError(
            f"corroboration status must be {STATUS_CORROBORATED!r} or "
            f"{STATUS_SINGLE_SOURCE!r}, got {status!r}"
        )
    return {
        "status": status,
        "sources": list(sources),
        "label": label,
        "rule_ids": list(rule_ids or []),
    }


def build_source_trace(
    *,
    systems: Sequence[str],
    artifacts: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the source-trace part.

    ``artifacts`` are {type, id, ...} pointers to concrete source records — case
    ids, referral ids, queue names, service-process types, and opaque household
    RECORD IDS (never household names). Each artifact must carry a type and id so
    a claim can be walked back to its records.
    """
    cleaned: List[Dict[str, Any]] = []
    for a in artifacts:
        if not isinstance(a, dict) or not a.get("type") or not a.get("id"):
            raise ValueError(
                f"source_trace artifact must carry 'type' and 'id', got {a!r}"
            )
        cleaned.append(dict(a))
    return {"systems": list(systems), "artifacts": cleaned}


def build_finding_contract(
    *,
    evidence: Dict[str, Any],
    confidence: Dict[str, Any],
    corroboration: Dict[str, Any],
    source_trace: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the four-part contract, failing loudly on an incomplete or
    individual-referencing finding."""
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("finding contract 'evidence' must be a non-empty dict")
    if not any(
        isinstance(v, (int, float)) and not isinstance(v, bool)
        for v in _flatten(evidence)
    ):
        raise ValueError("finding contract 'evidence' must contain a numeric value")
    for name, part in (("confidence", confidence), ("corroboration", corroboration)):
        if not isinstance(part, dict) or not part:
            raise ValueError(f"finding contract {name!r} must be a non-empty dict")
    if not source_trace.get("systems") or not source_trace.get("artifacts"):
        raise ValueError(
            "finding contract 'source_trace' must resolve to a system and artifact"
        )

    contract = {
        "evidence": evidence,
        "confidence": confidence,
        "corroboration": corroboration,
        "source_trace": source_trace,
    }
    leaked = find_individual_references(contract)
    if leaked:
        raise ValueError(
            f"finding contract references an individual (forbidden — households, "
            f"teams, queues and service processes only): {leaked}"
        )
    return contract


def is_contract_complete(contract: Any) -> bool:
    """True when ``contract`` carries all four non-empty parts."""
    if not isinstance(contract, dict):
        return False
    return all(bool(contract.get(f)) for f in FOUR_PART_CONTRACT_FIELDS)


def missing_contract_parts(contract: Any) -> List[str]:
    """Return the four-part fields absent/empty on ``contract`` ([] when complete)."""
    if not isinstance(contract, dict):
        return list(FOUR_PART_CONTRACT_FIELDS)
    return [f for f in FOUR_PART_CONTRACT_FIELDS if not contract.get(f)]


# ── Pack-boundary enforcement ───────────────────────────────────────────────────


class FscContractViolation(ValueError):
    """An FSC finding failed the four-part contract or the aggregation floor at
    the pack boundary. Raised to FAIL the run's pack execution (never swallowed)."""


def _finding_contract_of(result: Any) -> Any:
    raw = getattr(result, "raw_evidence", None)
    if raw is None and isinstance(result, dict):
        raw = result.get("raw_evidence", result)
    if not isinstance(raw, dict):
        return None
    return raw.get("finding_contract")


def enforce_finding_contract(
    contract: Any,
    *,
    detector_id: str = "",
    index: Optional[int] = None,
) -> None:
    """Raise :class:`FscContractViolation` unless ``contract`` carries all four
    non-empty parts AND references no individual."""
    where = f"detector {detector_id!r}" if detector_id else "finding"
    if index is not None:
        where += f" (index {index})"

    if contract is None:
        raise FscContractViolation(
            f"{where} carries no four-part finding_contract — every Financial "
            f"Services Cloud finding must carry {list(FOUR_PART_CONTRACT_FIELDS)}."
        )
    missing = missing_contract_parts(contract)
    if missing:
        raise FscContractViolation(
            f"{where} is missing required contract part(s) {missing}; a finding must "
            f"carry all four: {list(FOUR_PART_CONTRACT_FIELDS)}."
        )
    leaked = find_individual_references(contract)
    if leaked:
        raise FscContractViolation(
            f"{where} references an individual (forbidden — households, teams, "
            f"queues and service processes only): {leaked}."
        )


def enforce_pack_findings(results: Sequence[Any]) -> int:
    """Enforce the four-part contract + aggregation floor across every emitted
    finding. Raises on the first violation (failing the run). Returns the count
    validated."""
    count = 0
    for i, result in enumerate(results or []):
        detector_id = str(getattr(result, "detector_id", "") or "")
        enforce_finding_contract(
            _finding_contract_of(result), detector_id=detector_id, index=i
        )
        count += 1
    return count


# ── "No individuals" guarantee — structured AND narrative (AC5) ─────────────────


# Keys whose values are free NARRATIVE text (as opposed to a domain identifier
# like `queue` or `service_process_type`). The person-name heuristic runs on these.
NARRATIVE_KEYS = frozenset({
    "statement", "description", "narrative", "summary", "note", "detail",
    "explanation", "reason_text",
})


# Artifact types in a finding's source_trace whose ids are PERMITTED UNIT NAMES —
# the things AC5 explicitly allows a finding to be about. A queue named "Client
# Servicing Tier 1" is Title-Case and would otherwise be flagged as a person's
# name in the narrative, so the sweep allows the unit names the finding itself
# declares it is about.
#
# Honest about the limit: this is an allowlist, so a detector that put a person's
# name in as {"type": "queue", "id": "..."} would be permitted to repeat it in
# narrative. The real defences against that are upstream — the ingest resolves an
# owner to a queue name ONLY when Owner.Type == 'Queue' — and downstream, in the
# test that asserts no person literal from the fixture ever reaches a finding.
PERMITTED_UNIT_ARTIFACT_TYPES = frozenset({
    "queue", "team", "service_process_type", "review_type", "referral_type",
    "object_pair", "field_group", "temporal_baseline",
})


def permitted_unit_names(contract: Any) -> List[str]:
    """Return the permitted-unit names a contract's source_trace declares."""
    names: List[str] = []
    if not isinstance(contract, dict):
        return names
    artifacts = ((contract.get("source_trace") or {}) if isinstance(
        contract.get("source_trace"), dict) else {}).get("artifacts") or []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if str(artifact.get("type", "")).lower() in PERMITTED_UNIT_ARTIFACT_TYPES:
            value = artifact.get("id")
            if isinstance(value, str) and value:
                names.append(value)
    return names


def find_individual_references(
    obj: Any, *, _path: str = "", allow: Sequence[str] = ()
) -> List[str]:
    """Recursively scan a finding/contract for any individual-person reference.

    Three checks, applied at the level each is actually sound:

      * a person-shaped KEY carrying a value — everywhere;
      * an email or phone number — in every string (no false-positive risk);
      * a person/family-shaped NAME — only in :data:`NARRATIVE_KEYS` values, and
        with the finding's own declared permitted units allowed, because a queue
        name is Title-Case too.

    Returns dotted paths to each offending location ([] when clean)."""
    allowed = tuple(allow or ())
    if not _path:
        # Top-level call on a contract: the units it declares itself to be about
        # are legitimately mentionable in its own narrative.
        allowed = allowed + tuple(permitted_unit_names(obj))

    hits: List[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{_path}.{key}" if _path else str(key)
            if is_person_field(key) and value not in (None, "", [], {}):
                hits.append(here)
            if isinstance(value, str) and str(key).lower() in NARRATIVE_KEYS:
                hits.extend(
                    f"{here} ({r})" for r in find_person_text(value, allow=allowed)
                )
            hits.extend(find_individual_references(value, _path=here, allow=allowed))
    elif isinstance(obj, (list, tuple)):
        for i, value in enumerate(obj):
            hits.extend(
                find_individual_references(value, _path=f"{_path}[{i}]", allow=allowed)
            )
    elif isinstance(obj, str):
        for reason in find_contact_details(obj):
            hits.append(f"{_path} ({reason})")
    return hits


def find_contact_details(text: str) -> List[str]:
    """Return reasons ``text`` carries direct contact details ([] when clean).

    Email and phone shapes only — safe to run over EVERY string because no
    permitted FSC unit (queue, service-process type, referral type, object pair)
    looks like either.
    """
    value = str(text)
    reasons: List[str] = []
    if _EMAIL_RE.search(value):
        reasons.append("email-like value")
    if _PHONE_RE.search(value):
        reasons.append("phone-like value")
    return reasons


def find_person_text(text: str, *, allow: Sequence[str] = ()) -> List[str]:
    """Return reasons ``text`` names an individual ([] when clean).

    The narrative half of AC5: a detector could satisfy every structured check and
    still write "Priya Raman is reassigning these" into a description. ``allow``
    carries the PERMITTED unit names the caller legitimately mentions (queues,
    service-process types) so a queue name is not mistaken for a person.
    """
    value = str(text)
    reasons = find_contact_details(value)
    scrubbed = value
    for allowed in tuple(_NAME_ALLOWLIST) + tuple(allow or ()):
        if allowed:
            scrubbed = scrubbed.replace(str(allowed), "")
    match = _PERSON_NAME_RE.search(scrubbed)
    if match:
        reasons.append(f"person-or-family-name-like value {match.group(0)!r}")
    return reasons


def assert_no_individual_references(obj: Any) -> None:
    """Raise ValueError if ``obj`` references an individual."""
    hits = find_individual_references(obj)
    if hits:
        raise ValueError(f"finding references individuals (forbidden): {hits}")


def scrub_person_fields(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``record`` with every person-shaped field removed.

    The ingest boundary uses this so person-level FSC fields are dropped at the
    point of normalisation rather than relied upon not to be read later. FSC
    records legitimately CONTAIN owners and contacts; the pack's normalised
    signal must not.
    """
    if not isinstance(record, dict):
        return {}
    return {k: v for k, v in record.items() if not is_person_field(k)}


# ── Concentration-shaped wording (causality is the causal engine's job) ─────────

CAUSAL_PHRASES = (
    "caused by", "because", "cause of", "causes", "causing", "due to",
    "root cause", "root-cause", "responsible for", "leads to", "led to",
    "results in", "resulted in", "driven by", "blamed on", "culprit",
    "triggered by", "stems from",
)


def find_causal_language(text: str) -> List[str]:
    """Return the causal phrases present in ``text`` (case-insensitive), else []."""
    lowered = str(text).lower()
    return [p for p in CAUSAL_PHRASES if p in lowered]


def assert_not_causal(text: str) -> None:
    """Raise ValueError if ``text`` asserts causation."""
    hits = find_causal_language(text)
    if hits:
        raise ValueError(
            f"finding wording is causal (forbidden — concentration-shaped only): "
            f"{hits} in {text!r}"
        )


def build_concentration_statement(
    *,
    unit_label: str,
    unit: str,
    count: int,
    measure: str,
) -> str:
    """Produce a concentration-shaped statement and self-validate it.

    Always "X concentrates on ..." — never "caused by ...". Runs the causal gate
    AND the AC5 narrative sweep over its own output (with ``unit`` allowed, since
    it is a permitted queue/process name), so the wording contract cannot regress
    silently.
    """
    statement = f"{measure} concentrates on {count} {unit_label} ({unit})."
    assert_not_causal(statement)
    person_hits = find_person_text(statement, allow=[unit, unit_label])
    if person_hits:
        raise ValueError(
            f"concentration statement names an individual ({person_hits}): {statement!r}"
        )
    return statement


# ── internal ────────────────────────────────────────────────────────────────────


def _flatten(obj: Any):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)
    else:
        yield obj
