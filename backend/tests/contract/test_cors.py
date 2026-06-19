"""Contract tests for CORS configuration (Fix 4 / AT-291).

The permissive ``http://localhost:<port>`` regex is convenient for local dev
but must NOT be active in production, where it would let any localhost process
make authenticated cross-origin requests. These tests pin both modes.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.main import build_cors_kwargs

ALLOWED_ORIGINS = ["http://localhost:5173"]


def _make_client(environment: str) -> TestClient:
    """Build an isolated app whose CORS is configured for `environment`."""
    app = FastAPI()
    app.add_middleware(CORSMiddleware, **build_cors_kwargs(environment, ALLOWED_ORIGINS))

    @app.get("/ping")
    def ping():
        return {"ok": True}

    return TestClient(app)


def _preflight(client: TestClient, origin: str):
    return client.options(
        "/ping",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


def test_production_has_no_localhost_regex():
    """F4-AC1: production drops allow_origin_regex entirely."""
    kwargs = build_cors_kwargs("production", ALLOWED_ORIGINS)
    assert "allow_origin_regex" not in kwargs


def test_production_blocks_arbitrary_localhost_origin():
    """F4-AC1: OPTIONS from an arbitrary localhost port gets no ACAO header."""
    client = _make_client("production")
    resp = _preflight(client, "http://localhost:9999")
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_production_still_allows_explicit_origin():
    """Production still honours explicitly-allowed origins."""
    client = _make_client("production")
    resp = _preflight(client, "http://localhost:5173")
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_development_has_localhost_regex():
    """F4-AC2: development (and unset) keeps allow_origin_regex."""
    assert "allow_origin_regex" in build_cors_kwargs("development", ALLOWED_ORIGINS)
    assert "allow_origin_regex" in build_cors_kwargs("", ALLOWED_ORIGINS)


def test_development_allows_localhost_origin():
    """F4-AC2: OPTIONS from localhost:5173 echoes the origin back."""
    client = _make_client("development")
    resp = _preflight(client, "http://localhost:5173")
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_development_allows_arbitrary_localhost_via_regex():
    """F4-AC2: the regex lets any localhost port through in dev."""
    client = _make_client("development")
    resp = _preflight(client, "http://localhost:9999")
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:9999"


def test_environment_value_is_case_insensitive():
    """`Production` / ` PRODUCTION ` are treated as production."""
    assert "allow_origin_regex" not in build_cors_kwargs("Production", ALLOWED_ORIGINS)
    assert "allow_origin_regex" not in build_cors_kwargs(" PRODUCTION ", ALLOWED_ORIGINS)
