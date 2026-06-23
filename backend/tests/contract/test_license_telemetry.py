"""Unit tests for LIC-1 license telemetry registration (AT-348 / T7).

Confirms the five license lifecycle events are registered, that record_event()
succeeds for each (DB write is best-effort and never raises), and that an
unregistered event type still raises ValueError — the guard that forces
registration to land before any emission call-site (T4/T6).
"""

import pytest

from app.telemetry import (
    EVENT_PAYLOAD_TYPES,
    REGISTERED_EVENT_TYPES,
    record_event,
)

LICENSE_EVENTS = [
    "license.validated",
    "license.entered_grace",
    "license.entered_readonly",
    "license.updated",
    "license.clock_anomaly",
]

# Representative, PII-safe payloads (status / dates / customer id only — never a
# raw key string or secret).
SAMPLE_PAYLOADS = {
    "license.validated": {
        "customer": "City National Bank",
        "status": "grace",
        "expires_at": "2027-06-18",
        "days_remaining": -3,
    },
    "license.entered_grace": {"customer": "City National Bank", "expires_at": "2027-06-18"},
    "license.entered_readonly": {"customer": "City National Bank", "expires_at": "2027-06-18"},
    "license.updated": {"customer": "City National Bank", "status": "valid", "expires_at": "2028-06-18"},
    "license.clock_anomaly": {"last_seen": "2026-06-19", "now": "2026-06-15"},
}


@pytest.mark.parametrize("event", LICENSE_EVENTS)
def test_license_event_is_registered(event):
    assert event in REGISTERED_EVENT_TYPES
    # Each event has a TypedDict payload schema bound to it.
    assert EVENT_PAYLOAD_TYPES[event] is not None


@pytest.mark.parametrize("event", LICENSE_EVENTS)
def test_record_event_succeeds_for_each_license_event(event):
    # Must not raise for a registered type (the DB write is fire-and-forget).
    record_event(event, SAMPLE_PAYLOADS[event])


def test_unregistered_license_event_still_raises():
    with pytest.raises(ValueError):
        record_event("license.not_a_real_event", {})
