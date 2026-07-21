"""MSP-B5 T4 persisted analyst decisions for proposed runbook matches.

The current row is convenient state; the append-only history is the audit trail.
Accept and dismiss decisions also emit small, labelled feedback records for the
adaptive-learning path.  Defer remains pending and therefore is not training
feedback.  Every key and every query includes ``org_id``.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional
from uuid import uuid4

from app import db
from discovery.detectors.runbook_composite import (
    RUNBOOK_ABSENT,
    present_runbook_match,
    presentation_for_state,
)
from discovery.detectors.runbook_match import (
    MATCH_CONFIRMED,
    MATCH_OBSERVED,
    MATCH_PROPOSED,
    RunbookMatch,
)
from discovery.signals.evidence_store import OrgScopeError

ACTION_ACCEPT = "accept"
ACTION_DISMISS = "dismiss"
ACTION_DEFER = "defer"
DECISION_ACTIONS = frozenset({ACTION_ACCEPT, ACTION_DISMISS, ACTION_DEFER})

FEEDBACK_ACCEPTED = "runbook_match_accepted"
FEEDBACK_DISMISSED = "runbook_match_dismissed"


class RunbookMatchNotFound(LookupError):
    pass


class RunbookMatchDecisionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def _resulting_state(action: str) -> str:
    return {
        ACTION_ACCEPT: MATCH_CONFIRMED,
        ACTION_DISMISS: RUNBOOK_ABSENT,
        ACTION_DEFER: MATCH_PROPOSED,
    }[action]


def _effective_match(match: RunbookMatch, state: str) -> Optional[RunbookMatch]:
    if state == RUNBOOK_ABSENT:
        return None
    return match.with_state(state)


def _feedback_features(match: RunbookMatch) -> Dict[str, Any]:
    """Only stable matching features — no note text or document content."""
    return {
        "match_origin": match.origin,
        "match_confidence": match.match_confidence,
        "runbook_source_system": match.runbook.get("source_system"),
        "runbook_source_artifact": match.runbook.get("source_artifact"),
    }


@dataclass(frozen=True)
class DecisionOutcome:
    org_id: str
    recurrence_id: str
    action: str
    previous_action: Optional[str]
    previous_state: str
    current_state: str
    revision: int
    changed: bool
    current_match: Optional[RunbookMatch]
    decided_at: str
    actor_id: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "recurrence_id": self.recurrence_id,
            "action": self.action,
            "previous_action": self.previous_action,
            "previous_state": self.previous_state,
            "current_state": self.current_state,
            "revision": self.revision,
            "changed": self.changed,
            "current_match": (
                present_runbook_match(self.current_match.as_dict())
                if self.current_match else None
            ),
            "lifecycle": presentation_for_state(self.current_state),
            "decided_at": self.decided_at,
            "actor_id": self.actor_id,
        }


class RunbookMatchDecisionStore:
    def register_match(self, match: RunbookMatch) -> None:
        raise NotImplementedError

    def decide(self, org_id: str, recurrence_id: str, action: str, actor_id: str) -> DecisionOutcome:
        raise NotImplementedError

    def current(self, org_id: str, recurrence_id: str) -> Dict[str, Any]:
        raise NotImplementedError

    def history(self, org_id: str, recurrence_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def feedback(self, org_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError


class InMemoryRunbookMatchDecisionStore(RunbookMatchDecisionStore):
    """Thread-safe contract implementation used by offline runs and unit tests."""

    def __init__(self) -> None:
        self._rows: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._history: List[Dict[str, Any]] = []
        self._feedback: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def register_match(self, match: RunbookMatch) -> None:
        if match.match_state not in {MATCH_OBSERVED, MATCH_PROPOSED}:
            raise RunbookMatchDecisionError("only observed or proposed matches may be registered")
        key = (_required(match.org_id, "org_id"), _required(match.recurrence_id, "recurrence_id"))
        now = _now()
        with self._lock:
            existing = self._rows.get(key)
            if existing is None:
                self._rows[key] = {
                    "match": match,
                    "base_state": match.match_state,
                    "current_state": match.match_state,
                    "current_action": None,
                    "revision": 0,
                    "updated_by": None,
                    "created_at": now,
                    "updated_at": now,
                }
            else:
                # A re-run may refresh the proposal's evidence.  It never erases
                # an analyst decision or its revision/history.  An explicit
                # citation is different: observed source truth supersedes a
                # prior semantic-review state, while the old history remains.
                existing["match"] = match
                existing["base_state"] = match.match_state
                if match.match_state == MATCH_OBSERVED:
                    existing["current_state"] = MATCH_OBSERVED
                    existing["current_action"] = None
                    existing["updated_by"] = None
                elif existing["current_action"] is None:
                    existing["current_state"] = match.match_state
                existing["updated_at"] = now

    def _row(self, org_id: str, recurrence_id: str) -> Dict[str, Any]:
        row = self._rows.get((_required(org_id, "org_id"), _required(recurrence_id, "recurrence_id")))
        if row is None:
            raise RunbookMatchNotFound("runbook match not found")
        return row

    def decide(self, org_id: str, recurrence_id: str, action: str, actor_id: str) -> DecisionOutcome:
        org = _required(org_id, "org_id")
        recurrence = _required(recurrence_id, "recurrence_id")
        actor = _required(actor_id, "actor_id")
        normalized = str(action or "").strip().lower()
        if normalized not in DECISION_ACTIONS:
            raise RunbookMatchDecisionError("action must be accept, dismiss, or defer")
        with self._lock:
            row = self._row(org, recurrence)
            match: RunbookMatch = row["match"]
            if row["base_state"] != MATCH_PROPOSED:
                raise RunbookMatchDecisionError("only proposed matches can be reviewed")
            previous_action = row["current_action"]
            previous_state = row["current_state"]
            if previous_action == normalized:
                return DecisionOutcome(
                    org, recurrence, normalized, previous_action, previous_state,
                    previous_state, row["revision"], False,
                    _effective_match(match, previous_state), row["updated_at"], actor,
                )

            state = _resulting_state(normalized)
            revision = int(row["revision"]) + 1
            decided_at = _now()
            history_id = f"rmdh_{uuid4().hex}"
            event = {
                "id": history_id,
                "org_id": org,
                "recurrence_id": recurrence,
                "revision": revision,
                "action": normalized,
                "previous_action": previous_action,
                "previous_state": previous_state,
                "resulting_state": state,
                "actor_id": actor,
                "decided_at": decided_at,
            }
            self._history.append(event)
            if normalized in {ACTION_ACCEPT, ACTION_DISMISS}:
                self._feedback.append({
                    "id": f"rmbf_{uuid4().hex}",
                    "decision_history_id": history_id,
                    "org_id": org,
                    "recurrence_id": recurrence,
                    "feedback_label": (
                        FEEDBACK_ACCEPTED if normalized == ACTION_ACCEPT else FEEDBACK_DISMISSED
                    ),
                    "features": _feedback_features(match),
                    "actor_id": actor,
                    "created_at": decided_at,
                })
            row.update({
                "current_action": normalized,
                "current_state": state,
                "revision": revision,
                "updated_by": actor,
                "updated_at": decided_at,
            })
            return DecisionOutcome(
                org, recurrence, normalized, previous_action, previous_state,
                state, revision, True, _effective_match(match, state), decided_at, actor,
            )

    def current(self, org_id: str, recurrence_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._row(org_id, recurrence_id)
            match: RunbookMatch = row["match"]
            effective = _effective_match(match, row["current_state"])
            return {
                "org_id": _required(org_id, "org_id"),
                "recurrence_id": _required(recurrence_id, "recurrence_id"),
                "base_state": row["base_state"],
                "current_state": row["current_state"],
                "current_action": row["current_action"],
                "revision": row["revision"],
                "current_match": (
                    present_runbook_match(effective.as_dict()) if effective else None
                ),
                "lifecycle": presentation_for_state(row["current_state"]),
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            }

    def history(self, org_id: str, recurrence_id: str) -> List[Dict[str, Any]]:
        org = _required(org_id, "org_id")
        recurrence = _required(recurrence_id, "recurrence_id")
        return [
            dict(event) for event in reversed(self._history)
            if event["org_id"] == org and event["recurrence_id"] == recurrence
        ]

    def feedback(self, org_id: str) -> List[Dict[str, Any]]:
        org = _required(org_id, "org_id")
        return [dict(event) for event in self._feedback if event["org_id"] == org]


class PostgresRunbookMatchDecisionStore(RunbookMatchDecisionStore):
    """Production store.  Migrations provision its three tables."""

    def register_match(self, match: RunbookMatch) -> None:
        if match.match_state not in {MATCH_OBSERVED, MATCH_PROPOSED}:
            raise RunbookMatchDecisionError("only observed or proposed matches may be registered")
        org = _required(match.org_id, "org_id")
        recurrence = _required(match.recurrence_id, "recurrence_id")
        now = _now()
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                INSERT INTO runbook_matches
                    (org_id, recurrence_id, base_state, current_state, current_action,
                     match_payload, revision, updated_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, NULL, %s, 0, NULL, %s, %s)
                ON CONFLICT (org_id, recurrence_id) DO UPDATE SET
                    base_state = EXCLUDED.base_state,
                    match_payload = EXCLUDED.match_payload,
                    current_state = CASE
                        WHEN EXCLUDED.base_state = 'observed' THEN 'observed'
                        WHEN runbook_matches.current_action IS NULL THEN EXCLUDED.current_state
                        ELSE runbook_matches.current_state
                    END,
                    current_action = CASE
                        WHEN EXCLUDED.base_state = 'observed' THEN NULL
                        ELSE runbook_matches.current_action
                    END,
                    updated_by = CASE
                        WHEN EXCLUDED.base_state = 'observed' THEN NULL
                        ELSE runbook_matches.updated_by
                    END,
                    updated_at = EXCLUDED.updated_at
                """,
                (org, recurrence, match.match_state, match.match_state,
                 json.dumps(match.as_dict()), now, now),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    @staticmethod
    def _match(row: Mapping[str, Any]) -> RunbookMatch:
        payload = row["match_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return RunbookMatch.from_dict(payload)

    def decide(self, org_id: str, recurrence_id: str, action: str, actor_id: str) -> DecisionOutcome:
        org = _required(org_id, "org_id")
        recurrence = _required(recurrence_id, "recurrence_id")
        actor = _required(actor_id, "actor_id")
        normalized = str(action or "").strip().lower()
        if normalized not in DECISION_ACTIONS:
            raise RunbookMatchDecisionError("action must be accept, dismiss, or defer")

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT * FROM runbook_matches WHERE org_id = %s AND recurrence_id = %s FOR UPDATE",
                (org, recurrence),
            )
            row = cur.fetchone()
            if row is None:
                raise RunbookMatchNotFound("runbook match not found")
            match = self._match(row)
            if row["base_state"] != MATCH_PROPOSED:
                raise RunbookMatchDecisionError("only proposed matches can be reviewed")
            previous_action = row["current_action"]
            previous_state = row["current_state"]
            if previous_action == normalized:
                con.commit()
                return DecisionOutcome(
                    org, recurrence, normalized, previous_action, previous_state,
                    previous_state, int(row["revision"]), False,
                    _effective_match(match, previous_state), str(row["updated_at"]), actor,
                )

            state = _resulting_state(normalized)
            revision = int(row["revision"]) + 1
            decided_at = _now()
            history_id = f"rmdh_{uuid4().hex}"
            cur.execute(
                """
                UPDATE runbook_matches SET current_state = %s, current_action = %s,
                    revision = %s, updated_by = %s, updated_at = %s
                WHERE org_id = %s AND recurrence_id = %s
                """,
                (state, normalized, revision, actor, decided_at, org, recurrence),
            )
            cur.execute(
                """
                INSERT INTO runbook_match_decision_history
                    (id, org_id, recurrence_id, revision, action, previous_action,
                     previous_state, resulting_state, actor_id, decided_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (history_id, org, recurrence, revision, normalized, previous_action,
                 previous_state, state, actor, decided_at),
            )
            if normalized in {ACTION_ACCEPT, ACTION_DISMISS}:
                cur.execute(
                    """
                    INSERT INTO runbook_match_feedback
                        (id, decision_history_id, org_id, recurrence_id,
                         feedback_label, features_payload, actor_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"rmbf_{uuid4().hex}", history_id, org, recurrence,
                        FEEDBACK_ACCEPTED if normalized == ACTION_ACCEPT else FEEDBACK_DISMISSED,
                        json.dumps(_feedback_features(match)), actor, decided_at,
                    ),
                )
            con.commit()
            return DecisionOutcome(
                org, recurrence, normalized, previous_action, previous_state,
                state, revision, True, _effective_match(match, state), decided_at, actor,
            )
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def current(self, org_id: str, recurrence_id: str) -> Dict[str, Any]:
        org = _required(org_id, "org_id")
        recurrence = _required(recurrence_id, "recurrence_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT org_id, recurrence_id, base_state, current_state,
                       current_action, match_payload, revision, updated_by, updated_at
                FROM runbook_matches WHERE org_id = %s AND recurrence_id = %s
                """,
                (org, recurrence),
            )
            row = cur.fetchone()
            if row is None:
                raise RunbookMatchNotFound("runbook match not found")
            match = self._match(row)
            effective = _effective_match(match, row["current_state"])
            return {
                "org_id": org,
                "recurrence_id": recurrence,
                "base_state": row["base_state"],
                "current_state": row["current_state"],
                "current_action": row["current_action"],
                "revision": int(row["revision"]),
                "current_match": (
                    present_runbook_match(effective.as_dict()) if effective else None
                ),
                "lifecycle": presentation_for_state(row["current_state"]),
                "updated_by": row["updated_by"],
                "updated_at": str(row["updated_at"]),
            }
        finally:
            con.close()

    def history(self, org_id: str, recurrence_id: str) -> List[Dict[str, Any]]:
        org = _required(org_id, "org_id")
        recurrence = _required(recurrence_id, "recurrence_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, org_id, recurrence_id, revision, action, previous_action,
                       previous_state, resulting_state, actor_id, decided_at
                FROM runbook_match_decision_history
                WHERE org_id = %s AND recurrence_id = %s
                ORDER BY revision DESC
                """,
                (org, recurrence),
            )
            return [dict(row) for row in cur.fetchall()]
        finally:
            con.close()

    def feedback(self, org_id: str) -> List[Dict[str, Any]]:
        org = _required(org_id, "org_id")
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                """
                SELECT id, decision_history_id, org_id, recurrence_id,
                       feedback_label, features_payload, actor_id, created_at
                FROM runbook_match_feedback WHERE org_id = %s
                ORDER BY created_at, id
                """,
                (org,),
            )
            result = []
            for row in cur.fetchall():
                item = dict(row)
                payload = item.pop("features_payload")
                item["features"] = json.loads(payload) if isinstance(payload, str) else payload
                result.append(item)
            return result
        finally:
            con.close()


