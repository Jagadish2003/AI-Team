"""LIC-1 / T4 (AT-345) — startup + periodic license validation runtime.

This is the *runtime wiring* layer of the offline license scheme. It is the
only LIC-1 module that has side effects:

  * It resolves the installed license key **per organisation** from the
    ``org_licenses`` table (one row per org; a row exists only once an Owner
    installs a key). There is intentionally NO ``LICENSE_KEY`` env fallback:
    licensing is per-tenant, so a never-licensed org evaluates to ``no_license``
    until its Owner pastes a key on the License page.
  * It persists the per-org clock-rollback baseline (``last_seen_date``) and the
    cached status (``last_status``) back onto that same row — these used to be
    the app-global ``license:last_seen_date`` / ``license:last_status`` KV slots
    and are now scoped per org.
  * It applies the light clock-rollback guard (§6).
  * It emits license telemetry on each check and on state transitions.

Validation itself (signature + expiry/grace/read-only) lives in
``app.licensing.validate_license`` (T3) and is pure/side-effect-free; this
module calls it and layers per-org persistence, the clock guard, telemetry, and
the APScheduler periodic job on top.

``main.py`` calls :func:`run_startup_validation` once at startup and
:func:`start_license_scheduler` for the periodic re-check (the latter gated by
``AGENTIQ_DISABLE_BACKGROUND_JOBS``). Both iterate every licensed org.

Offline by design: nothing here makes a network call.
"""
from __future__ import annotations

import datetime
import logging
import os
import signal

from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .licensing import LicenseStatus, validate_license
from .telemetry import record_event

logger = logging.getLogger(__name__)

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
# Per-org storage (org_licenses table)
# ===========================================================================
def read_org_license(org_id: str) -> dict | None:
    """Return ``{license_key, last_seen_date, last_status}`` for an org, or None.

    None means the org has no installed key (no row) — the caller treats this as
    ``no_license``. Read through the raw psycopg2 layer (app.db), mirroring the
    rest of the app's KV/table access.
    """
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT license_key, last_seen_date, last_status "
            "FROM org_licenses WHERE org_id = %s",
            (org_id,),
        )
        row = cur.fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {
        "license_key": row[0],
        "last_seen_date": row[1],
        "last_status": row[2],
    }


