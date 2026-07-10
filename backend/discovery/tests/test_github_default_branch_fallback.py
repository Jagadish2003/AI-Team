"""GitHub signal connector — default-branch commit recovery (operational fix).

GitHub's ``/repos/{owner}/{repo}/commits`` reads the repository's *default
branch* HEAD, not "any commits anywhere". A repo whose default_branch is an
unborn 'main' while the commits live on 'master' therefore 409s the default
listing even though the repo is NOT empty. These tests cover the connector
recovering the commit-concentration signal by retrying against a branch that
actually has commits, and staying degraded only when the repo is truly empty.
"""
from __future__ import annotations

from connectors.saas import github


class _Resp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """Minimal stand-in for a requests.Session driven by a URL/params router."""

    def __init__(self, router):
        self._router = router
        self.calls = []

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        return self._router(url, params)


_EMPTY_MSG = {"message": "Git Repository is empty."}


def test_commit_concentration_recovers_from_unborn_default_branch():
    # Default-branch /commits 409s; commits live on 'master'.
    def router(url, params):
        if url.endswith("/commits") and "sha" not in params:
            return _Resp(409, _EMPTY_MSG)  # default (unborn 'main')
        if url.endswith("/branches"):
            return _Resp(200, [{"name": "master", "commit": {"sha": "abc"}}])
        if url.endswith("/commits") and params.get("sha") == "master":
            return _Resp(200, [
                {"author": {"login": "alice"}},
                {"author": {"login": "alice"}},
                {"author": {"login": "bob"}},
            ])
        return _Resp(404, {"message": "unexpected"})

    session = _FakeSession(router)
    result = github._fetch_commit_concentration(session, "Kusumareddy0896", "apex")

    # The repo was NOT empty — the signal is recovered from 'master', not degraded.
    assert result["degraded_signal"] is False
    assert result["total_contributors"] == 2
    assert result["top_author_name"] == "alice"
    assert result["top_author_pct"] == round(2 / 3, 4)
    # It retried the commits listing against 'master' after the default 409.
    assert any(
        url.endswith("/commits") and params.get("sha") == "master"
        for url, params in session.calls
    )


def test_truly_empty_repo_is_no_data_not_degraded():
    # Both the default /commits AND /branches report empty (409). An empty repo is
    # "no data", NOT a degraded signal — so it returns a clean zero result and does
    # not poison the cross-repo aggregate (any-degraded).
    def router(url, params):
        if url.endswith("/commits"):
            return _Resp(409, _EMPTY_MSG)
        if url.endswith("/branches"):
            return _Resp(409, _EMPTY_MSG)
        return _Resp(404, {"message": "unexpected"})

    session = _FakeSession(router)
    result = github._fetch_commit_concentration(session, "owner", "empty-repo")
    assert result["degraded_signal"] is False
    assert result["total_contributors"] == 0
    assert result["top_author_pct"] == 0.0


def test_transient_failure_is_degraded():
    # A 429/5xx (not a quiet status) IS a genuine failure → degrade the signal so
    # the detector holds off rather than concluding from partial data.
    def router(url, params):
        return _Resp(429, {"message": "rate limited"})

    session = _FakeSession(router)
    result = github._fetch_commit_concentration(session, "owner", "repo")
    assert result["degraded_signal"] is True


def test_first_nonempty_branch_prefers_conventional_trunk():
    def router(url, params):
        return _Resp(200, [
            {"name": "feature-x"},
            {"name": "master"},
            {"name": "feature-y"},
        ])

    session = _FakeSession(router)
    assert github._first_nonempty_branch(session, "o", "r") == "master"


def test_first_nonempty_branch_none_when_no_branches():
    session = _FakeSession(lambda url, params: _Resp(409, _EMPTY_MSG))
    assert github._first_nonempty_branch(session, "o", "r") is None


def test_healthy_default_branch_needs_no_fallback():
    # Default /commits returns commits directly → no /branches call, no retry.
    def router(url, params):
        if url.endswith("/commits") and "sha" not in params:
            return _Resp(200, [{"author": {"login": "carol"}}])
        if url.endswith("/branches"):
            raise AssertionError("must not fall back when the default branch works")
        return _Resp(404, {})

    session = _FakeSession(router)
    result = github._fetch_commit_concentration(session, "o", "r")
    assert result["degraded_signal"] is False
    assert result["top_author_name"] == "carol"
    assert all(not u.endswith("/branches") for u, _ in session.calls)


def test_empty_repo_does_not_degrade_aggregate_with_a_healthy_repo():
    # THE core fix: the commit signal is aggregated across repos with any-degraded.
    # An empty repo now contributes a clean zero (degraded_signal=False), so a
    # healthy repo's signal survives instead of the whole thing degrading.
    empty = {
        "top_author_pct": 0.0, "top_author_name": "", "total_contributors": 0,
        "degraded_signal": False,
    }
    healthy = {
        "top_author_pct": 0.75, "top_author_name": "alice", "total_contributors": 3,
        "degraded_signal": False,
    }
    agg = github._aggregate_commit_concentration([empty, healthy])
    assert agg["degraded_signal"] is False
    assert agg["top_author_name"] == "alice"
    assert agg["top_author_pct"] == 0.75


def test_transient_repo_still_degrades_aggregate():
    healthy = {
        "top_author_pct": 0.5, "top_author_name": "bob", "total_contributors": 2,
        "degraded_signal": False,
    }
    transient = {
        "top_author_pct": 0.0, "top_author_name": "", "total_contributors": 0,
        "degraded_signal": True,
    }
    agg = github._aggregate_commit_concentration([healthy, transient])
    # A genuine failure in any repo still degrades — that's correct (data missing).
    assert agg["degraded_signal"] is True


# ─────────────────────────────────────────────────────────────────────────────
# GITHUB_REPOS — explicit repository scope (read exactly the configured repos)
# ─────────────────────────────────────────────────────────────────────────────
def test_configured_repo_scope_single(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOS", "Kusumareddy0896/AgentIQ")
    assert github._configured_repo_scope() == [("Kusumareddy0896", "AgentIQ")]


def test_configured_repo_scope_comma_separated(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOS", "org/one, org/two ,org/one")
    # De-duplicated, order preserved.
    assert github._configured_repo_scope() == [("org", "one"), ("org", "two")]


def test_configured_repo_scope_json_array(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOS", '["Kusumareddy0896/AgentIQ", "acme/api"]')
    assert github._configured_repo_scope() == [
        ("Kusumareddy0896", "AgentIQ"),
        ("acme", "api"),
    ]


def test_configured_repo_scope_skips_malformed(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOS", "good/repo, not-a-pair, a/b/c, /x, y/")
    assert github._configured_repo_scope() == [("good", "repo")]


def test_configured_repo_scope_unset_is_empty(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOS", raising=False)
    assert github._configured_repo_scope() == []


def test_resolve_repos_uses_scope_without_discovery(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOS", "Kusumareddy0896/AgentIQ")

    class _BoomSession:
        def get(self, *a, **k):
            raise AssertionError("must not hit the API when a scope is configured")

    # session is never touched because the configured scope short-circuits.
    assert github._resolve_repos(_BoomSession(), "org-uuid") == [
        ("Kusumareddy0896", "AgentIQ")
    ]