_STORE: Optional[RunbookMatchDecisionStore] = None


def get_runbook_match_decision_store() -> RunbookMatchDecisionStore:
    global _STORE
    if _STORE is None:
        _STORE = PostgresRunbookMatchDecisionStore()
    return _STORE


def set_runbook_match_decision_store(store: Optional[RunbookMatchDecisionStore]) -> None:
    """Test/offline injection seam; ``None`` restores the production store."""
    global _STORE
    _STORE = store


def build_current_runbook_composite(
    org_id: str,
    recurrence: Any,
    *,
    runbook_match: Optional[RunbookMatch],
    retrieval_status: str,
    manual: bool = True,
    manual_evidence: tuple[Mapping[str, Any], ...] = (),
    store: Optional[RunbookMatchDecisionStore] = None,
):
    """Persist a detected match and build B6 from its current lifecycle state.

    This is the production integration seam for B6.  A proposal is registered
    once, then every later materialisation reads the analyst's current decision.
    Dismissed matches therefore disappear from active consideration; accepted
    matches reappear as confirmed; deferred matches remain proposed.
    """
    from discovery.detectors.runbook_composite import (
        build_documented_repeated_manual_composite,
    )

    org = _required(org_id, "org_id")
    current_match = runbook_match
    decision_store = store or get_runbook_match_decision_store()
    recurrence_id = (
        str(recurrence.get("record_id") or recurrence.get("recurrence_id") or "").strip()
        if isinstance(recurrence, Mapping)
        else str(getattr(recurrence, "record_id", "") or "").strip()
    )
    if runbook_match is not None:
        if runbook_match.org_id != org:
            raise OrgScopeError("runbook match belongs to another organization")
        decision_store.register_match(runbook_match)
        recurrence_id = runbook_match.recurrence_id
    if recurrence_id:
        try:
            snapshot = decision_store.current(org, recurrence_id)
        except RunbookMatchNotFound:
            snapshot = None
        if snapshot is not None:
            payload = snapshot.get("current_match")
            current_match = RunbookMatch.from_dict(payload) if payload else None

    return build_documented_repeated_manual_composite(
        org,
        recurrence,
        runbook_match=current_match,
        retrieval_status=retrieval_status,
        manual=manual,
        manual_evidence=manual_evidence,
    )