def set_org_license_key(org_id: str, key: str) -> None:
    """Install (upsert) an org's license key. Leaves last_seen/last_status alone."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO org_licenses (org_id, license_key) VALUES (%s, %s) "
            "ON CONFLICT (org_id) DO UPDATE SET "
            "license_key = EXCLUDED.license_key, updated_at = now()",
            (org_id, key),
        )
        con.commit()
    finally:
        con.close()


def persist_org_status(org_id: str, last_seen: str | None, last_status: str | None) -> None:
    """Write the per-org clock baseline + cached status — UPDATE existing row ONLY.

    Never inserts a row: a keyless org must stay row-less (so it keeps evaluating
    to ``no_license``) and the ``license_key`` NOT NULL column can't be violated.
    ``last_seen`` is left untouched when None (e.g. a status-only pin), so the
    clock baseline is not wiped by a transition write.
    """
    con = db.connect()
    try:
        cur = con.cursor()
        if last_seen is not None:
            cur.execute(
                "UPDATE org_licenses SET last_seen_date = %s, last_status = %s, "
                "updated_at = now() WHERE org_id = %s",
                (last_seen, last_status, org_id),
            )
        else:
            cur.execute(
                "UPDATE org_licenses SET last_status = %s, updated_at = now() "
                "WHERE org_id = %s",
                (last_status, org_id),
            )
        con.commit()
    finally:
        con.close()


def all_licensed_org_ids() -> list[str]:
    """Every org that has an installed key — the set startup/periodic checks iterate."""
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("SELECT org_id FROM org_licenses")
        rows = cur.fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


# ===========================================================================
# Core evaluation
# ===========================================================================
def evaluate_license(
    *,
    org_id: str,
    today: datetime.date | None = None,
    public_key=None,
    persist: bool = True,
    emit: bool = True,
) -> dict:
    """Evaluate one org's installed license and return its status dict.

    Order of operations:
      1. Clock-rollback guard (§6 / AC8). If the org's stored ``last_seen_date``
         is more than ``CLOCK_TOLERANCE_DAYS`` in the future relative to
         ``today``, emit ``license.clock_anomaly`` and return read-only with
         ``reason='clock_rollback'`` WITHOUT advancing ``last_seen_date``.
      2. Read the org's installed key from ``org_licenses``.
      3. No row / no key → read-only ``reason='no_license'`` (AC6).
      4. Otherwise validate offline via ``licensing.validate_license`` (T3).
      5. On a clock-consistent pass, persist ``today`` as the org's last_seen_date
         (UPDATE-only — keyless orgs have no row to update).
      6. Emit ``license.validated`` and any grace/read-only transition events.

    Pure-read callers (e.g. the T5 gate / T6 status route) pass
    ``persist=False, emit=False`` to compute status without side effects.

    Never depends on the network. ``public_key`` is forwarded to
    ``validate_license`` so tests can exercise the path with a throwaway key.
    """
    today = today or datetime.date.today()

    record = read_org_license(org_id)

    # 1. Clock-rollback guard — runs first, independent of key validity.
    stored_last_seen = record.get("last_seen_date") if record else None
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

    # 2 / 3. Resolve the org's installed key; none → read-only "no valid license".
    key_string = record.get("license_key") if record else None
    # Type-safety guard: license_key is a TEXT column (str | None), but defend
    # against a non-string ever reaching validate_license().split(".") — e.g. a
    # JSON object written to the slot by a future/other code path. Without this,
    # the split would AttributeError, get swallowed in verify_license_signature,
    # and the org would sit in permanent read-only with no indication why.
    if key_string is not None and not isinstance(key_string, str):
        logger.warning(
            "org %s license_key is %s, not str — treating as no_license",
            org_id,
            type(key_string).__name__,
        )
        key_string = None
    if not key_string:
        result = {"status": LicenseStatus.READONLY, "reason": "no_license"}
    else:
        # 4. Offline validation of the installed key.
        result = validate_license(key_string, public_key)

    # 5. Clock was consistent → record today as the org's new baseline. UPDATE-only,
    # so a keyless (row-less) org persists nothing and stays ``no_license``.
    if persist:
        persist_org_status(org_id, today.isoformat(), result.get("status"))

    # 6. Telemetry: per-check + transition events (uses the row's prior status).
    if emit:
        _emit_status_events(result, org_id=org_id, prev_status=(record or {}).get("last_status"))

    return result


def get_current_license_status(*, org_id: str | None = None, public_key=None) -> dict:
    """Side-effect-free read of an org's current license status.

    Used by the T5 run gate and T6 status/banner routes so they reuse one
    evaluation path without re-persisting state or double-emitting telemetry.
    When ``org_id`` is omitted it resolves the current request's org from the
    tenancy context (lazy import to avoid an import cycle); a missing context
    falls back to the default org.
    """
    if org_id is None:
        org_id = _resolve_context_org_id()
    return evaluate_license(org_id=org_id, public_key=public_key, persist=False, emit=False)


def persist_validated_status(
    result: dict, *, org_id: str, today: datetime.date | None = None
) -> None:
    """Persist last_seen_date + last_status for an org from an already-computed result.

    The admin update-key route (T6) validates the pasted key, stores it (creating
    the org's row), then calls this so the cached status and clock baseline
    immediately reflect the freshly installed key — instead of lagging until the
    next startup/periodic check. Deliberately does NOT re-verify the signature
    (the caller already validated) and does NOT emit telemetry (the caller emits
    ``license.updated``).
    """
    today = today or datetime.date.today()
    persist_org_status(org_id, today.isoformat(), result.get("status"))


def _resolve_context_org_id() -> str:
    """Best-effort current-request org id, defaulting to the dev/default org.

    Lazy import keeps license_runtime free of a hard dependency on the middleware
    package at import time and avoids a cycle.
    """
    try:
        from .middleware.tenancy import DEV_DEFAULT_ORG, get_current_org_id_optional

        return get_current_org_id_optional() or DEV_DEFAULT_ORG
    except Exception:  # pragma: no cover — defensive; never break a status read
        return "default"


# ===========================================================================
# Telemetry helpers
# ===========================================================================
def _emit_status_events(result: dict, *, org_id: str, prev_status: str | None) -> None:
    """Emit license.validated plus first-crossing grace/read-only events.

    Per-check and transition events carry the customer + dates only (PII guard);
    they are only meaningful for a verified key, so the no-key / invalid cases
    emit nothing here. Transition events fire only when the status changes from
    the org's previously stored status.
    """
    status = result.get("status")
    customer = result.get("customer")
    expires_at = result.get("expires_at")

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


def _parse_date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# ===========================================================================
# Startup hook + periodic scheduler (mirrors jobs/baseline_calculator.py style)
# ===========================================================================
def run_startup_validation() -> None:
    """One-shot startup validation of every licensed org. Never raises.

    Called by the app lifespan after the ensure_* table hooks. Runs regardless
    of ``AGENTIQ_DISABLE_BACKGROUND_JOBS`` (it is a one-shot, not a background
    job); only the periodic re-check is gated by that flag. Orgs with no
    installed key have no row and are simply absent from the iteration.
    """
    try:
        org_ids = all_licensed_org_ids()
    except Exception:  # pragma: no cover — startup must never fail
        logger.exception("license startup validation: could not list licensed orgs")
        return
    for org_id in org_ids:
        try:
            result = evaluate_license(org_id=org_id)
            logger.info(
                "license startup validation: org=%s status=%s reason=%s",
                org_id,
                result.get("status"),
                result.get("reason"),
            )
        except Exception:  # pragma: no cover — defensive; one org must not break startup
            logger.exception("license startup validation failed for org %s", org_id)


def run_license_check() -> None:
    """Periodic job entry point — re-evaluate every licensed org. Never raises.

    Enforcement does NOT depend on this job succeeding: the run gate
    (LicenseGateMiddleware) and the banner endpoint both re-validate the stored
    key LIVE on each request via get_current_license_status(), so a failed
    periodic check cannot open the gate. As defence in depth, when an org's
    evaluation fails we pin THAT org's cached status (last_status) to read-only so
    any cache consumer stays conservative (fail-closed) rather than serving a
    stale "valid".
    """
    try:
        org_ids = all_licensed_org_ids()
    except Exception:  # pragma: no cover — a scheduled check must not crash
        logger.exception("periodic license validation: could not list licensed orgs")
        return
    for org_id in org_ids:
        try:
            evaluate_license(org_id=org_id)
        except Exception:
            logger.exception("periodic license validation failed for org %s", org_id)
            try:
                persist_org_status(org_id, None, LicenseStatus.READONLY)
            except Exception:
                logger.exception(
                    "could not pin license status to read-only for org %s", org_id
                )


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
