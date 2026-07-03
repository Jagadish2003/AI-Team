"""Teams Graph client: cumulative 429 throttle-wait budget (Phase 3.3).

The client retries a throttled (HTTP 429) request honouring ``Retry-After``, but
the total time it will spend sleeping across a single ingest is bounded by
``_MAX_TOTAL_THROTTLE_WAIT_SECONDS`` so a heavily throttled tenant cannot stall a
run indefinitely. Exceeding the budget raises ``TeamsIngestError`` (non-blocking:
the caller degrades to an empty corroboration block).

These tests drive ``TeamsGraphClient._get`` directly with a fake throttling
session and a patched ``time.sleep`` (no real waiting), so they are fast and do
not depend on live credentials or ingest mode.
"""
from __future__ import annotations

import pytest

from discovery.ingest import teams as teams_mod
from discovery.ingest.teams import TeamsGraphClient, TeamsIngestError


class _FakeResp:
    def __init__(self, status_code, retry_after=None):
        self.status_code = status_code
        self.ok = status_code < 400
        self.headers = {} if retry_after is None else {"Retry-After": str(retry_after)}

    def json(self):
        return {"value": []}


class _FakeSession:
    """Returns a scripted sequence of responses; records .get() calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return _FakeResp(200)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(teams_mod.time, "sleep", lambda s: slept.append(s))
    return slept


def _client_with(session, monkeypatch):
    client = TeamsGraphClient("dummy-token")
    monkeypatch.setattr(client, "_sess", lambda: session)
    return client


def test_budget_exhausted_raises_before_sleeping_past_limit(monkeypatch, _no_real_sleep):
    # Budget 5s, Retry-After 3s: first 429 sleeps 3s (3<=5); second 429 would make
    # it 6s (>5) so the client gives up instead of sleeping past the budget.
    monkeypatch.setattr(teams_mod, "_MAX_TOTAL_THROTTLE_WAIT_SECONDS", 5)
    session = _FakeSession([_FakeResp(429, 3), _FakeResp(429, 3), _FakeResp(200)])
    client = _client_with(session, monkeypatch)

    with pytest.raises(TeamsIngestError):
        client._get("https://graph.microsoft.com/v1.0/teams")

    assert sum(_no_real_sleep) <= 5, f"slept past budget: {_no_real_sleep}"
    assert client._throttle_waited <= 5


def test_within_budget_retries_then_succeeds(monkeypatch, _no_real_sleep):
    # One 429 (Retry-After 2s) then 200 — well under the default budget, so the
    # request succeeds after a single bounded wait.
    monkeypatch.setattr(teams_mod, "_MAX_TOTAL_THROTTLE_WAIT_SECONDS", 120)
    session = _FakeSession([_FakeResp(429, 2), _FakeResp(200)])
    client = _client_with(session, monkeypatch)

    out = client._get("https://graph.microsoft.com/v1.0/teams")
    assert out == {"value": []}
    assert _no_real_sleep == [2]


def test_budget_is_cumulative_across_requests(monkeypatch, _no_real_sleep):
    # The budget spans the client's lifetime (one ingest), not a single request.
    # Budget 5s, Retry-After 3s: first _get spends 3s and succeeds; a second _get
    # that is throttled again would push past 5s and must give up.
    monkeypatch.setattr(teams_mod, "_MAX_TOTAL_THROTTLE_WAIT_SECONDS", 5)
    client = _client_with(_FakeSession([_FakeResp(429, 3), _FakeResp(200)]), monkeypatch)
    assert client._get("https://graph.microsoft.com/v1.0/teams") == {"value": []}

    # Same client, next request is throttled again — cumulative wait would exceed 5.
    monkeypatch.setattr(client, "_sess", lambda: _FakeSession([_FakeResp(429, 3)]))
    with pytest.raises(TeamsIngestError):
        client._get("https://graph.microsoft.com/v1.0/teams/x/channels")
