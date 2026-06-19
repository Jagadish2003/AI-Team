"""LIC-1 / T4 (AT-345) — startup + periodic license validation runtime.

This is the *runtime wiring* layer of the offline license scheme. It is the
only LIC-1 module that has side effects:

  * It resolves the installed license key (DB-stored key wins; otherwise the
    ``LICENSE_KEY`` env var, which is then persisted).
  * It persists app-global license state via the existing ``kv_get`` /
    ``kv_set`` helpers in ``app.db`` — NOT ``run_kv_*`` (license state is
    app-global, never run-scoped).
  * It applies the light clock-rollback guard (§6).
  * It emits license telemetry on each check and on state transitions.

Validation itself (signature + expiry/grace/read-only) lives in
``app.licensing.validate_license`` (T3) and is pure/side-effect-free; this
module calls it and layers persistence, the clock guard, telemetry, and the
APScheduler periodic job on top.

``main.py`` calls :func:`run_startup_validation` once at startup and
:func:`start_license_scheduler` for the periodic re-check (the latter gated by
``AGENTIQ_DISABLE_BACKGROUND_JOBS``). No validation loop is inlined into
``main.py``.

Offline by design: nothing here makes a network call.
"""
from __future__ import annotations

import datetime
import logging
import os
import signal

from apscheduler.schedulers.background import BackgroundScheduler

from .db import kv_get, kv_set
from .licensing import LicenseStatus, validate_license
from .telemetry import record_event

logger = logging.getLogger(__name__)

# --- App-global KV keys (NOT run-scoped) ----------------------------------
LICENSE_KEY_KV = "license:key"               # the installed key string
LICENSE_LAST_SEEN_KV = "license:last_seen_date"  # ISO date of last consistent check
LICENSE_LAST_STATUS_KV = "license:last_status"   # last status, for transition events

# --- Clock-rollback guard (§6) --------------------------------------------
# Deliberately tolerant: a 2-day window avoids false positives from legitimate
# NTP corrections or timezone quirks. A speed bump, not a vault door.
CLOCK_TOLERANCE_DAYS = 2

# --- Periodic re-check scheduling ------------------------------------------
LICENSE_CHECK_INTERVAL_HOURS = int(os.getenv("LICENSE_CHECK_INTERVAL_HOURS", "12"))
LICENSE_JOB_ID = "license_periodic_validation"

scheduler = BackgroundScheduler()
_sigterm_handler_registered = False


# ===========================================================================
# Core evaluation
# ===========================================================================
def evaluate_license(
    *,
    today: datetime.date | None = None,
    public_key=None,
    persist: bool = True,
    emit: bool = True,
) -> dict:
    """Evaluate the installed license and return its status dict.

    Order of operations:
      1. Clock-rollback guard (§6 / AC8). If the stored ``last_seen_date`` is
         more than ``CLOCK_TOLERANCE_DAYS`` in the future relative to ``today``,
         emit ``license.clock_anomaly`` and return read-only with
         ``reason='clock_rollback'`` WITHOUT advancing ``last_seen_date``.
      2. Resolve the key (DB wins, else ``LICENSE_KEY`` env → persisted).
      3. No key at all → read-only ``reason='no_license'`` (AC6).
      4. Otherwise validate offline via ``licensing.validate_license`` (T3).
      5. On a clock-consistent pass, persist ``today`` as ``last_seen_date``.
      6. Emit ``license.validated`` and any grace/read-only transition events.

    Pure-read callers (e.g. the T5 gate / T6 status route) can pass
    ``persist=False, emit=False`` to compute status without side effects.

    Never depends on the network. ``public_key`` is forwarded to
    ``validate_license`` so tests can exercise the path with a throwaway key.
    """
    today = today or datetime.date.today()

    # 1. Clock-rollback guard — runs first, independent of key validity.
    stored_last_seen = kv_get(LICENSE_LAST_SEEN_KV)
    if stored_last_seen:
        last_seen = _parse_date(stored_last_seen)
        if last_seen is not None and today < last_seen - datetime.timedelta(
            days=CLOCK_TOLERANCE_DAYS
        ):
            if emit:
                record_event(
                    "license.clock_anomaly",
                    {"last_seen": str(last_seen), "now": str(today)},
                )
            # Treat as read-only until the clock is consistent again. Do NOT
            # advance last_seen (the clock cannot be trusted right now).
            return {"status": LicenseStatus.READONLY, "reason": "clock_rollback"}

    # 2. Resolve the installed key.
    key_string = kv_get(LICENSE_KEY_KV)
    if not key_string:
        env_key = os.getenv("LICENSE_KEY")
        if env_key:
            key_string = env_key
            if persist:
                kv_set(LICENSE_KEY_KV, key_string)

    # 3 / 4. No key → read-only "no valid license" (AC6); else validate offline.
    if not key_string:
        result = {"status": LicenseStatus.READONLY, "reason": "no_license"}
    else:
        result = validate_license(key_string, public_key)

    # 5. Clock was consistent → record today as the new baseline.
    if persist:
        kv_set(LICENSE_LAST_SEEN_KV, today.isoformat())

    # 6. Telemetry: per-check + transition events.
    if emit:
        _emit_status_events(result, persist=persist)

    return result


