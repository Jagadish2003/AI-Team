"""
Database connection pool factory — T2-S10-A.

Pools are keyed by (org_id, connector_id).  Cross-workspace reuse is
structurally impossible.  Credentials exist only as local variables inside
_IsolatedPool._make_connection() and are discarded on return.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Any, Callable

from backend.app.db_connectors.models import DBConnectorConfig, DBConnectionError

MAX_POOL_SIZE_PER_CONNECTOR: int = 3
MIN_POOL_SIZE_PER_CONNECTOR: int = 1
POOL_TIMEOUT_S: int = 10
QUERY_TIMEOUT_S: int = 30
MAX_ROWS_PER_QUERY: int = 10_000

# ---------------------------------------------------------------------------
# Credential resolution bootstrap (T1-S10-A vault replaces this stub)
# ---------------------------------------------------------------------------

def resolve_secret(env_key: str) -> str:
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

_driver_factories: dict[str, Callable[[DBConnectorConfig, str, str], Any]] = {}


def register_driver(
    connector_id: str,
    factory: Callable[[DBConnectorConfig, str, str], Any],
) -> None:
    """Register a driver-specific connection factory (called by T3/T4/T5)."""
    _driver_factories[connector_id] = factory


# ---------------------------------------------------------------------------
# Isolated pool
# ---------------------------------------------------------------------------

class _IsolatedPool:
    """Thread-safe pool for exactly one (org_id, connector_id) pair.

    _config stores env-var key names only — never resolved secret values.
    """

    def __init__(self, config: DBConnectorConfig) -> None:
        self.org_id: str = config.org_id
        self.connector_id: str = config.connector_id
        self.key: tuple[str, str] = (config.org_id, config.connector_id)
        self._config: DBConnectorConfig = config
        self._pool: queue.Queue[Any] = queue.Queue(maxsize=MAX_POOL_SIZE_PER_CONNECTOR)
        # _size_lock guards _total_created only; never held while calling
        # _make_connection() to avoid re-entrant deadlock.
        self._size_lock: threading.Lock = threading.Lock()
        self._total_created: int = 0

        for _ in range(MIN_POOL_SIZE_PER_CONNECTOR):
            conn = self._make_connection()
            with self._size_lock:
                self._total_created += 1
            self._pool.put_nowait(conn)

    def acquire(self, timeout: float = POOL_TIMEOUT_S) -> Any:
        try:
            return self._pool.get(block=False)
        except queue.Empty:
            pass

        should_create = False
        with self._size_lock:
            if self._total_created < MAX_POOL_SIZE_PER_CONNECTOR:
                self._total_created += 1
                should_create = True

        if should_create:
            try:
                return self._make_connection()
            except Exception:
                with self._size_lock:
                    self._total_created -= 1
                raise

        try:
            return self._pool.get(timeout=timeout)
        except queue.Empty:
            raise DBConnectionError(
                f"Pool ({self.org_id!r}, {self.connector_id!r}) exhausted.",
                error_code="pool_exhausted",
            )

    def release(self, conn: Any) -> None:
        try:
            self._pool.put_nowait(conn)
        except queue.Full:
            try:
                conn.close()
            except Exception:
                pass

    def _make_connection(self) -> Any:
        driver_fn = _driver_factories.get(self.connector_id)
        if driver_fn is None:
            raise DBConnectionError(
                f"No driver registered for connector_id='{self.connector_id}'.",
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
                f"Driver '{self.connector_id}' failed: {type(exc).__name__}",
                error_code="connection_failed",
            ) from exc
        finally:
            username = ""
            password = ""
        return conn


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_pool_registry: dict[tuple[str, str], _IsolatedPool] = {}
_registry_lock: threading.Lock = threading.Lock()


def get_or_create_pool(config: DBConnectorConfig) -> _IsolatedPool:
    key = (config.org_id, config.connector_id)
    with _registry_lock:
        pool = _pool_registry.get(key)
        if pool is not None:
            return pool
        new_pool = _IsolatedPool(config)
        _pool_registry[key] = new_pool
        return new_pool


def clear_pool(org_id: str, connector_id: str) -> None:
    with _registry_lock:
        _pool_registry.pop((org_id, connector_id), None)


def clear_all_pools() -> None:
    with _registry_lock:
        _pool_registry.clear()
