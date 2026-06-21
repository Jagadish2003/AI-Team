"""Unit tests — LIC-1 review fix: license gate is an allow-list (fail-closed).

The run gate must allow a discovery run ONLY when the live status is valid or
grace; readonly/invalid AND any unrecognised/future status must be blocked, and
a status-check exception must fail closed. Exercises the middleware dispatch
directly with a fake request — no HTTP server, no DB.
"""
from __future__ import annotations

import asyncio

import pytest
from starlette.responses import PlainTextResponse

from app.middleware import license_gate


class _URL:
    def __init__(self, path: str) -> None:
        self.path = path


class _Req:
    def __init__(self, method: str, path: str) -> None:
        self.method = method
        self.url = _URL(path)


async def _call_next(_req):
    return PlainTextResponse("ok", status_code=200)


def _dispatch(monkeypatch, status_value, *, path="/api/runs/start", method="POST"):
    if isinstance(status_value, Exception):
        def _boom(*_a, **_k):
            raise status_value
        monkeypatch.setattr(license_gate, "get_current_license_status", _boom)
    else:
        monkeypatch.setattr(
            license_gate, "get_current_license_status", lambda *_a, **_k: {"status": status_value}
        )
    mw = license_gate.LicenseGateMiddleware(app=lambda scope, receive, send: None)
    return asyncio.run(mw.dispatch(_Req(method, path), _call_next))


@pytest.mark.parametrize("status", ["valid", "grace"])
def test_healthy_status_allows_run(monkeypatch, status):
    assert _dispatch(monkeypatch, status).status_code == 200


@pytest.mark.parametrize("status", ["readonly", "invalid"])
def test_unhealthy_status_blocks_run(monkeypatch, status):
    assert _dispatch(monkeypatch, status).status_code == 402


def test_unknown_status_fails_closed(monkeypatch):
    # A status not in the allow-list (e.g. a future "suspended") must be blocked.
    assert _dispatch(monkeypatch, "suspended").status_code == 402


def test_missing_status_fails_closed(monkeypatch):
    assert _dispatch(monkeypatch, None).status_code == 402


def test_status_check_error_fails_closed(monkeypatch):
    assert _dispatch(monkeypatch, RuntimeError("kv down")).status_code == 402


def test_non_gated_path_is_never_blocked(monkeypatch):
    # The banner read is not a run-trigger; it passes through even in readonly.
    assert _dispatch(monkeypatch, "readonly", path="/api/license/banner").status_code == 200


def test_non_post_method_is_not_gated(monkeypatch):
    assert _dispatch(monkeypatch, "readonly", method="GET").status_code == 200
