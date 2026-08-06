"""2.0-A3 T2 — the bounded ranking adjustment. A layer, not an edit.

**The defining property.** Base scoring is untouched and always recoverable.
``discovery/scorer.py`` (``_compute_impact`` / ``_rescale_impact``) learns
nothing and does not change; neither does ``cloud_ops_scorer``'s
``ops_impact_rank``. Those produce the base score. This module sits ABOVE it and
reorders, and it never writes a finding's ``impact``, ``effort``, ``tier``,
``confidence``, ``evidence`` or corroboration — a structural test
(``tests/unit/test_learning_signal_isolation.py``) fails the build if any module
in this layer contains an assignment to one of those fields.

Because the layer is applied at SERVE time over stored findings rather than
written into them, "what would this have ranked without learning?" is answerable
by definition: the stored order IS the base order, and every served finding
carries its ``baseRank`` and ``baseImpact`` alongside its adjusted position.
Turning the layer off restores base order exactly, with nothing to undo.

**One application point.** Ordering is currently decided in several places —
``scorer.py`` produces base impact, ``roadmap_engine.build_roadmap`` decides
stage membership then applies A1 T4's capped-confidence demotion,
``cloud_ops_scorer`` computes its own pack rank, and ``main.list_opportunities``
serves stored order. A learned adjustment added to more than one of those would
compound into unexplainable movement, so :func:`adjust_ranking` is the only
adjustment function and every presentation surface routes through it. The base
scorers are deliberately NOT application points: they produce the thing being
adjusted.

**It narrows an existing order; it does not replace one.** This follows A1 T4's
precedent exactly (``roadmap_engine._apply_projection_strength_rule`` →
``demote_capped_projections``): the incoming order already encodes deliberate
decisions — tier placement, approved-before-unreviewed, capped-confidence
demotion — and learning has no business overturning them. Items with no learned
adjustment keep their relative order; only the learned component moves things,
within the cap.

**Why the rank cap needs its own enforcement.** Capping each item's delta at N
does not bound how far items actually MOVE: an item can be passively displaced
by others jumping over it, by up to roughly 2N. A cap applied to the sort key
but not to the outcome reads as enforced while permitting double the promised
movement — the subtler cousin of the cap that lives only in a config file. So
placement uses a bounded-window algorithm (:func:`_bounded_placement`) that
guarantees ``|adjusted_rank - base_rank| <= max_rank_move`` by construction, and
the tests assert that bound over real output, not over intent.

**Clipping is recorded, never silent.** When the cap prevents the full move, the
finding's adjustment record says so and carries what learning wanted. That case
is the interesting one: it means the learned signal and the base scorer are in
genuine tension, and a constrained result should say it was constrained (A2's
posture, applied here).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .learning_signal_config import AdjustmentPolicy, LearningSignalConfig, load_config

logger = logging.getLogger(__name__)

ADJUSTMENT_SCHEMA_VERSION = "1.0.0"

#: Why the layer applied nothing. Recorded and served, so "learning is off" is
#: never indistinguishable from "learning had nothing to say".
NOT_APPLIED_DISABLED = "disabled_by_config"
NOT_APPLIED_COLD_START = "learning_not_yet_active"
NOT_APPLIED_NO_STATE = "no_adjustment_state"
NOT_APPLIED_EMPTY = "nothing_to_order"

#: Which cap bound an adjustment, when one did.
CAPPED_BY_SCORE_FRACTION = "score_fraction"
CAPPED_BY_RANK_MOVE = "rank_move"

#: The annotation key each served finding carries. Additive and namespaced: no
#: existing field is touched.
RANKING_FIELD = "_ranking"

#: What an emitted rank is RELATIVE TO. A rank is an index into the list this
#: function was handed, so the SAME finding legitimately holds different
#: ``baseRank`` values depending on the caller: the roadmap adjusts each stage
#: separately (stage-local ranks) while the run-scoped surfaces adjust one flat
#: list (run-global ranks). Both are correct; comparing them is not. Serving the
#: scope beside the rank is what stops "moved N places" being read against the
#: wrong denominator.
RANK_SCOPE_RUN = "run"
RANK_SCOPE_ROADMAP_STAGE = "roadmap_stage"


# --------------------------------------------------------------------------
# What the layer learned about one finding type
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupAdjustment:
    """The stored, per-org adjustment for one similarity group.

    Produced from T1's signal set and PERSISTED (see
    ``learning_adjustment_state.py``) rather than derived at read time. Deriving
    it on the fly would be cheaper, but a customer's ranking would then change
    silently as history accrued, with no record of what was applied when — which
    makes T4's audit and reset impossible to answer honestly.
    """

    detector_id: Optional[str]
    pack_id: Optional[str]
    net_weight: float
    outcome_weight: float = 0.0
    decision_weight: float = 0.0
    has_outcome_evidence: bool = False
    signal_count: int = 0
    contributing_refs: Tuple[Dict[str, Any], ...] = ()

    @property
    def key(self) -> Tuple[Optional[str], Optional[str]]:
        return (self.detector_id, self.pack_id)


# --------------------------------------------------------------------------
# What the layer did to one finding
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OpportunityAdjustment:
    """One finding's movement, and everything needed to explain it."""

    opportunity_id: Optional[str]
    opportunity_identity: Optional[str]
    detector_id: Optional[str]
    pack_id: Optional[str]
    base_rank: int
    adjusted_rank: int
    base_impact: float
    #: What learning asked for, before any cap.
    requested_delta: float
    #: What survived the score cap.
    applied_delta: float
    #: Positions requested, after the score cap and before the rank cap.
    requested_rank_delta: int
    net_weight: float
    has_outcome_evidence: bool
    signal_count: int
    capped_by: Optional[str] = None
    contributing_refs: Tuple[Dict[str, Any], ...] = ()

    @property
    def moved(self) -> int:
        """Actual positions moved. Negative means it moved up the list."""
        return self.adjusted_rank - self.base_rank

    @property
    def was_capped(self) -> bool:
        return self.capped_by is not None

    @property
    def effective_impact(self) -> float:
        """Base impact plus the applied delta. The base score is not modified."""
        return round(self.base_impact + self.applied_delta, 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": ADJUSTMENT_SCHEMA_VERSION,
            "opportunityId": self.opportunity_id,
            "opportunityIdentity": self.opportunity_identity,
            "detectorId": self.detector_id,
            "packId": self.pack_id,
            "baseRank": self.base_rank,
            "adjustedRank": self.adjusted_rank,
            "moved": self.moved,
            "baseImpact": round(self.base_impact, 4),
            "effectiveImpact": self.effective_impact,
            "requestedDelta": round(self.requested_delta, 4),
            "appliedDelta": round(self.applied_delta, 4),
            "requestedRankDelta": self.requested_rank_delta,
            "netWeight": round(self.net_weight, 4),
            "hasOutcomeEvidence": self.has_outcome_evidence,
            "signalCount": self.signal_count,
            "wasCapped": self.was_capped,
            "cappedBy": self.capped_by,
            "contributingRefs": [dict(r) for r in self.contributing_refs],
            # 2.0-A3 T3 — the structured reason travels with the record, so the
            # inspection surfaces and the served finding render identical
            # wording rather than each composing their own.
            "reason": self._reason_dict(),
        }

    def _reason_dict(self) -> Optional[Dict[str, Any]]:
        try:
            from .learning_reason import describe_adjustment

            return describe_adjustment(self)
        except Exception as exc:  # noqa: BLE001 - a reason is never fatal
            logger.warning("Could not build adjustment reason: %s", exc)
            return None