def get_current_license_status(public_key=None) -> dict:
    """Side-effect-free read of the current license status.

    Used by the T5 run gate and T6 status route so they reuse one evaluation
    path without re-persisting state or double-emitting telemetry.
    """
    return evaluate_license(public_key=public_key, persist=False, emit=False)


# ===========================================================================
# Telemetry helpers
# ===========================================================================
def _emit_status_events(result: dict, *, persist: bool) -> None:
    """Emit license.validated plus first-crossing grace/read-only events.

    Per-check and transition events carry the customer + dates only (PII guard);
    they are only meaningful for a verified key, so the no-key / invalid cases
    emit nothing here. Transition events fire only when the status changes from
    the previously stored status.
    """
    status = result.get("status")
    customer = result.get("customer")
    expires_at = result.get("expires_at")
    prev_status = kv_get(LICENSE_LAST_STATUS_KV)

    if customer is not None and expires_at is not None:
        record_event(
            "license.validated",
            {
                "customer": customer,
                "status": status,
                "expires_at": expires_at,
                "days_remaining": result.get("days_remaining", 0),
            },
        )
        if status != prev_status:
            if status == LicenseStatus.GRACE:
                record_event(
                    "license.entered_grace",
                    {"customer": customer, "expires_at": expires_at},
                )
            elif status == LicenseStatus.READONLY:
                record_event(
                    "license.entered_readonly",
                    {"customer": customer, "expires_at": expires_at},
                )

    if persist:
        kv_set(LICENSE_LAST_STATUS_KV, status)


def _parse_date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# Startup hook + periodic scheduler (mirrors jobs/baseline_calculator.py style)
# ===========================================================================
def run_startup_validation() -> dict | None:
    """One-shot startup validation. Never raises — startup must not fail.

    Called by the app lifespan after the ensure_* table hooks. Runs regardless
    of ``AGENTIQ_DISABLE_BACKGROUND_JOBS`` (it is a one-shot, not a background
    job); only the periodic re-check is gated by that flag.
    """
    try:
        result = evaluate_license()
        logger.info(
            "license startup validation: status=%s reason=%s",
            result.get("status"),
            result.get("reason"),
        )
        return result
    except Exception:  # pragma: no cover — defensive; startup must never fail
        logger.exception("license startup validation failed; treating as unlicensed")
        return None


def run_license_check() -> None:
    """Periodic job entry point — re-evaluate the license. Never raises."""
    try:
        evaluate_license()
    except Exception:  # pragma: no cover — a scheduled check must not crash
        logger.exception("periodic license validation failed")


def _shutdown_scheduler(*_args) -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def _register_sigterm_handler() -> None:
    global _sigterm_handler_registered
    if not _sigterm_handler_registered:
        try:
            signal.signal(signal.SIGTERM, _shutdown_scheduler)
            _sigterm_handler_registered = True
        except ValueError:
            # TestClient may run lifespan hooks outside the main interpreter thread.
            pass


def start_license_scheduler() -> BackgroundScheduler:
    """Start the periodic license re-check. Idempotent.

    No ``next_run_time=now`` — startup validation already ran the first check in
    the lifespan, so the first scheduled run is one interval out.
    """
    if scheduler.running:
        _register_sigterm_handler()
        return scheduler

    scheduler.add_job(
        run_license_check,
        trigger="interval",
        hours=LICENSE_CHECK_INTERVAL_HOURS,
        id=LICENSE_JOB_ID,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    _register_sigterm_handler()
    return scheduler


def stop_license_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
