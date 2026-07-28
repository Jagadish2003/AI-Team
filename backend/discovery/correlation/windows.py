"""MSP-B7 / AT-673 — the correlation-window service (Track B, fifth discipline).

Cross-stream joins are how AgentIQ turns separate facts into one story: an AWS
event and a ServiceNow incident, two provider events about the same resource. But
two things happening *near* each other in a noisy stream is not the same as two
things happening *because* of each other. This service is the honesty discipline
applied to time: **a join is valid ONLY within a configurable time window**, the
window used and the observed delta are **recorded in the evidence trace of every
joined claim**, and **out-of-window agreement contributes ZERO confidence** to
corroboration — so coincidence can never inflate confidence.

> *Windows are epistemology, not plumbing.* Corroboration means independent
> sources agreeing about the SAME moment. Recording the window in the trace lets a
> reviewer challenge the join itself — the interrogability the platform promises.

What this provides
-------------------
* :func:`within_window` / :func:`join_within_window` — is a join valid? The latter
  returns a :class:`WindowJoin` carrying ``(join_type, window, delta)`` for the
  evidence trace.
* :class:`CorrelationWindowPolicy` — per-join-type default windows, per-org
  tunable (the ``window_config(org_id, join_type)`` of the MSP-B7 sketch).
* :func:`gate_operational_corroboration` — the corroboration integration point for
  operational sources: an event↔incident agreement **inside** the window elevates
  confidence (MEDIUM→HIGH, like an observed system-of-record corroborator); the
  identical agreement **outside** the window contributes zero — and the rejected
  coincidence is still recorded on the trace, never silently dropped.

The confidence vocabulary is shared verbatim with the corroboration rule registry
(:mod:`discovery.packs.corroboration_rules`) so this service and the rules speak
the same language.

Scope (T5)
----------
Windows are configurable per join type and per org; their calibrated defaults come
from B8's month-scale measurements in T6. This service is the reusable surface the
MSP event corroboration rules (B4/B6) consult; it deliberately does not rewire the
existing app-friction corroborators (COR-09/COR-10), which are 30-day freshness
rules, not cloud event↔incident joins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

try:
    from discovery.packs.corroboration_rules import (
        CONFIDENCE_HIGH,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )
    from discovery.signals.ops_calibration import (
        CALIBRATED_CORRELATION_WINDOWS,
        CALIBRATED_DEFAULT_WINDOW_SECONDS,
    )
except ModuleNotFoundError:  # project-root execution uses backend as package
    from backend.discovery.packs.corroboration_rules import (
        CONFIDENCE_HIGH,
        CONFIDENCE_MEDIUM,
        CONFIDENCE_ORDER,
    )
    from backend.discovery.signals.ops_calibration import (
        CALIBRATED_CORRELATION_WINDOWS,
        CALIBRATED_DEFAULT_WINDOW_SECONDS,
    )

# ─────────────────────────────────────────────────────────────────────────────
# Join types + default windows (per-join-type, per-org tunable — T6 calibrates)
# ─────────────────────────────────────────────────────────────────────────────

#: An operational (cloud) event correlated with a ServiceNow-style incident.
JOIN_EVENT_INCIDENT = "event_incident"
#: A cloud event correlated with another cloud event (cross-provider).
JOIN_EVENT_EVENT = "event_event"

#: Default windows in SECONDS, per join type — CALIBRATED from B8's month-scale
#: sample (MSP-B7 T6, see :mod:`discovery.signals.ops_calibration`): ``event_event``
#: is kept tight against the measured ~42 events/hour density, ``event_incident``
#: is the operational incident-creation lag (2h). Per-org tunable.
DEFAULT_CORRELATION_WINDOWS: Dict[str, int] = dict(CALIBRATED_CORRELATION_WINDOWS)

#: Fallback window for a join type with no configured default (1 hour). Calibrated (T6).
DEFAULT_WINDOW_SECONDS = CALIBRATED_DEFAULT_WINDOW_SECONDS


# ─────────────────────────────────────────────────────────────────────────────
# Timestamp extraction (tolerant — facts arrive in several shapes)
# ─────────────────────────────────────────────────────────────────────────────

_TS_ATTRS = ("occurred_at", "observed_at", "opened_at", "timestamp", "first_seen")
_TS_KEYS = ("occurred_at", "observed_at", "opened_at", "timestamp", "first_seen",
            "eventTime", "opened", "closed_at")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse a UTC ISO-8601 timestamp to an aware datetime, tolerantly.

    Accepts a trailing ``Z``, a space separator instead of ``T``, and naive
    timestamps (assumed UTC). Returns ``None`` for an empty/unparseable value so a
    join degrades to "cannot confirm" rather than raising.
    """
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Tolerate a "YYYY-MM-DD HH:MM:SS" space separator.
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T", 1))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _occurred_at(fact: Any) -> Tuple[Optional[datetime], Optional[str]]:
    """Extract ``(aware_datetime, original_string)`` from a fact of any shape.

    Understands :class:`~discovery.signals.operational_event.OperationalEvent`
    (``observed_at``), active signals (``first_seen``), incident-like objects/dicts
    (``opened_at`` / ``occurred_at`` / ``timestamp`` …), a raw ISO string, or a
    ``datetime``. Returns ``(None, None)`` when no timestamp can be found.
    """
    if fact is None:
        return None, None
    if isinstance(fact, datetime):
        dt = fact if fact.tzinfo else fact.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), fact.isoformat()
    if isinstance(fact, str):
        return _parse_iso(fact), fact

    raw: Any = None
    if isinstance(fact, dict):
        for k in _TS_KEYS:
            if fact.get(k):
                raw = fact[k]
                break
    else:
        for a in _TS_ATTRS:
            v = getattr(fact, a, None)
            if v:
                raw = v
                break
    if raw is None:
        return None, None
    if isinstance(raw, datetime):
        dt = raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), raw.isoformat()
    return _parse_iso(str(raw)), str(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Per-join-type / per-org window policy
# ─────────────────────────────────────────────────────────────────────────────

class CorrelationWindowPolicy:
    """Per-join-type correlation windows, tunable per org.

    ``windows`` overrides :data:`DEFAULT_CORRELATION_WINDOWS` (seconds per join
    type); an unknown join type uses ``default_window_seconds``. Per-org overrides
    are layered on top via :meth:`set_org_window` and win for that org
    (the ``window_config(org_id, join_type)`` resolver of the MSP-B7 sketch).
    """

    def __init__(
        self,
        windows: Optional[Dict[str, int]] = None,
        *,
        default_window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ):
        if default_window_seconds <= 0:
            raise ValueError("default_window_seconds must be > 0")
        resolved = dict(DEFAULT_CORRELATION_WINDOWS if windows is None else windows)
        for jt, secs in resolved.items():
            if secs <= 0:
                raise ValueError(f"window for {jt!r} must be > 0, got {secs}")
        self._windows = resolved
        self._default = int(default_window_seconds)
        self._org_overrides: Dict[str, Dict[str, int]] = {}

    def set_org_window(self, org_id: str, join_type: str, seconds: int) -> None:
        """Tune the window for one org + join type (per-org override)."""
        if not org_id:
            raise ValueError("org_id is required")
        if seconds <= 0:
            raise ValueError("window seconds must be > 0")
        self._org_overrides.setdefault(org_id, {})[join_type] = int(seconds)

    def window_for(self, join_type: str, org_id: Optional[str] = None) -> int:
        """Resolve the window (seconds) for a join type, honouring per-org overrides."""
        if org_id:
            org = self._org_overrides.get(org_id)
            if org and join_type in org:
                return org[join_type]
        return self._windows.get(join_type, self._default)


#: Process-wide default policy used when a caller passes no explicit policy.
_DEFAULT_POLICY = CorrelationWindowPolicy()


def window_for(
    join_type: str, org_id: Optional[str] = None,
    policy: Optional[CorrelationWindowPolicy] = None,
) -> int:
    """The configured window (seconds) for a join type (default policy if none)."""
    return (policy or _DEFAULT_POLICY).window_for(join_type, org_id)


# ─────────────────────────────────────────────────────────────────────────────
# The join + its evidence trace
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WindowJoin:
    """The result of a windowed correlation join — and its evidence trace fragment.

    ``within`` is the verdict; ``window_seconds`` is the window applied and
    ``delta_seconds`` the observed gap between the two facts (``None`` when a
    timestamp could not be parsed → the join cannot be confirmed). :meth:`to_trace`
    is the record attached to the joined claim's evidence so a reviewer can
    challenge the join.
    """

    join_type: str
    window_seconds: int
    delta_seconds: Optional[float]
    within: bool
    a_at: Optional[str]
    b_at: Optional[str]

    def to_trace(self) -> Dict[str, Any]:
        """The ``correlation_window`` evidence-trace fragment (join_type, window, delta)."""
        return {
            "correlation_window": {
                "join_type": self.join_type,
                "window_seconds": self.window_seconds,
                "delta_seconds": self.delta_seconds,
                "within_window": self.within,
                "a_at": self.a_at,
                "b_at": self.b_at,
            }
        }


def join_within_window(
    a: Any, b: Any, join_type: str, *,
    org_id: Optional[str] = None,
    policy: Optional[CorrelationWindowPolicy] = None,
) -> WindowJoin:
    """Evaluate whether two facts correlate within the configured window.

    Extracts each fact's timestamp, resolves the window for ``join_type`` (and
    ``org_id``), and returns a :class:`WindowJoin` recording the window and the
    observed delta. An unparseable timestamp yields ``within=False`` with
    ``delta_seconds=None`` — a join that cannot be confirmed never counts.
    """
    window = window_for(join_type, org_id, policy)
    ta, a_at = _occurred_at(a)
    tb, b_at = _occurred_at(b)
    if ta is None or tb is None:
        return WindowJoin(join_type, window, None, False, a_at, b_at)
    delta = abs((ta - tb).total_seconds())
    return WindowJoin(join_type, window, delta, delta <= window, a_at, b_at)


def within_window(
    a: Any, b: Any, join_type: str, *,
    org_id: Optional[str] = None,
    policy: Optional[CorrelationWindowPolicy] = None,
) -> bool:
    """True iff facts ``a`` and ``b`` fall within the configured window for ``join_type``."""
    return join_within_window(a, b, join_type, org_id=org_id, policy=policy).within


# ─────────────────────────────────────────────────────────────────────────────
# Corroboration integration — the confidence gate for operational sources
# ─────────────────────────────────────────────────────────────────────────────

def _higher_confidence(a: str, b: str) -> str:
    """Return the higher of two confidence levels by the shared ordering."""
    return a if CONFIDENCE_ORDER.get(a, 0) >= CONFIDENCE_ORDER.get(b, 0) else b


@dataclass(frozen=True)
class GatedCorroboration:
    """The window-gated confidence effect of an operational corroboration.

    ``within`` says whether the two facts fell in the window; ``elevates`` and
    ``confidence`` are the resulting effect — a within-window agreement elevates
    (e.g. MEDIUM→HIGH), an out-of-window one contributes ZERO (stays at the base
    confidence, ``elevates=False``). ``join`` carries the window/delta so the
    decision — elevation OR rejection — is recorded on the evidence trace.
    """

    within: bool
    elevates: bool
    confidence: str
    join: WindowJoin

    def to_trace(self) -> Dict[str, Any]:
        """Evidence-trace fragment: the window record plus the confidence effect."""
        trace = dict(self.join.to_trace())
        trace["corroboration"] = {
            "within_window": self.within,
            "elevates": self.elevates,
            "confidence": self.confidence,
        }
        return trace


def gate_operational_corroboration(
    event_fact: Any,
    incident_fact: Any,
    *,
    join_type: str = JOIN_EVENT_INCIDENT,
    base_confidence: str = CONFIDENCE_MEDIUM,
    elevation_target: str = CONFIDENCE_HIGH,
    org_id: Optional[str] = None,
    policy: Optional[CorrelationWindowPolicy] = None,
) -> GatedCorroboration:
    """Gate an operational corroboration on the correlation window.

    The MSP-B7 confidence rule for operational sources: an event↔incident
    agreement **inside** the window raises confidence to ``elevation_target``
    (like an observed system-of-record corroborator); the identical agreement
    **outside** the window contributes zero and confidence stays at
    ``base_confidence`` — *coincidence never inflates confidence*. Either way the
    window and delta are recorded on the returned join, so the decision is
    auditable rather than silent.
    """
    j = join_within_window(event_fact, incident_fact, join_type, org_id=org_id, policy=policy)
    if not j.within:
        return GatedCorroboration(False, False, base_confidence, j)
    elevated = _higher_confidence(base_confidence, elevation_target)
    return GatedCorroboration(True, elevated != base_confidence, elevated, j)