@dataclass(frozen=True)
class AdjustedRanking:
    """The reordered list plus the full record of what was done to it."""

    ordered: Tuple[Mapping[str, Any], ...]
    adjustments: Tuple[OpportunityAdjustment, ...]
    applied: bool
    reason: Optional[str] = None
    policy: Optional[AdjustmentPolicy] = None

    @property
    def moved_count(self) -> int:
        return sum(1 for a in self.adjustments if a.moved != 0)

    @property
    def capped_count(self) -> int:
        return sum(1 for a in self.adjustments if a.was_capped)

    @property
    def max_movement(self) -> int:
        return max((abs(a.moved) for a in self.adjustments), default=0)

    def by_opportunity_id(self) -> Dict[str, OpportunityAdjustment]:
        return {a.opportunity_id: a for a in self.adjustments if a.opportunity_id}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": ADJUSTMENT_SCHEMA_VERSION,
            "applied": self.applied,
            "reason": self.reason,
            "movedCount": self.moved_count,
            "cappedCount": self.capped_count,
            "maxMovement": self.max_movement,
            "caps": {
                "maxScoreFraction": self.policy.max_score_fraction if self.policy else None,
                "maxRankMove": self.policy.max_rank_move if self.policy else None,
            },
            "adjustments": [a.to_dict() for a in self.adjustments],
        }


