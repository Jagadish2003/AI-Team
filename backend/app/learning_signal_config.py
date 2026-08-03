"""2.0-A3 T1 — external configuration for the learning signal set.

Every weight in ``config/learning_signals.json`` is a first guess made before a
single production outcome has been measured. The story says as much: *"Keep the
weighting explicit and configurable — it will need tuning once real outcome data
exists."* So the numbers live in config, each declaring a ``basis`` of
``measured`` / ``operationally_justified`` / ``provisional``, exactly as
``outcome_confounders.json`` and ``discovery/signals/ops_calibration.py`` do.

**The one rule this loader enforces rather than documents.**

Outcome-weighted learning is the whole distinction between this feature and
click-tracking. That distinction lives in a relationship between numbers — every
outcome weight must exceed every decision weight — and a relationship between
numbers in a JSON file is precisely the kind of invariant that gets edited away
by someone tuning a value in good faith. Nothing in the product would look
different afterwards; the ranking would simply start following opinions.

So :func:`validate_config` REFUSES such a config. A deployment that inverts the
principle fails loudly at load rather than quietly reweighting itself, and
:func:`load_config` falls back to the shipped defaults so a bad edit degrades to
the documented behaviour instead of to no learning at all.

Loaded with an mtime cache so an edit is picked up without a restart;
documentation-only keys (``_``-prefixed) are stripped before use.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = str(Path(__file__).parent / "config" / "learning_signals.json")

#: Recognised bases, in the order a reader should trust them.
BASIS_MEASURED = "measured"
BASIS_OPERATIONALLY_JUSTIFIED = "operationally_justified"
BASIS_PROVISIONAL = "provisional"
RECOGNISED_BASES: Tuple[str, ...] = (
    BASIS_MEASURED,
    BASIS_OPERATIONALLY_JUSTIFIED,
    BASIS_PROVISIONAL,
)

DIRECTION_POSITIVE = "positive"
DIRECTION_NEGATIVE = "negative"
DIRECTION_NEUTRAL = "neutral"
RECOGNISED_DIRECTIONS: Tuple[str, ...] = (
    DIRECTION_POSITIVE,
    DIRECTION_NEGATIVE,
    DIRECTION_NEUTRAL,
)


class LearningConfigError(ValueError):
    """A config that would change what the learning layer fundamentally is."""


@dataclass(frozen=True)
class WeightedSignal:
    """One weight and the direction it pushes ranking in."""

    weight: float
    direction: str

    @property
    def is_neutral(self) -> bool:
        return self.direction == DIRECTION_NEUTRAL or self.weight == 0.0


@dataclass(frozen=True)
class SimilarityConfig:
    same_detector_same_pack: float = 1.0
    same_detector_other_pack: float = 0.6
    same_signal_concept: float = 0.4
    minimum_score: float = 0.4


@dataclass(frozen=True)
class AdjustmentPolicy:
    """2.0-A3 T2 — how far the adjustment layer may move a finding.

    Both caps are enforced; the weaker binds. See the ``ranking_adjustment``
    block in ``config/learning_signals.json`` for why there are two.
    """

    enabled: bool = True
    max_score_fraction: float = 0.15
    max_rank_move: int = 3
    points_per_signal_unit: float = 0.35

    def score_cap_for(self, base_impact: float) -> float:
        """The absolute point budget for one finding, from its own base score.

        Proportional rather than absolute, so the layer is least free to reorder
        the findings the base scorer is most confident about.
        """
        return abs(float(base_impact)) * self.max_score_fraction


@dataclass(frozen=True)
class ColdStartConfig:
    """AC4's threshold. All conditions must be met before learning activates."""

    activation_policy: str = "decision_floor_plus_distinct_identity"
    minimum_decisions: int = 10
    minimum_signals: int = 10
    minimum_distinct_identities: int = 5


@dataclass(frozen=True)
class RecencyConfig:
    half_life_days: float = 180.0
    floor: float = 0.1


