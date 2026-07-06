"""
Database connection pool factory — T2-S10-A.

Design invariants:
  - Pools are keyed by (org_id, connector_id).  One isolated pool per
    workspace per connector.  Cross-workspace reuse is structurally impossible.
  - Credentials (actual username/password values) exist only as local
    variables inside _IsolatedPool._make_connection().  They are never stored
    on pool objects, config objects, or at module scope.
  - _IsolatedPool._config holds only env-var key *names* (username_key,
    password_key), never the resolved values.
  - resolve_db_credentials() is the single credential-resolution path.  It
    resolves per-org from the encrypted vault (R17-D3 Addendum A, T11/AC8) via
    app.auth.credentials.get_connector_credentials, keyed by
    (config.org_id, config.connector_id) — the same one path every SaaS
    ingestor uses.  resolve_secret() remains only as the CLI/standalone env
    fallback used when the org has no vaulted credential.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Any, Callable

try:
    from backend.app.db_connectors.models import DBConnectorConfig, DBConnectionError
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level
    from app.db_connectors.models import DBConnectorConfig, DBConnectionError

# ---------------------------------------------------------------------------
# Pool constants  (spec: T2-S10-A table 9)
# ---------------------------------------------------------------------------

MAX_POOL_SIZE_PER_CONNECTOR: int = 3
MIN_POOL_SIZE_PER_CONNECTOR: int = 1
POOL_TIMEOUT_S: int = 10
QUERY_TIMEOUT_S: int = 30
MAX_ROWS_PER_QUERY: int = 10_000

# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

def resolve_secret(env_key: str) -> str:
    """Return the secret stored under *env_key* in the process environment.

    Raises KeyError when the variable is absent so callers receive an explicit
    failure rather than a silent None.  This is the CLI/standalone fallback used
    by resolve_db_credentials() when the org has no vaulted credential — never
    the primary path on a shared multi-tenant instance (per-client DB secrets
    are purged from .env by R17-D3 Addendum A, T13).
    """
    value = os.environ.get(env_key)
    if value is None:
        raise KeyError(
            f"Secret env var '{env_key}' is not set. "
            "Configure it before creating a connection pool."
        )
    return value


def resolve_db_credentials(config: DBConnectorConfig) -> tuple[str, str]:
    """Return (username, password) for a DB connector, resolved per org.

    R17-D3 Addendum A (T11 / AC8): DB connector credentials resolve through the
    ONE per-org vault path — ``app.auth.credentials.get_connector_credentials``
    — keyed by ``(config.org_id, config.connector_id)``, exactly like the SaaS
    ingestors.  A static credential entered per org through the Integration Hub
    form (T12) and stored Fernet-encrypted in the vault (T10) is the primary
    source; the DB host/port/database remain instance configuration.

    Only when the org has NO vaulted credential does this fall back to the
    documented CLI/standalone env vars (``config.username_key`` /
    ``config.password_key``) — a single-tenant convenience, never a per-client
    secret on a shared instance.  The vault lookup is defensive: any
    infrastructure failure (no DB reachable, no vault key) degrades to the env
    fallback rather than crashing pool creation.  A missing credential in BOTH
    the vault and env surfaces as ``DBConnectionError(error_code='missing_secret')``
    at the call site — a clear 'not configured' state, never a silent success
    (AC11).  Resolved values are returned to the caller only, never logged.
    """
    record = None
    static_record_type = None
    try:
        try:
            # Prefer the top-level ``app`` path: it is the one the running app and
            # the contract-test DB patching use, so the vault read hits the same
            # (patched) connection the rest of the credential layer does.
            from app.auth.credentials import try_get_connector_credentials
            from app.auth.models import StaticCredentialRecord as _SCR
        except ModuleNotFoundError:  # Imported as backend.app.* in some contexts.
            from backend.app.auth.credentials import try_get_connector_credentials
            from backend.app.auth.models import StaticCredentialRecord as _SCR
        static_record_type = _SCR
        record = try_get_connector_credentials(config.org_id, config.connector_id)
    except Exception:
        # Vault/DB unavailable (no DB, no vault key, table absent). Degrade to the
        # env fallback rather than crash — matches the project's degrade-not-crash
        # rule and keeps CLI/standalone use working. Never surfaces the credential.
        record = None

    if static_record_type is not None and isinstance(record, static_record_type):
        return record.username, record.secret

    # CLI/standalone fallback — dynamic env read, per-deployment only.
    return resolve_secret(config.username_key), resolve_secret(config.password_key)


# ---------------------------------------------------------------------------
# Driver factory registry — populated by T3/T4/T5 at import time
# ---------------------------------------------------------------------------

# Maps connector_id -> Callable(config, username, password) -> connection
_driver_factories: dict[str, Callable[[DBConnectorConfig, str, str], Any]] = {}


def register_driver(
    connector_id: str,
    factory: Callable[[DBConnectorConfig, str, str], Any],
) -> None:
    """Register a driver-specific connection factory.

    Called once at import time by each driver module (T3 SQL Server, T4 Oracle,
    T5 PostgreSQL).  factory(config, username, password) must return an opaque
    connection object and raise on failure.
    """
    _driver_factories[connector_id] = factory


# ---------------------------------------------------------------------------
# Isolated pool
# ---------------------------------------------------------------------------

class _IsolatedPool:
    """Thread-safe connection pool for exactly one (org_id, connector_id) pair.

    _config stores only env-var key names (DBConnectorConfig.username_key,
    DBConnectorConfig.password_key).  Actual credential values are resolved
    inside _make_connection() as local variables and discarded on return.
    """

    def __init__(self, config: DBConnectorConfig) -> None:
        self.org_id: str = config.org_id
        self.connector_id: str = config.connector_id
        # Structural proof: key encodes both dimensions — reuse across orgs
        # is impossible because keys never collide across distinct org_ids.
        self.key: tuple[str, str] = (config.org_id, config.connector_id)

        # Stored for lazy connection creation only.  Contains no secrets:
        # username_key and password_key are env-var *names*, not values.
        self._config: DBConnectorConfig = config

        self._pool: queue.Queue[Any] = queue.Queue(maxsize=MAX_POOL_SIZE_PER_CONNECTOR)
        # _size_lock guards _total_created only; never held while calling
        # _make_connection() to avoid re-entrant deadlock.
        self._size_lock: threading.Lock = threading.Lock()
        self._total_created: int = 0

        # Pre-warm MIN connections.  Failure propagates to get_or_create_pool()
        # before the pool is registered, giving the caller a clean error.
        for _ in range(MIN_POOL_SIZE_PER_CONNECTOR):
            conn = self._make_connection()
            # Increment outside _make_connection to keep that method lock-free.
            with self._size_lock:
                self._total_created += 1
            self._pool.put_nowait(conn)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def acquire(self, timeout: float = POOL_TIMEOUT_S) -> Any:
        """Return a live connection from the pool.

        Strategy (in order):
          1. Non-blocking get from queue.
          2. If queue is empty and pool is below MAX_POOL_SIZE, create a new
             connection under lock.
          3. Block up to *timeout* seconds for a released connection.
          4. Raise DBConnectionError(error_code='pool_exhausted').
        """
        try:
            return self._pool.get(block=False)
        except queue.Empty:
            pass

        # Pool is empty; try to grow if under the limit.
        # Reserve the slot under lock, then create the connection outside the
        # lock so _make_connection() can safely use _size_lock itself.
        should_create = False
        with self._size_lock:
            if self._total_created < MAX_POOL_SIZE_PER_CONNECTOR:
                self._total_created += 1  # reserve slot optimistically
                should_create = True

        if should_create:
            try:
                return self._make_connection()
            except Exception:
                # Creation failed — release the reserved slot.
                with self._size_lock:
                    self._total_created -= 1
                raise

        # At capacity — wait for a connection to be released.
        try:
            return self._pool.get(timeout=timeout)
        except queue.Empty:
            raise DBConnectionError(
                f"Pool ({self.org_id!r}, {self.connector_id!r}) exhausted; "
                "no connection became available within the timeout.",
                error_code="pool_exhausted",
            )

    def release(self, conn: Any) -> None:
        """Return *conn* to the pool.  Silently discards if the pool is full."""
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _make_connection(self) -> Any:
        """Create one new connection via the registered driver factory.

        Credentials are resolved here as local variables and overwritten before
        the function returns.  They are never stored on self or returned to the
        caller.
        """
        driver_fn = _driver_factories.get(self.connector_id)
        if driver_fn is None:
            raise DBConnectionError(
                f"No driver registered for connector_id='{self.connector_id}'. "
                "Import the driver module before creating a pool.",
                error_code="driver_not_registered",
            )

        username: str = ""
        password: str = ""
        try:
            username, password = resolve_db_credentials(self._config)
            conn = driver_fn(self._config, username, password)
        except DBConnectionError:
            raise
        except KeyError as exc:
            raise DBConnectionError(str(exc), error_code="missing_secret") from exc
        except Exception as exc:
            raise DBConnectionError(
                f"Driver '{self.connector_id}' failed to create connection: "
                f"{type(exc).__name__}",
                error_code="connection_failed",
            ) from exc
        finally:
            # Overwrite locals so credential values leave the stack as soon
            # as possible, even if an exception is in flight.
            username = ""
            password = ""

        return conn


# ---------------------------------------------------------------------------
# Pool registry
# ---------------------------------------------------------------------------

_pool_registry: dict[tuple[str, str], _IsolatedPool] = {}
_registry_lock: threading.Lock = threading.Lock()


def get_or_create_pool(config: DBConnectorConfig) -> _IsolatedPool:
    """Return the pool for (config.org_id, config.connector_id), creating it
    if it does not yet exist.

    Thread-safe.  Pool creation happens under the registry lock so two threads
    racing on the same key will not create duplicate pools.

    Raises DBConnectionError when the driver is not registered or when
    credential resolution or the initial connection attempt fails.  On failure
    the pool is NOT added to the registry.
    """
    key = (config.org_id, config.connector_id)

    with _registry_lock:
        pool = _pool_registry.get(key)
        if pool is not None:
            return pool

        # Create under lock — prevents duplicate pool for same key.
        new_pool = _IsolatedPool(config)
        _pool_registry[key] = new_pool
        return new_pool


def clear_pool(org_id: str, connector_id: str) -> None:
    """Remove the pool for (org_id, connector_id) from the registry.

    Does not close connections currently held by callers.
    Intended for testing and explicit connector teardown.
    """
    with _registry_lock:
        _pool_registry.pop((org_id, connector_id), None)


def clear_all_pools() -> None:
    """Remove all pools from the registry.  Intended for tests only."""
    with _registry_lock:
        _pool_registry.clear()
