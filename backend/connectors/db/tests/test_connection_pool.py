"""
Unit tests for the T2-S10-A connection pool factory.

Acceptance criteria verified:
  AC12 — pools keyed by (org_id, connector_id); cross-workspace reuse impossible
  AC13 — credentials not stored on pool objects after creation
  + pool reuse on repeated calls for the same key
  + pool isolation: org_A cannot obtain org_B's pool
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, call, patch

import pytest

from backend.app.db_connectors.models import DBConnectorConfig, DBConnectionError
from backend.connectors.db.connection_pool import (
    MAX_POOL_SIZE_PER_CONNECTOR,
    MIN_POOL_SIZE_PER_CONNECTOR,
    _IsolatedPool,
    _driver_factories,
    _pool_registry,
    clear_all_pools,
    clear_pool,
    get_or_create_pool,
    register_driver,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(org_id: str = "acme", connector_id: str = "sqlserver") -> DBConnectorConfig:
    return DBConnectorConfig(
        connector_id=connector_id,
        org_id=org_id,
        host="db.example.com",
        port=1433,
        database="sales",
        driver="pyodbc",
        username_key="TEST_DB_USER",
        password_key="TEST_DB_PASS",
    )


def _fake_driver(config: DBConnectorConfig, username: str, password: str) -> MagicMock:
    """Minimal driver factory: returns a fresh mock connection object."""
    conn = MagicMock()
    conn.username_captured = None   # deliberately never store username on conn
    conn.password_captured = None   # deliberately never store password on conn
    return conn


ENV = {"TEST_DB_USER": "test_user", "TEST_DB_PASS": "test_pass"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_registry():
    """Each test starts with an empty pool registry and a registered fake driver."""
    clear_all_pools()
    _driver_factories.clear()
    register_driver("sqlserver", _fake_driver)
    register_driver("postgresql", _fake_driver)
    yield
    clear_all_pools()
    _driver_factories.clear()


# ---------------------------------------------------------------------------
# AC12 — pool keyed by (org_id, connector_id)
# ---------------------------------------------------------------------------

class TestPoolKey:
    def test_pool_key_is_org_and_connector(self):
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config("acme", "sqlserver"))
        assert pool.key == ("acme", "sqlserver")
        assert pool.org_id == "acme"
        assert pool.connector_id == "sqlserver"

    def test_same_key_returns_same_pool(self):
        """Repeated calls with identical (org_id, connector_id) return the same object."""
        with patch.dict("os.environ", ENV):
            pool_a = get_or_create_pool(_config("acme", "sqlserver"))
            pool_b = get_or_create_pool(_config("acme", "sqlserver"))
        assert pool_a is pool_b

    def test_different_org_different_pool(self):
        """org_A and org_B each get a distinct pool — cross-workspace reuse impossible."""
        with patch.dict("os.environ", ENV):
            pool_acme = get_or_create_pool(_config("acme", "sqlserver"))
            pool_beta = get_or_create_pool(_config("beta_corp", "sqlserver"))
        assert pool_acme is not pool_beta
        assert pool_acme.key != pool_beta.key

    def test_different_connector_different_pool(self):
        """Same org, different connector_id → distinct pools."""
        with patch.dict("os.environ", ENV):
            pool_sql = get_or_create_pool(_config("acme", "sqlserver"))
            pool_pg = get_or_create_pool(_config("acme", "postgresql"))
        assert pool_sql is not pool_pg
        assert pool_sql.key != pool_pg.key

    def test_org_a_cannot_get_org_b_pool(self):
        """Structural test: pool registry keys never collide across orgs."""
        with patch.dict("os.environ", ENV):
            get_or_create_pool(_config("org_a", "sqlserver"))
            get_or_create_pool(_config("org_b", "sqlserver"))

        keys = list(_pool_registry.keys())
        assert ("org_a", "sqlserver") in keys
        assert ("org_b", "sqlserver") in keys
        # The two entries are distinct objects
        assert _pool_registry[("org_a", "sqlserver")] is not _pool_registry[("org_b", "sqlserver")]


# ---------------------------------------------------------------------------
# AC13 — credentials not retained on pool objects
# ---------------------------------------------------------------------------

class TestCredentialNonRetention:
    def test_pool_has_no_password_attribute(self):
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
        for attr in ("password", "passwd", "secret", "credential"):
            assert not hasattr(pool, attr), f"Pool must not have attribute '{attr}'"

    def test_pool_has_no_username_attribute(self):
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
        for attr in ("username", "user", "login"):
            assert not hasattr(pool, attr), f"Pool must not have attribute '{attr}'"

    def test_config_on_pool_has_no_password_value(self):
        """_config holds key *names* only — not resolved secret values."""
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
        cfg = pool._config
        # username_key / password_key are env-var names, not values
        assert cfg.username_key == "TEST_DB_USER"
        assert cfg.password_key == "TEST_DB_PASS"
        # The config object must not contain the actual secret values
        assert not hasattr(cfg, "password")
        assert not hasattr(cfg, "username")

    def test_resolve_secret_called_with_key_names(self):
        """resolve_secret is invoked with the env-var key name, never the value."""
        with patch(
            "backend.connectors.db.connection_pool.resolve_secret",
            side_effect=lambda k: ENV[k],
        ) as mock_resolve:
            get_or_create_pool(_config())

        # Must have been called for both keys
        mock_resolve.assert_any_call("TEST_DB_USER")
        mock_resolve.assert_any_call("TEST_DB_PASS")

    def test_pool_repr_does_not_contain_secret_values(self):
        """repr() of the pool must not leak resolved credential strings."""
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
        rep = repr(pool)
        assert "test_user" not in rep
        assert "test_pass" not in rep


# ---------------------------------------------------------------------------
# Pool acquire / release behaviour
# ---------------------------------------------------------------------------

class TestAcquireRelease:
    def test_pre_warmed_connection_is_available(self):
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
        conn = pool.acquire()
        assert conn is not None

    def test_release_returns_connection_to_pool(self):
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
        conn = pool.acquire()
        pool.release(conn)
        # Same connection object should be re-acquirable
        conn2 = pool.acquire()
        assert conn2 is conn

    def test_pool_grows_up_to_max_size(self):
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
            # acquire() calls _make_connection() for new connections — must keep
            # env patch active so resolve_secret() can read the vars.
            conns = [pool.acquire() for _ in range(MAX_POOL_SIZE_PER_CONNECTOR)]
        assert len(conns) == MAX_POOL_SIZE_PER_CONNECTOR
        # All connections should be distinct objects
        assert len(set(id(c) for c in conns)) == MAX_POOL_SIZE_PER_CONNECTOR

    def test_pool_exhausted_raises_db_connection_error(self):
        with patch.dict("os.environ", ENV):
            pool = get_or_create_pool(_config())
            # Drain pool to MAX (env must stay active for lazy connection creation)
            held = [pool.acquire() for _ in range(MAX_POOL_SIZE_PER_CONNECTOR)]
            with pytest.raises(DBConnectionError) as exc_info:
                pool.acquire(timeout=0.05)  # short timeout for test speed
            assert exc_info.value.error_code == "pool_exhausted"
            # Cleanup
            for c in held:
                pool.release(c)


# ---------------------------------------------------------------------------
# Pool isolation — thread-safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_pool_creation_same_key_yields_one_pool(self):
        """Two threads racing on the same key must produce exactly one pool."""
        results: list[_IsolatedPool] = []
        errors: list[Exception] = []

        def _create():
            try:
                p = get_or_create_pool(_config("acme", "sqlserver"))
                results.append(p)
            except Exception as exc:
                errors.append(exc)

        # Patch the environment ONCE around all threads. Per-thread patch.dict is
        # not thread-safe — concurrent save/restore of os.environ races and can
        # leak the credential env vars into the real environment, corrupting later
        # tests. Patching in the main thread before the workers start avoids that.
        with patch.dict("os.environ", ENV):
            threads = [threading.Thread(target=_create) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert not errors
        # All threads must have received the same pool object
        assert all(p is results[0] for p in results)


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

class TestRegistryHelpers:
    def test_clear_pool_removes_specific_entry(self):
        with patch.dict("os.environ", ENV):
            get_or_create_pool(_config("acme", "sqlserver"))
            get_or_create_pool(_config("acme", "postgresql"))
        clear_pool("acme", "sqlserver")
        assert ("acme", "sqlserver") not in _pool_registry
        assert ("acme", "postgresql") in _pool_registry

    def test_clear_all_pools_empties_registry(self):
        with patch.dict("os.environ", ENV):
            get_or_create_pool(_config("acme", "sqlserver"))
            get_or_create_pool(_config("beta", "postgresql"))
        clear_all_pools()
        assert len(_pool_registry) == 0


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

class TestErrorCases:
    def test_missing_secret_raises_db_connection_error(self):
        with pytest.raises(DBConnectionError) as exc_info:
            # No env vars patched — resolve_secret will raise KeyError
            get_or_create_pool(_config())
        assert exc_info.value.error_code == "missing_secret"

    def test_unregistered_driver_raises_db_connection_error(self):
        cfg = _config(connector_id="oracle_db")  # not registered in this fixture
        with patch.dict("os.environ", ENV):
            with pytest.raises(DBConnectionError) as exc_info:
                get_or_create_pool(cfg)
        assert exc_info.value.error_code == "driver_not_registered"

    def test_failed_pool_not_added_to_registry(self):
        """If pool creation fails, the registry stays clean."""
        key = ("acme", "sqlserver")
        with pytest.raises(DBConnectionError):
            get_or_create_pool(_config())  # no env vars → missing_secret
        assert key not in _pool_registry
