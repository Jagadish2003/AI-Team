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
  - resolve_secret() is the only credential-resolution path.  T1-S10-A will
    replace the env-var bootstrap stub with the real vault implementation;
    the call-site signature is identical.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Any, Callable

from backend.app.db_connectors.models import DBConnectorConfig, DBConnectionError

# ---------------------------------------------------------------------------
# Pool constants  (spec: T2-S10-A table 9)
# ---------------------------------------------------------------------------

MAX_POOL_SIZE_PER_CONNECTOR: int = 3
MIN_POOL_SIZE_PER_CONNECTOR: int = 1
POOL_TIMEOUT_S: int = 10
QUERY_TIMEOUT_S: int = 30
MAX_ROWS_PER_QUERY: int = 10_000

# ---------------------------------------------------------------------------
# Credential resolution — bootstrap stub  (T1-S10-A owns the real vault)
# ---------------------------------------------------------------------------

def resolve_secret(env_key: str) -> str:
    """Return the secret stored under *env_key* in the process environment.

    Raises KeyError when the variable is absent so callers receive an explicit
    failure rather than a silent None.  T1-S10-A will swap this stub for the
    per-workspace credential vault without changing the call signature.
    """
    value = os.environ.get(env_key)
    if value is None:
        raise KeyError(
            f"Secret env var '{env_key}' is not set. "
            "Configure it before creating a connection pool."
        )
    return value


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
            username = resolve_secret(self._config.username_key)
            password = resolve_secret(self._config.password_key)
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