@dataclass(frozen=True)
class LearningSignalConfig:
    outcome_signals: Dict[str, WeightedSignal] = field(default_factory=dict)
    #: Measured direction, used only when no projection existed to validate.
    movement_direction: Dict[str, WeightedSignal] = field(default_factory=dict)
    decision_signals: Dict[str, WeightedSignal] = field(default_factory=dict)
    defer_reasons: Dict[str, float] = field(default_factory=dict)
    comparability: Dict[str, float] = field(default_factory=dict)
    material_caveat_multiplier: float = 0.6
    advisory_caveat_multiplier: float = 0.9
    #: A weighted outcome never falls below this multiple of the strongest
    #: decision weight, however heavily caveated — the evidence-class boundary.
    #: See ``outcome_floor`` in the config for the full argument.
    outcome_floor_ratio: float = 1.05
    recency: RecencyConfig = field(default_factory=RecencyConfig)
    cold_start: ColdStartConfig = field(default_factory=ColdStartConfig)
    adjustment: AdjustmentPolicy = field(default_factory=AdjustmentPolicy)
    similarity: SimilarityConfig = field(default_factory=SimilarityConfig)
    config_version: str = "1.0.0"
    configuration_scope: str = "deployment_wide"
    bases: Dict[str, str] = field(default_factory=dict)

    # -- lookups -----------------------------------------------------------

    def outcome_weight(self, verdict: str) -> WeightedSignal:
        return self.outcome_signals.get(
            str(verdict or "").strip().lower(),
            WeightedSignal(0.0, DIRECTION_NEUTRAL),
        )

    def movement_direction_weight(self, direction: str) -> WeightedSignal:
        """The fallback when a measurement exists but no projection validated it.

        A projection verdict answers "was our model right?"; the direction
        answers "did the action help?" — and ranking cares about the second.
        """
        return self.movement_direction.get(
            str(direction or "").strip().lower(),
            WeightedSignal(0.0, DIRECTION_NEUTRAL),
        )

    def decision_weight(self, action: str) -> WeightedSignal:
        return self.decision_signals.get(
            str(action or "").strip().lower(),
            WeightedSignal(0.0, DIRECTION_NEUTRAL),
        )

    def defer_multiplier(self, reason_code: Optional[str]) -> float:
        """A defer with no recognised reason contributes nothing.

        Not a default multiplier: an unrecognised reason is an unknown, and
        guessing a weight for an unknown is how a learning layer starts learning
        from noise. ``other`` exists in the vocabulary for the honest case.
        """
        if not reason_code:
            return 0.0
        return float(self.defer_reasons.get(str(reason_code).strip().lower(), 0.0))

    def comparability_multiplier(self, verdict: Optional[str]) -> float:
        """Caveated measurements are down-weighted, never dropped (A2 T3's rule).

        An unknown verdict takes the most conservative multiplier rather than
        1.0, so a verdict this code does not recognise cannot accidentally count
        as a clean comparison.
        """
        if not verdict:
            return min(self.comparability.values()) if self.comparability else 1.0
        key = str(verdict).strip().lower()
        if key in self.comparability:
            return float(self.comparability[key])
        return min(self.comparability.values()) if self.comparability else 1.0

    @property
    def strongest_decision_weight(self) -> float:
        return max((s.weight for s in self.decision_signals.values()), default=0.0)

    @property
    def outcome_floor(self) -> float:
        """The lowest weight a real (non-neutral) outcome may carry.

        Applied BEFORE recency decay, so decay — which is identical for both
        classes — cannot break the ordering it protects. See the ``outcome_floor``
        block in ``config/learning_signals.json`` for the full argument.
        """
        return self.strongest_decision_weight * self.outcome_floor_ratio

    def basis_for(self, section: str) -> str:
        return self.bases.get(section, BASIS_PROVISIONAL)

    def is_provisional(self, section: str) -> bool:
        return self.basis_for(section) == BASIS_PROVISIONAL


# --------------------------------------------------------------------------
# Shipped defaults — the fallback when the file is missing or refused.
# --------------------------------------------------------------------------

_DEFAULTS = LearningSignalConfig(
    outcome_signals={
        "within_band": WeightedSignal(3.0, DIRECTION_POSITIVE),
        "above_band": WeightedSignal(2.5, DIRECTION_POSITIVE),
        "below_band": WeightedSignal(2.0, DIRECTION_NEGATIVE),
        "not_projected": WeightedSignal(0.0, DIRECTION_NEUTRAL),
        "too_early": WeightedSignal(0.0, DIRECTION_NEUTRAL),
    },
    movement_direction={
        "improved": WeightedSignal(1.5, DIRECTION_POSITIVE),
        "worsened": WeightedSignal(1.5, DIRECTION_NEGATIVE),
        "unchanged": WeightedSignal(1.2, DIRECTION_NEGATIVE),
        "unknown": WeightedSignal(0.0, DIRECTION_NEUTRAL),
    },
    decision_signals={
        "accept": WeightedSignal(1.0, DIRECTION_POSITIVE),
        "dismiss": WeightedSignal(1.0, DIRECTION_NEGATIVE),
        "defer": WeightedSignal(0.35, DIRECTION_NEGATIVE),
    },
    defer_reasons={
        "no_capacity": 0.0,
        "blocked_by_dependency": 0.0,
        "awaiting_approval": 0.0,
        "needs_more_evidence": 0.6,
        "timing_not_right": 0.4,
        "lower_priority": 0.8,
        "other": 0.3,
    },
    comparability={"comparable": 1.0, "weakly_comparable": 0.5, "not_comparable": 0.2},
)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _strip_doc_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _strip_doc_keys(v) for k, v in value.items() if not str(k).startswith("_")
        }
    return value