# --------------------------------------------------------------------------
# Reading a finding without assuming a shape
# --------------------------------------------------------------------------


def _norm(value: Any) -> Optional[str]:
    text = str(value).strip().lower() if value is not None else ""
    return text or None


def _detector_of(opp: Mapping[str, Any]) -> Optional[str]:
    debug = opp.get("_debug")
    if isinstance(debug, Mapping) and debug.get("detector_id"):
        return _norm(debug.get("detector_id"))
    return _norm(opp.get("detector_id") or opp.get("detectorId"))


def _base_impact_of(opp: Mapping[str, Any]) -> float:
    """The base score, read and never written.

    Falls back to the midpoint of the 1-10 scale rather than zero when a finding
    carries no impact: a zero would give the finding a zero score cap, silently
    exempting it from adjustment for what is a data gap rather than a decision.
    """
    for key in ("impact", "baseImpact"):
        value = opp.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 5.0


# --------------------------------------------------------------------------
# The bounded placement — where the rank cap is actually enforced
# --------------------------------------------------------------------------


def _bounded_placement(
    keys: Sequence[Tuple[float, ...]], max_move: int
) -> Optional[List[int]]:
    """Order base indices by ``keys`` with a HARD displacement bound.

    Returns a permutation of ``range(len(keys))`` in which every index ``i``
    lands at a position ``j`` with ``|j - i| <= max_move``. That bound holds by
    construction rather than by hope: at each output slot only items still
    within reach are eligible, and an item on its last legal slot is forced into
    it before any preference is consulted.

    This is why the layer does not simply sort by an adjusted key. Under a plain
    sort, per-item deltas bounded by N still permit actual displacement of about
    2N through passive shoving — so the promise "nothing moved more than N
    places" would be false while every individual delta respected N.

    Each key is ``(target_slot, -rank_delta, base_index)``. The middle term is
    what makes a one-rank move actually happen: an item asking for slot ``j``
    ties with the item already sitting there, and without a tie-break favouring
    the CLAIM over the incumbent, every single-rank adjustment would compute
    correctly and then change nothing. Ties among equal claims fall through to
    base index, so a run with no adjustments returns the identity permutation and
    every deliberate ordering decision upstream survives intact.

    Returns ``None`` if placement is somehow infeasible; the caller then serves
    base order, because no adjustment is always safer than an unbounded one.
    """
    n = len(keys)
    if n == 0:
        return []
    if max_move <= 0:
        return list(range(n))

    remaining = list(range(n))
    out: List[int] = []
    for slot in range(n):
        lo, hi = slot - max_move, slot + max_move
        eligible = [i for i in remaining if lo <= i <= hi]
        if not eligible:
            logger.warning(
                "Bounded placement became infeasible at slot %s; serving base order",
                slot,
            )
            return None
        # An item at base index `slot - max_move` can never be placed later than
        # this slot, so it is forced regardless of preference.
        forced = [i for i in eligible if i <= lo]
        pool = forced or eligible
        chosen = min(pool, key=lambda i: (keys[i], i))
        out.append(chosen)
        remaining.remove(chosen)
    return out


# --------------------------------------------------------------------------
# The one adjustment function
# --------------------------------------------------------------------------


