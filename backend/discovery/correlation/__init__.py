"""MSP-B7 — cross-stream correlation (Track B).

Correlation is where cloud event streams meet each other and the rest of the
estate: event↔event across providers, event↔incident against ServiceNow. This
package holds the *discipline* that keeps those joins honest — the
correlation-window service (MSP-B7 T5), which makes a join valid only when the
two facts fall within a configurable time window, records the window and the
observed delta in the joined claim's evidence trace, and gates operational
corroboration so out-of-window agreement contributes ZERO confidence.

See :mod:`discovery.correlation.windows`.
"""

from .windows import (  # noqa: F401
    DEFAULT_CORRELATION_WINDOWS,
    DEFAULT_WINDOW_SECONDS,
    JOIN_EVENT_EVENT,
    JOIN_EVENT_INCIDENT,
    CorrelationWindowPolicy,
    GatedCorroboration,
    WindowJoin,
    gate_operational_corroboration,
    join_within_window,
    window_for,
    within_window,
)

__all__ = [
    "JOIN_EVENT_INCIDENT",
    "JOIN_EVENT_EVENT",
    "DEFAULT_CORRELATION_WINDOWS",
    "DEFAULT_WINDOW_SECONDS",
    "CorrelationWindowPolicy",
    "WindowJoin",
    "GatedCorroboration",
    "within_window",
    "join_within_window",
    "gate_operational_corroboration",
    "window_for",
]