def _as_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or value is None:
        return fallback
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    if result != result or result in (float("inf"), float("-inf")):
        return fallback
    return result


def _weighted(raw: Any) -> Optional[WeightedSignal]:
    if not isinstance(raw, Mapping):
        return None
    weight = _as_float(raw.get("weight"), 0.0)
    direction = str(raw.get("direction", DIRECTION_NEUTRAL)).strip().lower()
    if direction not in RECOGNISED_DIRECTIONS:
        direction = DIRECTION_NEUTRAL
    # A weight is a magnitude; direction carries the sign. A negative weight
    # would mean the sign is expressed twice, and the two could disagree.
    return WeightedSignal(abs(weight), direction)


def validate_config(config: LearningSignalConfig) -> None:
    """Refuse a config that would make this ordinary click-tracking.

    Raises:
        LearningConfigError: if any decision weight meets or exceeds any
            non-zero outcome weight, or if a required group is empty.
    """
    if not config.outcome_signals:
        raise LearningConfigError("no outcome signals configured")
    if not config.decision_signals:
        raise LearningConfigError("no decision signals configured")

    # Zero-weighted outcomes (not_projected, too_early) are counted-but-unweighted
    # by design and are not part of the ordering guarantee — comparing against
    # them would make the invariant unsatisfiable for any non-zero decision.
    # Directional signals are outcomes too — a measurement without a projection
    # is still a measurement — so they are held to the same class boundary. A
    # config that set them below an opinion would be the same inversion arriving
    # through a different key.
    outcome_weights = [
        s.weight
        for s in list(config.outcome_signals.values())
        + list(config.movement_direction.values())
        if s.weight > 0
    ]
    if not outcome_weights:
        raise LearningConfigError("every outcome signal is weighted zero")

    weakest_outcome = min(outcome_weights)
    strongest_decision = max(
        (s.weight for s in config.decision_signals.values()), default=0.0
    )
    if strongest_decision >= weakest_outcome:
        raise LearningConfigError(
            "an outcome must outweigh an opinion: the strongest decision weight "
            f"({strongest_decision}) meets or exceeds the weakest non-zero outcome "
            f"weight ({weakest_outcome}). Outcome-weighted learning is what "
            "separates this feature from click-tracking; a config that inverts it "
            "is refused rather than silently applied."
        )

    if config.outcome_floor_ratio <= 1.0:
        raise LearningConfigError(
            f"outcome_floor.ratio_to_strongest_decision is {config.outcome_floor_ratio}: "
            "it must exceed 1.0, or a heavily caveated outcome can be weighted "
            "below a clean analyst opinion. That is the governing principle "
            "breaking in the data rather than in the config, where nothing would "
            "reveal it — see the outcome_floor block for the full argument."
        )

    # The caps are what make the adjustment layer safe, so a config that removes
    # the bound is refused rather than applied. An unbounded learned adjustment
    # is not a smaller version of a bounded one — it is the invisible drift the
    # whole story exists to prevent.
    adjustment = config.adjustment
    if not 0.0 < adjustment.max_score_fraction <= 1.0:
        raise LearningConfigError(
            f"ranking_adjustment.max_score_fraction is {adjustment.max_score_fraction}: "
            "it must be in (0, 1]. Zero disables the layer silently (use `enabled` "
            "for that, so the reason is recorded); above 1.0 lets learning more "
            "than double a base score, which is an edit rather than an adjustment."
        )
    if adjustment.max_rank_move < 0:
        raise LearningConfigError(
            f"ranking_adjustment.max_rank_move is {adjustment.max_rank_move}: a "
            "negative rank cap is meaningless. Use 0 to allow no movement."
        )
    if adjustment.points_per_signal_unit < 0:
        raise LearningConfigError(
            "ranking_adjustment.points_per_signal_unit must not be negative — the "
            "sign belongs to the signal's direction, and expressing it twice lets "
            "the two disagree"
        )

    if not config.cold_start.activation_policy:
        raise LearningConfigError("cold_start.activation_policy must be declared")
    if config.cold_start.minimum_decisions < 1:
        raise LearningConfigError(
            "cold_start.minimum_decisions must be at least 1 - a decision "
            "floor of zero means outcomes could activate learning before the "
            "org has enough explicit judgements, which is what AC4 forbids"
        )
    if config.cold_start.minimum_signals < 1:
        raise LearningConfigError(
            "cold_start.minimum_signals must be at least 1 — a threshold of zero "
            "means learning is always active, which is what AC4 forbids"
        )
    if config.cold_start.minimum_distinct_identities < 1:
        raise LearningConfigError(
            "cold_start.minimum_distinct_identities must be at least 1 - a "
            "single finding must not switch learning on for an org"
        )
    if not 0.0 < config.similarity.minimum_score <= 1.0:
        raise LearningConfigError("similarity.minimum_score must be in (0, 1]")

    for name, multiplier in config.comparability.items():
        if multiplier <= 0.0:
            raise LearningConfigError(
                f"comparability multiplier for {name!r} is {multiplier}: a zero "
                "multiplier silently discards a caveated measurement, which is "
                "the blocking 2.0-A2 T3 explicitly refused"
            )


