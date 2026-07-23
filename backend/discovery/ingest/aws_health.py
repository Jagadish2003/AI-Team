"""MSP-B1 / AT-646 (T6) — per-account failure loudness for the AWS connector.

The native AWS connector polls many accounts under one connection. When one
account's role is revoked, its credentials expire, or its API throttles, that must
be **loud** — surfaced per account in run health (the R18-C2 connector panel) —
never a silent skip that quietly thins the data and lets a run look complete when
it isn't. This module is the health surface the poll source records into:

* :class:`AWSAccountHealth` — one account's outcome across its scopes: which
  surfaces succeeded, which failed (with the reason), whether authentication
  failed outright, and how many times it was throttled + backed off.
* :class:`AWSConnectorHealth` — the aggregate over all polled accounts, with a
  JSON-serialisable :meth:`to_dict` that is the run-record / R18-C2 panel artifact
  (the same pattern as the B7 ``budget_report``).

The load-bearing rule (AT-646): a failure is recorded and logged at WARNING, and
partial success is reported as ``partial`` — one account failing never removes the
others' data and never hides its own absence. Throttling backs off and is counted;
it does not thin the data quietly.

This is a pure data + logging surface (no boto3, no network), so it is fully
testable offline and drops into the future run-health wiring unchanged.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Per-account status vocabulary (resolved from what happened during the poll).
STATUS_OK = "ok"                 # every scope for the account polled cleanly
STATUS_AUTH_FAILED = "auth_failed"  # credentials/role assumption failed — no data
STATUS_PARTIAL = "partial"       # some scopes succeeded, some failed
STATUS_FAILED = "failed"         # every attempted scope failed (non-auth)

#: AWS throttling error codes (botocore ``ClientError`` ``Error.Code`` values).
_THROTTLE_CODES = frozenset({
    "Throttling",
    "ThrottlingException",
    "ThrottledException",
    "RequestLimitExceeded",
    "TooManyRequestsException",
    "RequestThrottled",
    "SlowDown",
})


def is_throttle_error(exc: BaseException) -> bool:
    """True when ``exc`` is an AWS throttling / rate-limit error.

    Detected without importing botocore: a botocore ``ClientError`` carries
    ``.response['Error']['Code']``; failing that, the exception class name is
    matched against the throttle families. So a real ClientError and a test double
    that mimics either shape are both recognised.
    """
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code") or "")
        if code in _THROTTLE_CODES:
            return True
    name = type(exc).__name__
    return any(tok in name for tok in ("Throttl", "TooManyRequests", "RequestLimitExceeded"))


@dataclass
class AWSAccountHealth:
    """One managed account's outcome across the surfaces polled for it."""

    account_id: str
    auth_failed: bool = False
    message: str = ""
    scopes_ok: List[str] = field(default_factory=list)
    scopes_failed: Dict[str, str] = field(default_factory=dict)  # surface → reason
    throttle_events: int = 0

    @property
    def status(self) -> str:
        if self.auth_failed:
            return STATUS_AUTH_FAILED
        if self.scopes_failed and self.scopes_ok:
            return STATUS_PARTIAL
        if self.scopes_failed:
            return STATUS_FAILED
        return STATUS_OK

    @property
    def healthy(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "status": self.status,
            "message": self.message,
            "surfaces_ok": sorted(self.scopes_ok),
            "surfaces_failed": dict(self.scopes_failed),
            "throttle_events": self.throttle_events,
        }


class AWSConnectorHealth:
    """Aggregate per-account health for one AWS connector run (loud, never silent)."""

    connector_id = "aws_events"

    def __init__(self) -> None:
        self._accounts: Dict[str, AWSAccountHealth] = {}

    def _get(self, account_id: str) -> AWSAccountHealth:
        health = self._accounts.get(account_id)
        if health is None:
            health = AWSAccountHealth(account_id=account_id)
            self._accounts[account_id] = health
        return health

    # -- record (each failure is logged loudly) ------------------------------
    def mark_scope_ok(self, account_id: str, surface: str) -> None:
        health = self._get(account_id)
        if surface not in health.scopes_ok:
            health.scopes_ok.append(surface)

    def mark_auth_failed(self, account_id: str, message: str) -> None:
        health = self._get(account_id)
        if not health.auth_failed:
            health.auth_failed = True
            health.message = message
            logger.warning(
                "aws_events: account %s authentication failed — %s (this account is "
                "reported failed in run health; other accounts continue)",
                account_id, message,
            )

    def mark_scope_failed(self, account_id: str, surface: str, message: str) -> None:
        health = self._get(account_id)
        health.scopes_failed[surface] = message
        logger.warning(
            "aws_events: account %s surface %s failed — %s (reported in run health, "
            "not silently skipped)",
            account_id, surface, message,
        )

    def record_throttle(self, account_id: str, surface: str, attempt: int) -> None:
        health = self._get(account_id)
        health.throttle_events += 1
        logger.warning(
            "aws_events: account %s surface %s throttled — backing off (attempt %d); "
            "data is retried, never thinned",
            account_id, surface, attempt,
        )

    # -- read ----------------------------------------------------------------
    def accounts(self) -> List[AWSAccountHealth]:
        return [self._accounts[a] for a in sorted(self._accounts)]

    @property
    def all_healthy(self) -> bool:
        return all(a.healthy for a in self._accounts.values())

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable run-health / R18-C2 connector-panel artifact."""
        accounts = self.accounts()
        return {
            "connector": self.connector_id,
            "all_healthy": self.all_healthy,
            "failed_accounts": [a.account_id for a in accounts if not a.healthy],
            "accounts": [a.to_dict() for a in accounts],
        }
