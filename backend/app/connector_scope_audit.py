"""2.0-D4 T1 — auditing connector scope selection (pin / unpin).

D4's completeness sweep names *scope pin/unpin* by hand, and it had the largest
single hole in the trail: the seven SaaS scope routes (Slack and Teams channels,
Jira projects, Confluence spaces, SharePoint sites, GitHub repos, Salesforce
products) all persisted a selection with nothing recorded. The cloud and native-DB
scope paths already emitted ``scope_declared``, so the event type existed and a
registry-level check passed while the routes that a customer actually uses wrote
no row at all.

**Why this is a security-review question, not bookkeeping.** A scope selection
decides what AgentIQ is allowed to read for an org, for every future run. Widening
it is a data-access grant. "Who added the #finance channel, and when?" is exactly
the question a bank or federal reviewer asks, and before this the only answer was
the current value of the field.

**Why one helper rather than seven call sites.** The seven routes are structurally
identical — validate ids, assign one key on the connector record, persist. Seven
copies of an audit call would drift the moment one route changed, which is the
failure this repo has already been bitten by elsewhere (three copies of the
CI-dependency traversal, silently diverged). One helper also means the payload
shape is uniform, so a reviewer can filter ``scope_declared`` across every
connector and get comparable rows.

**Pinned and unpinned are recorded, not just the result.** A row carrying only the
new selection forces a reviewer to diff consecutive rows to learn what changed,
and cannot answer the question at all for the first write. The added and removed
ids are therefore computed here and stored alongside the resulting selection —
"unpin" is a first-class fact, matching the language D4 uses.

The emission never breaks the request: ``log_event`` already swallows write
failures by design, and the diff below is defensive so a malformed stored value
cannot raise into a route that has already persisted its change.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .middleware.audit import OUTCOME_SUCCESS, SCOPE_DECLARED, log_event

logger = logging.getLogger(__name__)

#: The connector-record key each connector stores its selection under, mapped to
#: the noun a reviewer reads. Explicit rather than derived: a scope key is part
#: of the stored contract, and guessing it from the connector id would silently
#: mislabel a row the day a connector uses a different field.
SCOPE_KINDS: Dict[str, str] = {
    "channels": "channel",
    "projects": "project",
    "spaces": "space",
    "sites": "site",
    "repos": "repository",
    "products": "product",
    "scope": "table",
    "accounts": "account",
    "subscriptions": "subscription",
}


def _as_ids(value: Any) -> List[str]:
    """Normalise a stored selection to a list of ids.

    Tolerant on purpose: a selection that has never been saved is absent, and
    some records store richer objects than bare ids. Neither should cost the
    audit row.
    """
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        if isinstance(item, dict):
            ident = item.get("id") or item.get("name") or item.get("key")
            if ident is not None:
                out.append(str(ident))
        elif item is not None:
            out.append(str(item))
    return out


def scope_delta(previous: Any, selected: Any) -> Tuple[List[str], List[str]]:
    """What was pinned and what was unpinned, in stable order.

    Order-stable rather than set-ordered so two identical changes produce
    identical rows — an audit trail whose payload reshuffles between runs is
    harder to diff and invites doubt about whether anything really changed.
    """
    before = _as_ids(previous)
    after = _as_ids(selected)
    before_set, after_set = set(before), set(after)
    pinned = [i for i in after if i not in before_set]
    unpinned = [i for i in before if i not in after_set]
    return pinned, unpinned


def audit_scope_selection(
    *,
    connector_id: str,
    scope_key: str,
    previous: Any,
    selected: Any,
    actor_id: Optional[str] = None,
    first_selection: bool = False,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Record one scope pin/unpin against ``connector_id``.

    Carries D4's five required fields: the actor (``user_id``), the org (resolved
    by ``log_event`` from request tenancy), the target (connector + scope key),
    the timestamp (stamped by ``log_event``), and the outcome.

    ``first_selection`` distinguishes "narrowed from the default" from "changed an
    existing selection". Before a selection is saved most connectors read
    everything they can see, so the first save is usually a *narrowing* even
    though every id shows as pinned — without this flag that row reads as a broad
    grant when it was the opposite.
    """
    try:
        pinned, unpinned = scope_delta(previous, selected)
        resulting = _as_ids(selected)
        payload: Dict[str, Any] = {
            "scope_key": scope_key,
            "scope_kind": SCOPE_KINDS.get(scope_key, scope_key),
            "target": f"{connector_id}:{scope_key}",
            "pinned": pinned,
            "unpinned": unpinned,
            "selected_count": len(resulting),
            "selected": resulting,
            "first_selection": bool(first_selection),
            "changed": bool(pinned or unpinned),
            "outcome": OUTCOME_SUCCESS,
        }
        if detail:
            payload.update(detail)
        log_event(
            SCOPE_DECLARED,
            connector_id=connector_id,
            user_id=actor_id,
            **payload,
        )
    except Exception as exc:  # noqa: BLE001
        # log_event already swallows write failures; this guards the diff itself.
        # A scope change that succeeded must not be reported as failed because
        # its audit payload could not be built.
        logger.warning(
            "Could not audit scope selection for %s:%s — %s",
            connector_id,
            scope_key,
            exc,
        )


__all__ = ["SCOPE_KINDS", "audit_scope_selection", "scope_delta"]