def parse_config(raw: Mapping[str, Any]) -> LearningSignalConfig:
    data = _strip_doc_keys(dict(raw))
    bases = {
        section: str(
            (raw.get(section) or {}).get("_basis", BASIS_PROVISIONAL)
            if isinstance(raw.get(section), Mapping)
            else BASIS_PROVISIONAL
        )
        for section in (
            "outcome_signals",
            "movement_direction",
            "decision_signals",
            "defer_reasons",
            "recency",
            "comparability",
            "confounders",
            "outcome_floor",
            "cold_start",
            "ranking_adjustment",
            "similarity",
        )
    }
    for section, basis in list(bases.items()):
        if basis not in RECOGNISED_BASES:
            logger.warning(
                "learning_signals.json section %s declares unrecognised basis %r; "
                "treating as provisional",
                section,
                basis,
            )
            bases[section] = BASIS_PROVISIONAL

    outcomes = {}
    for key, value in (data.get("outcome_signals") or {}).items():
        signal = _weighted(value)
        if signal is not None:
            outcomes[str(key).strip().lower()] = signal

    directions = {}
    for key, value in (data.get("movement_direction") or {}).items():
        signal = _weighted(value)
        if signal is not None:
            directions[str(key).strip().lower()] = signal

    decisions = {}
    for key, value in (data.get("decision_signals") or {}).items():
        signal = _weighted(value)
        if signal is not None:
            decisions[str(key).strip().lower()] = signal

    defer_reasons = {}
    for key, value in (data.get("defer_reasons") or {}).items():
        if isinstance(value, Mapping):
            defer_reasons[str(key).strip().lower()] = _as_float(
                value.get("multiplier"), 0.0
            )

    comparability_raw = data.get("comparability") or {}
    comparability = {
        str(k).strip().lower(): _as_float(v, 1.0)
        for k, v in comparability_raw.items()
        if not isinstance(v, Mapping)
    }

    confounders = data.get("confounders") or {}
    recency_raw = data.get("recency") or {}
    cold_raw = data.get("cold_start") or {}
    adjust_raw = data.get("ranking_adjustment") or {}
    similarity_raw = data.get("similarity") or {}

    def _sim(key: str, fallback: float) -> float:
        entry = similarity_raw.get(key)
        if isinstance(entry, Mapping):
            return _as_float(entry.get("score"), fallback)
        return _as_float(entry, fallback)

    return LearningSignalConfig(
        outcome_signals=outcomes or dict(_DEFAULTS.outcome_signals),
        movement_direction=directions or dict(_DEFAULTS.movement_direction),
        decision_signals=decisions or dict(_DEFAULTS.decision_signals),
        defer_reasons=defer_reasons or dict(_DEFAULTS.defer_reasons),
        comparability=comparability or dict(_DEFAULTS.comparability),
        material_caveat_multiplier=_as_float(
            confounders.get("material_caveat_multiplier"), 0.6
        ),
        advisory_caveat_multiplier=_as_float(
            confounders.get("advisory_caveat_multiplier"), 0.9
        ),
        outcome_floor_ratio=_as_float(
            (data.get("outcome_floor") or {}).get("ratio_to_strongest_decision"), 1.05
        ),
        recency=RecencyConfig(
            half_life_days=_as_float(recency_raw.get("half_life_days"), 180.0),
            floor=_as_float(recency_raw.get("floor"), 0.1),
        ),
        adjustment=AdjustmentPolicy(
            enabled=bool(adjust_raw.get("enabled", True)),
            max_score_fraction=_as_float(adjust_raw.get("max_score_fraction"), 0.15),
            max_rank_move=int(_as_float(adjust_raw.get("max_rank_move"), 3)),
            points_per_signal_unit=_as_float(
                adjust_raw.get("points_per_signal_unit"), 0.35
            ),
        ),
        cold_start=ColdStartConfig(
            activation_policy=str(
                cold_raw.get(
                    "activation_policy", "decision_floor_plus_distinct_identity"
                )
            ),
            minimum_decisions=int(
                _as_float(
                    cold_raw.get("minimum_decisions"),
                    _as_float(cold_raw.get("minimum_signals"), 10),
                )
            ),
            minimum_signals=int(_as_float(cold_raw.get("minimum_signals"), 10)),
            minimum_distinct_identities=int(
                _as_float(cold_raw.get("minimum_distinct_identities"), 5)
            ),
        ),
        similarity=SimilarityConfig(
            same_detector_same_pack=_sim("same_detector_same_pack", 1.0),
            same_detector_other_pack=_sim("same_detector_other_pack", 0.6),
            same_signal_concept=_sim("same_signal_concept", 0.4),
            minimum_score=_as_float(similarity_raw.get("minimum_score"), 0.4),
        ),
        config_version=str(
            (raw.get("_meta") or {}).get("configVersion", "1.0.0")
            if isinstance(raw.get("_meta"), Mapping)
            else "1.0.0"
        ),
        configuration_scope=str(
            (raw.get("_meta") or {}).get("configurationScope", "deployment_wide")
            if isinstance(raw.get("_meta"), Mapping)
            else "deployment_wide"
        ),
        bases=bases,
    )


