import os
import subprocess
import sys
import tempfile
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
    # Contract tests must never hit live connectors or external LLM APIs. Force
    # offline ingestion and drop the Anthropic key regardless of the developer's
    # .env (which may set INGEST_MODE=live and ANTHROPIC_API_KEY) so the suite
    # stays fast and deterministic and uses the offline fixtures + deterministic
    # enrichment fallback. Set before app import; load_dotenv() does not override
    # already-set env vars. Tests that exercise live behaviour set their own env
    # via monkeypatch/patch.dict.
    # Setting (not popping) keeps them "present" so app.main's load_dotenv(),
    # which uses override=False, will not re-populate them from .env. An empty
    # ANTHROPIC_API_KEY is treated as "not set" by llm_enrichment.
    os.environ["INGEST_MODE"] = "offline"
    os.environ["ANTHROPIC_API_KEY"] = ""

    try:
        alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
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

    # Seed the dev user as owner of the default org so legacy contract tests
    # pass with RBAC applied. The app's lifespan does this for context-managed
    # TestClients, but some test modules instantiate TestClient(app) directly
    # (no lifespan), so seed here to cover the whole suite. Tests that need a
    # specific role use a fresh org via the X-Org-Id header and are unaffected.
    from app.rbac import seed_owner

    seed_owner("default", os.environ["DEV_JWT"])


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
