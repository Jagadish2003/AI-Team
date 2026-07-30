"""2.0-A2 T2 — the frozen measurement basis, as a pure artifact.

At the moment a finding is created, record exactly what it was measured from:
which signals, over which window, at which values, under which pack version.

**Why freeze rather than re-derive.** Without this, every later comparison is
against a moving target. The temporal baseline in ``temporal.py`` is a *rolling,
recomputed, org-and-signal-level* statistic — ``baseline_calculator`` recomputes
``baseline_mean`` / ``baseline_stddev`` over a sliding ``BASELINE_WINDOW_DAYS``
window every time it runs. That is a useful signal, and it is emphatically not
"the basis this specific finding was born with". Re-deriving "what did this look
like before?" from historical runs at read time would give a different answer as
pack logic, thresholds or the signal set evolved. Freezing at creation is what
makes an outcome claim defensible six months later.

**Pack version is load-bearing.** Packs stamp ``packVersion`` onto every
opportunity so governance can tell a *data* change from a *pack-logic* change.
T4's confounder detection reads exactly this field to flag pack-version drift
between the two measurements — captured here or T4 has nothing to compare.

**Which signals.** Taken from the A1 detector signal registry rather than guessed:
that registry is already the single mapping from a detector to the REAL measured
field names it emits, and T3 will re-measure those same fields. A finding whose
detector has no profile still gets a baseline — from its raw measured evidence —
because a baseline that silently skips some findings would make them quietly
unmeasurable.

Pure: no DB, no clock read except the caller-supplied ``captured_at``, so the
artifact is fully testable and byte-reproducible from one opportunity record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

#: Bumped when the artifact's shape changes in a way T3/T4 must notice.
BASELINE_SCHEMA_VERSION = "1.0.0"

#: How the observation window was arrived at. Recorded rather than implied, so a
#: reader can tell a real measured window from a fallback.
WINDOW_FROM_TEMPORAL_BASELINE = "temporal_baseline_window_days"
WINDOW_FROM_DETECTOR_DEFAULT = "detector_default_window_days"
WINDOW_UNKNOWN = "unknown"

#: Used only when nothing on the record declares a window. Named so it is
#: obviously a fallback in stored data rather than a measured fact.
DEFAULT_WINDOW_DAYS = 90

#: The role a signal played in producing the finding.
ROLE_MOVEMENT = "movement"
ROLE_INSTANCE = "instance"
ROLE_POPULATION = "population"
ROLE_MEASURED = "measured"


class BaselineCaptureError(ValueError):
    """The opportunity cannot produce an honest baseline artifact."""


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _as_number(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else round(value, 6)


def _as_int(value: Any) -> Optional[int]:
    number = _safe_float(value)
    return int(number) if number is not None else None


def _debug(opp: Mapping[str, Any]) -> Mapping[str, Any]:
    debug = opp.get("_debug")
    return debug if isinstance(debug, Mapping) else {}


def _field(opp: Mapping[str, Any], *names: str) -> Any:
    """First present value among ``names``, checking ``_debug`` as a fallback."""
    debug = _debug(opp)
    for name in names:
        if opp.get(name) not in (None, ""):
            return opp.get(name)
        if debug.get(name) not in (None, ""):
            return debug.get(name)
    return None


def _raw_evidence(opp: Mapping[str, Any]) -> Dict[str, Any]:
    """The detector's raw measured numbers, wherever they survived.

    The Track A adapter keeps them under ``_debug``; an in-pipeline opportunity
    still has them at the top level.
    """
    for candidate in (opp.get("raw_evidence"), _debug(opp).get("raw_evidence")):
        if isinstance(candidate, Mapping) and candidate:
            return dict(candidate)
    return {}


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = _iso(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Signal selection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BaselineSignal:
    """One measured signal the finding was derived from, at its captured value."""

    signal_name: str
    role: str
    value: Optional[float]
    unit: Optional[str] = None
    concept: Optional[str] = None
    concept_label: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signalName": self.signal_name,
            "role": self.role,
            "value": _as_number(self.value),
            "unit": self.unit,
            "concept": self.concept,
            "conceptLabel": self.concept_label,
        }


def _profile_signals(
    detector_id: str, raw: Mapping[str, Any]
) -> List[BaselineSignal]:
    """The signals the A1 registry says this detector's finding rests on.

    Reused rather than re-derived: T3 must re-measure the SAME fields, and the
    registry is already the one mapping from detector to real field names.
    """
    try:
        from discovery.projection.signal_registry import (
            SIGNAL_CONCEPTS,
            get_detector_profile,
        )
    except Exception:  # noqa: BLE001 - registry is advisory here
        return []

    profile = get_detector_profile(detector_id)
    if profile is None:
        return []

    concept_label = SIGNAL_CONCEPTS.get(profile.concept, profile.concept)
    seen: set = set()
    signals: List[BaselineSignal] = []
    for name, role, unit in (
        (profile.movement_signal, ROLE_MOVEMENT, profile.unit),
        (profile.instance_field, ROLE_INSTANCE, "count"),
        (profile.volume_signal, ROLE_POPULATION, "count"),
    ):
        if not name or name in seen:
            continue
        seen.add(name)
        signals.append(
            BaselineSignal(
                signal_name=name,
                role=role,
                value=_safe_float(raw.get(name)),
                unit=unit,
                concept=profile.concept,
                concept_label=concept_label,
            )
        )
    return signals


def _measured_signals(raw: Mapping[str, Any]) -> List[BaselineSignal]:
    """Every numeric field the detector actually measured, sorted by name.

    The fallback when a detector has no registry profile — and always stored
    alongside the profiled signals, because T3 comparing a field the profile
    happens not to name is better served by having the number than by not.
    """
    signals: List[BaselineSignal] = []
    for name in sorted(raw):
        value = _safe_float(raw.get(name))
        if value is not None:
            signals.append(
                BaselineSignal(signal_name=name, role=ROLE_MEASURED, value=value)
            )
    return signals


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationWindow:
    """The window the finding was measured over, and how that was derived."""

    days: Optional[int]
    started_at: Optional[str]
    ended_at: Optional[str]
    derivation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "days": self.days,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "derivation": self.derivation,
        }


@dataclass(frozen=True)
class BaselineArtifact:
    """The immutable measurement basis a finding is born with.

    Everything T3's comparison and T4's confounder checks need, frozen at
    creation. Distinct from the per-run ``opportunity_instances`` row, which it
    REFERENCES rather than replaces: that row is "how the finding scored on that
    run"; this is "the measurement basis we will be judged against".
    """

    org_id: str
    opportunity_identity: str
    run_id: str
    detector_id: str
    pack_id: Optional[str]
    pack_version: Optional[str]
    opportunity_ref: Optional[str]
    signal_keys: List[str]
    signals: List[BaselineSignal]
    window: ObservationWindow
    baseline_stats: Dict[str, Any]
    measured_values: Dict[str, Any]
    captured_at: str
    schema_version: str = BASELINE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "orgId": self.org_id,
            "opportunityIdentity": self.opportunity_identity,
            "runId": self.run_id,
            "detectorId": self.detector_id,
            "packId": self.pack_id,
            "packVersion": self.pack_version,
            "opportunityRef": self.opportunity_ref,
            # The originating per-run instance, by its natural key. A reference,
            # not a copy — the instance row keeps its own life.
            "instanceRef": {
                "opportunityIdentity": self.opportunity_identity,
                "runId": self.run_id,
            },
            "signalKeys": list(self.signal_keys),
            "signals": [s.to_dict() for s in self.signals],
            "window": self.window.to_dict(),
            "baselineStats": dict(self.baseline_stats),
            "measuredValues": dict(self.measured_values),
            "capturedAt": self.captured_at,
        }


def _build_window(opp: Mapping[str, Any], captured_at: str) -> ObservationWindow:
    """Resolve the observation window and record how it was derived.

    The derivation is stored because a fallback window and a measured one must
    never be indistinguishable in the frozen record.
    """
    days = _as_int(_field(opp, "baseline_window_days"))
    if days and days > 0:
        derivation = WINDOW_FROM_TEMPORAL_BASELINE
    else:
        days = DEFAULT_WINDOW_DAYS
        derivation = WINDOW_FROM_DETECTOR_DEFAULT

    ended = _parse_dt(
        _field(opp, "run_completed_at", "completedAt", "captured_at")
    ) or _parse_dt(captured_at)
    started = ended - timedelta(days=days) if ended else None
    return ObservationWindow(
        days=days,
        started_at=_iso(started),
        ended_at=_iso(ended),
        derivation=derivation if ended else WINDOW_UNKNOWN,
    )


def build_baseline_artifact(
    opp: Mapping[str, Any],
    *,
    org_id: str,
    run_id: str,
    captured_at: str,
) -> Dict[str, Any]:
    """Freeze one opportunity's measurement basis.

    ``captured_at`` is supplied by the caller rather than read here, so the
    builder stays pure and the artifact is reproducible in a test.

    Raises :class:`BaselineCaptureError` when the opportunity carries no stable
    identity — a baseline that cannot be found again by identity is worthless,
    and silently writing one keyed on nothing would be worse than none.
    """
    identity = str(opp.get("opportunity_identity") or "").strip()
    if not identity:
        raise BaselineCaptureError(
            "opportunity has no opportunity_identity: a baseline keyed on nothing "
            "could never be matched to a later measurement"
        )

    detector_id = str(_field(opp, "detector_id") or "").strip()
    if not detector_id:
        raise BaselineCaptureError(
            f"opportunity {identity!r} has no detector_id: T4 cannot compare a "
            "measurement whose detector is unknown"
        )

    raw = _raw_evidence(opp)
    signals = _profile_signals(detector_id, raw)
    # Profiled signals first (the ones T3 re-measures), then every other numeric
    # field, de-duplicated by name.
    named = {s.signal_name for s in signals}
    signals.extend(s for s in _measured_signals(raw) if s.signal_name not in named)

    signal_key = _field(opp, "signal_key")
    signal_keys = [str(signal_key)] if signal_key else []

    baseline_stats = {
        # The ROLLING temporal statistic as it stood at capture. Frozen here
        # precisely because baseline_calculator will keep moving it.
        "mean": _as_number(_safe_float(_field(opp, "baseline_mean"))),
        "stddev": _as_number(_safe_float(_field(opp, "baseline_stddev"))),
        "windowDays": _as_int(_field(opp, "baseline_window_days")),
        "runCount": _as_int(_field(opp, "run_count")),
        "currentValue": _as_number(_safe_float(_field(opp, "current_value"))),
        "recentValues": [
            _as_number(v)
            for v in (_safe_float(x) for x in (opp.get("recent_values") or ()))
            if v is not None
        ],
        "metricValue": _as_number(_safe_float(_field(opp, "metric_value"))),
        "threshold": _as_number(_safe_float(_field(opp, "threshold"))),
        "confidence": (str(opp.get("confidence")).strip().upper() or None)
        if opp.get("confidence")
        else None,
    }

    artifact = BaselineArtifact(
        org_id=str(org_id).strip(),
        opportunity_identity=identity,
        run_id=str(run_id).strip(),
        detector_id=detector_id,
        pack_id=(str(_field(opp, "packId", "pack_id")) if _field(opp, "packId", "pack_id") else None),
        pack_version=(
            str(_field(opp, "packVersion", "pack_version"))
            if _field(opp, "packVersion", "pack_version")
            else None
        ),
        opportunity_ref=(str(opp.get("id")) if opp.get("id") else None),
        signal_keys=signal_keys,
        signals=signals,
        window=_build_window(opp, captured_at),
        baseline_stats=baseline_stats,
        measured_values={k: _as_number(_safe_float(v)) for k, v in sorted(raw.items())
                         if _safe_float(v) is not None},
        captured_at=captured_at,
    )
    return artifact.to_dict()


#: Fields T3's comparison and T4's confounder checks require. Asserted by test so
#: a future shape change cannot quietly drop something a later subtask needs.
REQUIRED_ARTIFACT_FIELDS = (
    "schemaVersion",
    "orgId",
    "opportunityIdentity",
    "runId",
    "detectorId",
    "packVersion",
    "signals",
    "window",
    "baselineStats",
    "measuredValues",
    "capturedAt",
)


def missing_artifact_fields(artifact: Optional[Mapping[str, Any]]) -> List[str]:
    """Which required fields an artifact lacks. Empty list means complete."""
    if not isinstance(artifact, Mapping):
        return list(REQUIRED_ARTIFACT_FIELDS)
    return [
        name
        for name in REQUIRED_ARTIFACT_FIELDS
        if artifact.get(name) in (None, "", [], {})
        # packVersion may legitimately be absent on a pack that never stamped
        # one; it is required to be PRESENT as a key, not to be non-empty.
        and name != "packVersion"
    ] + ([] if "packVersion" in artifact else ["packVersion"])


__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "DEFAULT_WINDOW_DAYS",
    "REQUIRED_ARTIFACT_FIELDS",
    "ROLE_INSTANCE",
    "ROLE_MEASURED",
    "ROLE_MOVEMENT",
    "ROLE_POPULATION",
    "WINDOW_FROM_DETECTOR_DEFAULT",
    "WINDOW_FROM_TEMPORAL_BASELINE",
    "WINDOW_UNKNOWN",
    "BaselineArtifact",
    "BaselineCaptureError",
    "BaselineSignal",
    "ObservationWindow",
    "build_baseline_artifact",
    "missing_artifact_fields",
]