def adjust_ranking(
    opportunities: Sequence[Mapping[str, Any]],
    adjustments: Mapping[Tuple[Optional[str], Optional[str]], GroupAdjustment],
    *,
    is_active: bool = True,
    inactive_reason: Optional[str] = None,
    policy: Optional[AdjustmentPolicy] = None,
    config: Optional[LearningSignalConfig] = None,
    rank_scope: str = RANK_SCOPE_RUN,
) -> AdjustedRanking:
    """Reorder one list of findings within the configured cap.

    The ONLY adjustment function. Pure: no DB, no clock, no I/O, so the same
    inputs always produce the same order and the caps can be asserted directly.

    The incoming order IS the base order — its index is each finding's
    ``baseRank``. Findings are returned as annotated COPIES; not one input
    mapping is mutated, so a caller holding the stored list still holds the base.

    Args:
        opportunities: findings in base order.
        adjustments: stored per-group learning, keyed ``(detector_id, pack_id)``.
        is_active: T1's cold-start gate (``SignalSet.is_active``). False applies
            nothing — "no pretending to personalise from three data points".
        inactive_reason: the plain-language reason, carried through for the UI.
        rank_scope: what the emitted ranks are RELATIVE TO. A rank is an index
            into the list it was given, so a caller adjusting one roadmap stage
            gets stage-local ranks while a caller adjusting a whole run gets
            run-global ones — the same finding legitimately holds two different
            ``baseRank`` values. Recording the scope means "moved N places" is
            never read against the wrong denominator; comparing ranks across
            differing scopes is meaningless. See ``RANK_SCOPE_*``.
    """
    cfg = config or load_config()
    active_policy = policy or cfg.adjustment
    items = list(opportunities or ())

    def unchanged(reason: Optional[str]) -> AdjustedRanking:
        return AdjustedRanking(
            ordered=tuple(
                _annotate(o, index, None, active_policy, rank_scope)
                for index, o in enumerate(items)
            ),
            adjustments=(),
            applied=False,
            reason=reason,
            policy=active_policy,
        )

    if not items:
        return unchanged(NOT_APPLIED_EMPTY)
    if not active_policy.enabled:
        return unchanged(NOT_APPLIED_DISABLED)
    if not is_active:
        return unchanged(inactive_reason or NOT_APPLIED_COLD_START)
    if not adjustments:
        return unchanged(NOT_APPLIED_NO_STATE)

    # --- 1. the score cap, enforced where the delta is computed --------------
    records: List[Optional[OpportunityAdjustment]] = []
    keys: List[Tuple[float, ...]] = []
    for index, opp in enumerate(items):
        group = adjustments.get((_detector_of(opp), _norm(opp.get("packId") or opp.get("pack_id"))))
        if group is None or not group.net_weight:
            records.append(None)
            keys.append((float(index), 0, index))
            continue

        base_impact = _base_impact_of(opp)
        requested = group.net_weight * active_policy.points_per_signal_unit
        score_cap = active_policy.score_cap_for(base_impact)
        applied = max(-score_cap, min(score_cap, requested))
        capped_by = (
            CAPPED_BY_SCORE_FRACTION if abs(applied) < abs(requested) - 1e-9 else None
        )

        # One impact point is worth one rank of movement: intuitive on a 1-10
        # scale, and it keeps the two caps commensurable so a reader can see
        # which one bound a given finding.
        requested_rank_delta = int(round(applied))
        rank_delta = max(
            -active_policy.max_rank_move,
            min(active_policy.max_rank_move, requested_rank_delta),
        )
        if abs(rank_delta) < abs(requested_rank_delta):
            capped_by = CAPPED_BY_RANK_MOVE

        records.append(
            OpportunityAdjustment(
                opportunity_id=opp.get("id"),
                opportunity_identity=opp.get("opportunity_identity"),
                detector_id=group.detector_id,
                pack_id=group.pack_id,
                base_rank=index,
                adjusted_rank=index,  # replaced once placement is known
                base_impact=base_impact,
                requested_delta=requested,
                applied_delta=applied,
                requested_rank_delta=requested_rank_delta,
                net_weight=group.net_weight,
                has_outcome_evidence=group.has_outcome_evidence,
                signal_count=group.signal_count,
                capped_by=capped_by,
                contributing_refs=tuple(group.contributing_refs),
            )
        )
        # A positive delta should move a finding UP, i.e. towards index 0. The
        # -rank_delta term breaks the tie against the incumbent at that slot;
        # without it a one-rank move is computed and then silently discarded.
        keys.append((float(index) - rank_delta, -rank_delta, index))

    # --- 2. the rank cap, enforced on the OUTPUT -----------------------------
    placement = _bounded_placement(keys, active_policy.max_rank_move)
    if placement is None:
        return unchanged(NOT_APPLIED_NO_STATE)

    ordered: List[Mapping[str, Any]] = []
    final_records: List[OpportunityAdjustment] = []
    for slot, base_index in enumerate(placement):
        record = records[base_index]
        if record is not None:
            record = replace(record, adjusted_rank=slot)
            final_records.append(record)
        ordered.append(
            _annotate(items[base_index], base_index, record, active_policy, rank_scope)
        )

    return AdjustedRanking(
        ordered=tuple(ordered),
        adjustments=tuple(final_records),
        applied=True,
        reason=None,
        policy=active_policy,
    )


