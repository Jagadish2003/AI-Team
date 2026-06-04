import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
SEED_LOADER = BACKEND_DIR / "database" / "seed_loader.py"

for path in (str(REPO_ROOT), str(BACKEND_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

# Use a temp DB for contract tests so the live dev.db is never touched
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
TEST_DB_PATH = _tmp_db.name


def pytest_configure(config):
    """Seed a fresh temporary database before any contract tests run."""
    os.environ.setdefault("DEV_JWT", "dev-token-change-me")
    os.environ["DB_PATH"] = TEST_DB_PATH
    os.environ.setdefault("SEED_DIR", str(BACKEND_DIR / "database" / "seed"))
    os.environ.setdefault("CORS_ORIGINS", "http://localhost:5173")

    try:
        alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
        alembic_command.upgrade(alembic_cfg, "head")
    except Exception as exc:
        raise RuntimeError(f"alembic upgrade failed:\n{exc}") from exc

    result = subprocess.run(
        [sys.executable, str(SEED_LOADER)],
        cwd=str(BACKEND_DIR),
        env={**os.environ, "DB_PATH": TEST_DB_PATH, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"seed_loader.py failed:\n{result.stderr}")

    # Some legacy contract tests instantiate TestClient(app) without entering
    # the lifespan context. Seed the default dev owner here as well so RBAC
    # matches normal app startup.
    from database.models.workspace_members import CREATE_WORKSPACE_MEMBERS_TABLE

    with sqlite3.connect(TEST_DB_PATH) as conn:
        conn.execute(CREATE_WORKSPACE_MEMBERS_TABLE)
        conn.execute(
            """
            INSERT OR IGNORE INTO workspace_members (org_id, user_id, role, created_at)
            VALUES (?, ?, 'owner', ?)
            """,
            (
                "default",
                os.environ["DEV_JWT"],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()


def pytest_sessionfinish(session, exitstatus):
    """Clean up the temporary database after the test session."""
    try:
        os.remove(TEST_DB_PATH)
    except OSError:
        pass


@pytest.fixture()
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c