# --------------------------------------------------------------------------
# Loading (mtime-cached)
# --------------------------------------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, Any] = {"path": None, "mtime": None, "config": None}


def config_path() -> str:
    return os.getenv("LEARNING_SIGNALS_CONFIG", DEFAULT_CONFIG_PATH)


def load_config(*, force: bool = False) -> LearningSignalConfig:
    """The current config, re-read when the file changes on disk.

    Never raises. A missing, malformed or REFUSED config degrades to the shipped
    defaults with a warning — the documented behaviour rather than none at all.
    """
    path = config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None

    with _lock:
        if (
            not force
            and _cache["config"] is not None
            and _cache["path"] == path
            and _cache["mtime"] == mtime
        ):
            return _cache["config"]

    config = _DEFAULTS
    if mtime is None:
        logger.warning(
            "learning signal config not found at %s; using shipped defaults", path
        )
    else:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            config = parse_config(raw)
            validate_config(config)
        except LearningConfigError as exc:
            logger.error(
                "REFUSED learning signal config at %s: %s. Falling back to shipped "
                "defaults — the ranking layer will not run on this config.",
                path,
                exc,
            )
            config = _DEFAULTS
        except Exception as exc:  # noqa: BLE001 - config must never break a read
            logger.warning(
                "could not load learning signal config at %s (%s); using defaults",
                path,
                exc,
            )
            config = _DEFAULTS

    with _lock:
        _cache.update({"path": path, "mtime": mtime, "config": config})
    return config


def reset_cache() -> None:
    with _lock:
        _cache.update({"path": None, "mtime": None, "config": None})


__all__ = [
    "BASIS_MEASURED",
    "BASIS_OPERATIONALLY_JUSTIFIED",
    "BASIS_PROVISIONAL",
    "DIRECTION_NEGATIVE",
    "DIRECTION_NEUTRAL",
    "DIRECTION_POSITIVE",
    "RECOGNISED_BASES",
    "AdjustmentPolicy",
    "ColdStartConfig",
    "LearningConfigError",
    "LearningSignalConfig",
    "RecencyConfig",
    "SimilarityConfig",
    "WeightedSignal",
    "config_path",
    "load_config",
    "parse_config",
    "reset_cache",
    "validate_config",
]