def _annotate(
    opp: Mapping[str, Any],
    base_rank: int,
    record: Optional[OpportunityAdjustment],
    policy: AdjustmentPolicy,
    rank_scope: str = RANK_SCOPE_RUN,
) -> Mapping[str, Any]:
    """A COPY carrying its base position. Nothing existing is overwritten.

    ``baseRank`` and ``baseImpact`` travel on every finding, adjusted or not, so
    "what would this have ranked without learning?" is answerable from the
    response itself rather than from a second request against a different code
    path that might disagree.

    ``rankScope`` travels with them because a rank is only meaningful against the
    list it indexes: the roadmap adjusts per stage and this run's surfaces adjust
    one flat list, so the same finding carries a stage-local rank in one payload
    and a run-global one in the other. Both are right; silently comparing them is
    not, and the scope is what makes the difference legible.
    """
    annotated = dict(opp)
    ranking: Dict[str, Any] = {
        "schemaVersion": ADJUSTMENT_SCHEMA_VERSION,
        "rankScope": rank_scope,
        "baseRank": base_rank,
        "baseImpact": round(_base_impact_of(opp), 4),
        "adjusted": record is not None and record.moved != 0,
        "caps": {
            "maxScoreFraction": policy.max_score_fraction,
            "maxRankMove": policy.max_rank_move,
        },
    }
    if record is not None:
        ranking.update(
            {
                "adjustedRank": record.adjusted_rank,
                "moved": record.moved,
                "effectiveImpact": record.effective_impact,
                "appliedDelta": round(record.applied_delta, 4),
                "requestedDelta": round(record.requested_delta, 4),
                "wasCapped": record.was_capped,
                "cappedBy": record.capped_by,
                "hasOutcomeEvidence": record.has_outcome_evidence,
                "signalCount": record.signal_count,
            }
        )
        # 2.0-A3 T3 — the structured reason, namespaced UNDER _ranking so it
        # explains the ordering and nothing else. It must never sit beside
        # confidence, corroboration or the evidence trace: AC3 forbids the
        # adjustment touching those, and copy placed among them would imply the
        # learned signal contributed to the finding's credibility, violating the
        # spirit of that criterion while passing its letter.
        #
        # Built here, from the record, rather than composed as prose at the point
        # of adjustment — so it can be counted, filtered and re-rendered (A2 T4's
        # confounder pattern). Never fatal: an unexplainable move is a defect,
        # but a serve failure would be worse.
        try:
            from .learning_reason import describe_adjustment

            reason = describe_adjustment(record)
            if reason is not None:
                ranking["reason"] = reason
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not build adjustment reason: %s", exc)
    else:
        ranking["adjustedRank"] = base_rank
        ranking["moved"] = 0
    annotated[RANKING_FIELD] = ranking
    return annotated


def base_order(opportunities: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """The order a customer gets when they ask "without learning".

    Trivial by construction — the stored order IS the base order, because the
    layer never writes into the finding. That this function has nothing to undo
    is the point of it existing.
    """
    return [dict(opp) for opp in opportunities or ()]


__all__ = [
    "ADJUSTMENT_SCHEMA_VERSION",
    "CAPPED_BY_RANK_MOVE",
    "CAPPED_BY_SCORE_FRACTION",
    "NOT_APPLIED_COLD_START",
    "NOT_APPLIED_DISABLED",
    "NOT_APPLIED_EMPTY",
    "NOT_APPLIED_NO_STATE",
    "RANKING_FIELD",
    "RANK_SCOPE_ROADMAP_STAGE",
    "RANK_SCOPE_RUN",
    "AdjustedRanking",
    "GroupAdjustment",
    "OpportunityAdjustment",
    "adjust_ranking",
    "base_order",
]
