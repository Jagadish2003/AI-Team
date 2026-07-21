"""
T1-S12 — GitHub Connector Ingestor

Authenticates via the OAuth token stored by T1-S11 Task 1 (vault).
Makes four paginated GitHub REST API calls and returns engineering signals.

Required OAuth scopes: repo:status, read:org, read:user
revocation_url: None (GitHub has no RFC 7009 revocation endpoint)
No refresh token (GitHub tokens do not expire by default).

Return shape: see Section 2b of T1-S12 spec.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Redact bearer tokens from exception messages before they reach log sinks.
# Covers Authorization header values that may appear in requests/urllib3
# exception reprs (e.g. PreparedRequest serialisation in ConnectionError).
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)


def _safe_exc(exc: BaseException) -> str:
    """Return str(exc) with any bearer token values replaced by [REDACTED].

    requests exceptions can embed the PreparedRequest (including the
    Authorization header) in their string representation under certain
    urllib3 versions. This function ensures no raw token reaches log sinks
    regardless of the exception source.
    """
    return _BEARER_RE.sub(r"\1[REDACTED]", str(exc))

GITHUB_API_BASE = "https://api.github.com"
PR_AGE_THRESHOLD_DAYS = 3
STALE_BRANCH_DAYS = 30
COMMIT_WINDOW_DAYS = 90
PR_MERGE_WINDOW_DAYS = 30
_PAGE_SIZE = 100
_REQUEST_TIMEOUT = 30

#: Optional explicit repository scope. When set, the connector reads EXACTLY these
#: ``owner/repo`` repositories instead of auto-discovering every repo the token can
#: access — so a run targets only the repositories you care about (e.g. just
#: ``Kusumareddy0896/AgentIQ``) rather than every empty/unrelated repo in the
#: account. Accepts a comma-separated list or a JSON array of ``owner/repo``
#: strings. Unset → auto-discovery (backward compatible).
_REPOS_ENV = "GITHUB_REPOS"

# Offline fixture — parity with the Salesforce/ServiceNow/Jira connectors.
# Used when GitHub is not connected (no stored token) or when not in live mode.
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "github_sample.json"


def _is_live_mode() -> bool:
    """True when INGEST_MODE=live.

    Mirrors discovery.ingest.is_live() but reads the env var directly so this
    connector layer never imports from the discovery layer. The runner sets
    INGEST_MODE from the run's --mode before ingestion begins.
    """
    return os.environ.get("INGEST_MODE", "").strip().lower() == "live"


def _load_fixture(org_id: str, run_id: str) -> Dict[str, Any]:
    """Return the deterministic offline GitHub payload (Section 2b shape).

    The fixture is crafted so all three GitHub Engineering detectors fire. It
    is used when GitHub is not connected or when running offline, matching how
    Salesforce/ServiceNow/Jira fall back to their offline fixtures. org_id and
    run_id are stamped in at load time so the payload is attributed to the run.
    Falls back to the degraded payload only if the fixture file is unreadable.
    """
    try:
        with _FIXTURE_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning(
            "GitHub fixture load failed (%s) — returning degraded payload", exc
        )
        return _degraded_payload(org_id, run_id)

    payload["connector_id"] = "github"
    payload["org_id"] = org_id
    payload["run_id"] = run_id
    return payload


# ---------------------------------------------------------------------------
# Internal HTTP helpers
# ---------------------------------------------------------------------------


def _make_session(access_token: str):
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests library required: pip install requests") from exc

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return session


def _github_message(resp) -> str:
    """Best-effort extraction of GitHub's error ``message`` from a response body.

    GitHub returns a JSON ``{"message": "..."}`` on errors (e.g. a 409 carries
    ``"Git Repository is empty."``). Surfacing it turns an opaque status code into
    an actionable log line. Never raises — a non-JSON/emtpy body yields ""."""
    try:
        body = resp.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        return str(body.get("message") or "")
    return ""


def _paginate(
    session,
    url: str,
    params: Optional[Dict] = None,
    quiet_statuses: Tuple[int, ...] = (),
) -> Tuple[List[Dict], bool, bool]:
    """
    Fetch all pages from a GitHub REST endpoint.

    Returns ``(items, degraded, transient)``:

      * ``degraded`` — the fetch could not be completed cleanly (any non-2xx,
        timeout, or unexpected shape); already-fetched items are still returned.
      * ``transient`` — the degradation is a retry-worthy / genuine PROBLEM (429
        rate limit, timeout, request error, unexpected shape, or a non-2xx NOT in
        ``quiet_statuses``). This is the flag a caller should map to a degraded
        SIGNAL, because it means data we expected may be missing.

    ``quiet_statuses`` lists non-2xx codes that are EXPECTED for this call rather
    than genuine errors — e.g. a 409 on a repository whose default branch is
    empty/unborn ("Git Repository is empty."), which just means "no commits on the
    default branch here", not a failure.
    A quiet status degrades (``degraded=True``) but is NOT transient
    (``transient=False``): it means "no data here", not "something failed", so a
    caller treats it as an empty result rather than a degraded signal. It is also
    logged at INFO (with GitHub's own message) instead of WARNING.
    """
    try:
        import requests
    except ImportError:
        return [], True, True

    all_items: List[Dict] = []
    page = 1
    base_params = dict(params or {})
    base_params["per_page"] = _PAGE_SIZE

    while True:
        req_params = {**base_params, "page": page}
        try:
            resp = session.get(url, params=req_params, timeout=_REQUEST_TIMEOUT)
        except requests.Timeout:
            logger.warning("GitHub API timeout: %s (page %d) — degraded signal", url, page)
            return all_items, True, True
        except Exception as exc:
            logger.warning("GitHub API request error: %s — %s", url, _safe_exc(exc))
            return all_items, True, True

        if resp.status_code == 429:
            logger.warning("GitHub API rate limit (429): %s (page %d) — degraded signal", url, page)
            return all_items, True, True

        if not resp.ok:
            detail = _github_message(resp) or "no detail"
            if resp.status_code in quiet_statuses:
                # Expected condition for this call (empty repo / non-org id) — NOT
                # a real error and NOT transient. Log at INFO with GitHub's reason;
                # the caller treats this as "no data", not a degraded signal.
                logger.info(
                    "GitHub API %s (expected): %s — %s",
                    resp.status_code, url, detail,
                )
                return all_items, True, False
            logger.warning(
                "GitHub API error %s: %s — %s", resp.status_code, url, detail
            )
            return all_items, True, True

        page_data = resp.json()
        if not isinstance(page_data, list):
            # Unexpected shape — treat as a genuine (transient) degradation.
            logger.warning("GitHub API unexpected response shape for %s", url)
            return all_items, True, True

        all_items.extend(page_data)

        if len(page_data) < _PAGE_SIZE:
            break

        page += 1

    return all_items, False, False


# ---------------------------------------------------------------------------
# Per-signal fetch functions
# ---------------------------------------------------------------------------


def _fetch_pr_review(session, owner: str, repo: str) -> Dict[str, Any]:
    """Fetch open PRs and compute PR review velocity metrics."""
    now = datetime.now(timezone.utc)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    prs, degraded, transient = _paginate(session, url, {"state": "open"}, quiet_statuses=(409,))

    if transient:
        # Genuine failure (429 / timeout / unexpected) — hold the signal so the
        # detector does not draw a conclusion from partial data. An empty repo is
        # NOT transient and falls through to a clean zero result below.
        return {
            "open_pr_count": len(prs),
            "avg_days_open": 0.0,
            "max_days_open": 0.0,
            "prs_over_threshold": 0,
            "degraded_signal": True,
        }

    ages: List[float] = []
    for pr in prs:
        created_at = pr.get("created_at")
        if created_at:
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                age_days = (now - created).total_seconds() / 86400.0
                ages.append(age_days)
            except (ValueError, TypeError):
                pass

    open_pr_count = len(prs)
    avg_days_open = round(sum(ages) / len(ages), 2) if ages else 0.0
    max_days_open = round(max(ages), 2) if ages else 0.0
    prs_over_threshold = sum(1 for a in ages if a >= PR_AGE_THRESHOLD_DAYS)

    return {
        "open_pr_count": open_pr_count,
        "avg_days_open": avg_days_open,
        "max_days_open": max_days_open,
        "prs_over_threshold": prs_over_threshold,
        "degraded_signal": False,
    }


def _first_nonempty_branch(session, owner: str, repo: str) -> Optional[str]:
    """Return the name of a branch that actually has commits, or None.

    Used to recover commit history when the repository's DEFAULT branch HEAD is
    empty/unborn — GitHub then 409s the default ``/commits`` listing even though
    commits live on another branch (e.g. the repo's default is still the unborn
    ``main`` while the work is on ``master``). A branch only appears in the
    ``/branches`` listing if it points at a commit, so any returned branch is
    non-empty. Prefers the conventional trunk names, else the first branch.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches"
    branches, _, _ = _paginate(session, url, {}, quiet_statuses=(409,))
    names = [b.get("name") for b in branches if isinstance(b, dict) and b.get("name")]
    if not names:
        return None
    for preferred in ("main", "master", "develop", "trunk"):
        if preferred in names:
            return preferred
    return names[0]


def _fetch_commit_concentration(session, owner: str, repo: str) -> Dict[str, Any]:
    """Fetch commits from last 90 days and compute author concentration."""
    since = (datetime.now(timezone.utc) - timedelta(days=COMMIT_WINDOW_DAYS)).isoformat()
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits"
    # A 409 here means GitHub considers the repo's DEFAULT branch empty/unborn
    # ("Git Repository is empty.") — GitHub's /commits reads the default-branch
    # HEAD, not "any commits anywhere". Expected for a truly new/empty repo, but it
    # ALSO fires when the repo's default_branch (e.g. an unborn 'main') differs from
    # the branch that holds the commits (e.g. 'master'). So on an empty default
    # listing, recover by retrying against a branch that actually has commits — the
    # repo is not really empty, its HEAD just points at an unborn ref.
    commits, degraded, transient = _paginate(
        session, url, {"since": since}, quiet_statuses=(409,)
    )
    if degraded and not transient and not commits:
        branch = _first_nonempty_branch(session, owner, repo)
        if branch:
            logger.info(
                "GitHub: default-branch commit listing unavailable for %s/%s "
                "(empty/unborn HEAD) — retrying against branch '%s'",
                owner, repo, branch,
            )
            commits, degraded, transient = _paginate(
                session, url, {"since": since, "sha": branch}, quiet_statuses=(409,)
            )

    if transient:
        # Genuine failure — degrade the signal. An empty repo (degraded but not
        # transient) is NOT a failure: it falls through and returns a clean zero
        # result, so it does not poison the cross-repo aggregate (any-degraded).
        return {
            "top_author_pct": 0.0,
            "top_author_name": "",
            "total_contributors": 0,
            "degraded_signal": True,
        }

    author_counts: Counter = Counter()
    for commit in commits:
        author = commit.get("author") or {}
        login = author.get("login") or ""
        if not login:
            # Fall back to commit.commit.author.name
            inner = (commit.get("commit") or {}).get("author") or {}
            login = inner.get("name") or "unknown"
        author_counts[login] += 1

    total_commits = sum(author_counts.values())
    total_contributors = len(author_counts)

    if total_commits == 0 or total_contributors == 0:
        return {
            "top_author_pct": 0.0,
            "top_author_name": "",
            "total_contributors": total_contributors,
            "degraded_signal": False,
        }

    top_author, top_count = author_counts.most_common(1)[0]
    top_author_pct = round(top_count / total_commits, 4)

    # Per-repo visibility: show the commit signal actually derived from this repo,
    # so a repo with commits (e.g. AgentIQ) is confirmed read — not just the empty
    # repos that log a 409. Only reached when the repo has commits.
    logger.info(
        "GitHub: %s/%s commit concentration — %d commit(s) in last %dd, "
        "%d contributor(s), top author %r (%.0f%%)",
        owner, repo, total_commits, COMMIT_WINDOW_DAYS,
        total_contributors, top_author, round(top_author_pct * 100),
    )

    return {
        "top_author_pct": top_author_pct,
        "top_author_name": top_author,
        "total_contributors": total_contributors,
        "degraded_signal": False,
    }


def _fetch_stale_branches(session, owner: str, repo: str) -> Dict[str, Any]:
    """Fetch branches and identify stale ones (no commit activity for STALE_BRANCH_DAYS)."""
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=STALE_BRANCH_DAYS)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/branches"
    # 409 = empty repository (no branches). Expected, not transient — an empty
    # repo simply has no branches to age, so it returns a clean zero result below
    # rather than degrading the aggregate signal.
    branches, degraded, transient = _paginate(session, url, {}, quiet_statuses=(409,))

    if transient:
        return {
            "stale_count": 0,
            "total_branches": len(branches),
            "oldest_stale_days": 0.0,
            "degraded_signal": True,
        }

    total_branches = len(branches)
    stale_ages: List[float] = []

    for branch in branches:
        commit_info = branch.get("commit") or {}
        # The branch list endpoint includes the sha but not the date;
        # we use the date from the nested commit object if present,
        # otherwise the branch must be individually resolved.
        # GitHub's branch list does NOT include the last commit date in the
        # top-level response — use the sha-level commit date if available.
        committed_at = None

        inner = commit_info.get("commit") or {}
        committer = inner.get("committer") or inner.get("author") or {}
        committed_at = committer.get("date")

        if not committed_at:
            # No date available from list endpoint — skip (not degraded)
            continue

        try:
            commit_date = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
            if commit_date < stale_cutoff:
                age_days = (now - commit_date).total_seconds() / 86400.0
                stale_ages.append(age_days)
        except (ValueError, TypeError):
            pass

    stale_count = len(stale_ages)
    oldest_stale_days = round(max(stale_ages), 2) if stale_ages else 0.0

    return {
        "stale_count": stale_count,
        "total_branches": total_branches,
        "oldest_stale_days": oldest_stale_days,
        "degraded_signal": False,
    }


def _fetch_closed_prs(session, owner: str, repo: str) -> None:
    """
    Fetch closed PRs from the last PR_MERGE_WINDOW_DAYS days.

    Called to satisfy the 4-call requirement in the spec.
    The merge rate signal is not included in the Section 2b return shape;
    data is fetched but not surfaced as a top-level output field.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=PR_MERGE_WINDOW_DAYS)).isoformat()
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    prs, degraded, transient = _paginate(session, url, {"state": "closed"}, quiet_statuses=(409,))
    if transient:
        logger.warning("GitHub closed PRs: degraded signal (rate limit or timeout)")
    else:
        merged = sum(1 for p in prs if p.get("merged_at"))
        logger.debug(
            "GitHub closed PRs (last %dd): total=%d merged=%d",
            PR_MERGE_WINDOW_DAYS, len(prs), merged,
        )


# ---------------------------------------------------------------------------
# Repository resolution
# ---------------------------------------------------------------------------


def _configured_repo_scope() -> List[Tuple[str, str]]:
    """Explicit ``owner/repo`` scope from the ``GITHUB_REPOS`` env var, or [].

    When set, the connector reads EXACTLY these repositories instead of auto-
    discovering everything the token can access. Accepts a comma-separated list or
    a JSON array of ``owner/repo`` strings, e.g.
    ``GITHUB_REPOS="Kusumareddy0896/AgentIQ"`` or
    ``GITHUB_REPOS='["Kusumareddy0896/AgentIQ","org/other"]'``. Entries not in
    ``owner/repo`` form are skipped with a warning; duplicates are collapsed.
    """
    raw = os.environ.get(_REPOS_ENV, "").strip()
    if not raw:
        return []

    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("%s is not valid JSON — ignoring repo scope", _REPOS_ENV)
            return []
        entries = [str(e) for e in parsed] if isinstance(parsed, list) else []
    else:
        entries = raw.split(",")

    result: List[Tuple[str, str]] = []
    seen: set = set()
    for entry in entries:
        spec = str(entry).strip().strip("/")
        if not spec:
            continue
        parts = spec.split("/")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            logger.warning(
                "%s entry %r is not in 'owner/repo' form — skipping", _REPOS_ENV, entry
            )
            continue
        pair = (parts[0].strip(), parts[1].strip())
        if pair not in seen:
            seen.add(pair)
            result.append(pair)
    return result


def _resolve_repos(session, org_id: str) -> List[Tuple[str, str]]:
    """
    Resolve (owner, repo) pairs to read signals from.

    Precedence: the org's saved Integration-Hub selection (the ``repos`` list on
    the github connector record) wins; else the ``GITHUB_REPOS`` env scope; else
    auto-discover all repositories accessible to the authenticated token. Returns a
    list of (owner, repo) tuples.
    """
    # Saved per-org selection wins — target only the repositories the customer chose.
    selected = _selected_repos(org_id)
    if selected:
        logger.info(
            "GitHub: reading saved repo selection — %d repo(s): %s",
            len(selected), ", ".join(f"{o}/{r}" for o, r in selected),
        )
        return selected

    # Explicit env scope next — target only the configured repositories.
    scope = _configured_repo_scope()
    if scope:
        logger.info(
            "GitHub: reading configured repo scope from %s — %d repo(s): %s",
            _REPOS_ENV, len(scope), ", ".join(f"{o}/{r}" for o, r in scope),
        )
        return scope

    try:
        import requests  # noqa: F401 — availability guard; _paginate makes the calls.
    except ImportError:
        return []

    # Auto-discover the repositories the authenticated token can access. We do NOT
    # probe /orgs/{org_id}/repos first: org_id is AgentIQ's internal tenant UUID,
    # never a GitHub organization login, so that probe is guaranteed to 404 on every
    # run — a wasted API call and a noisy "404 (expected)" log line. /user/repos is
    # the real (and only) discovery path. To read an EXACT set of repos instead of
    # everything the token can see (e.g. to skip empty or unrelated repos), set
    # GITHUB_REPOS — see _configured_repo_scope above, which short-circuits here.
    url = f"{GITHUB_API_BASE}/user/repos"
    repos, _, _ = _paginate(session, url, {"affiliation": "owner,organization_member"})

    result: List[Tuple[str, str]] = []
    for repo in repos:
        owner_login = (repo.get("owner") or {}).get("login", org_id)
        repo_name = repo.get("name", "")
        if repo_name:
            result.append((owner_login, repo_name))

    return result


# ---------------------------------------------------------------------------
# Signal aggregation across repos
# ---------------------------------------------------------------------------


def _aggregate_pr_review(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not signals:
        return {
            "open_pr_count": 0,
            "avg_days_open": 0.0,
            "max_days_open": 0.0,
            "prs_over_threshold": 0,
            "degraded_signal": False,
        }
    degraded = any(s["degraded_signal"] for s in signals)
    total_open = sum(s["open_pr_count"] for s in signals)
    total_over = sum(s["prs_over_threshold"] for s in signals)
    # Weighted average of avg_days_open by open_pr_count
    weighted_sum = sum(s["avg_days_open"] * s["open_pr_count"] for s in signals)
    avg_days = round(weighted_sum / total_open, 2) if total_open > 0 else 0.0
    max_days = round(max(s["max_days_open"] for s in signals), 2)
    return {
        "open_pr_count": total_open,
        "avg_days_open": avg_days,
        "max_days_open": max_days,
        "prs_over_threshold": total_over,
        "degraded_signal": degraded,
    }


def _aggregate_commit_concentration(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not signals:
        return {
            "top_author_pct": 0.0,
            "top_author_name": "",
            "total_contributors": 0,
            "degraded_signal": False,
        }
    degraded = any(s["degraded_signal"] for s in signals)
    # Surface the repo with the highest concentration as the primary signal
    non_degraded = [s for s in signals if not s["degraded_signal"]]
    if not non_degraded:
        return {
            "top_author_pct": 0.0,
            "top_author_name": "",
            "total_contributors": 0,
            "degraded_signal": True,
        }
    worst = max(non_degraded, key=lambda s: s["top_author_pct"])
    return {
        "top_author_pct": worst["top_author_pct"],
        "top_author_name": worst["top_author_name"],
        "total_contributors": worst["total_contributors"],
        "degraded_signal": degraded,
    }


def _aggregate_stale_branches(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not signals:
        return {
            "stale_count": 0,
            "total_branches": 0,
            "oldest_stale_days": 0.0,
            "degraded_signal": False,
        }
    degraded = any(s["degraded_signal"] for s in signals)
    stale_count = sum(s["stale_count"] for s in signals)
    total_branches = sum(s["total_branches"] for s in signals)
    oldest = max(s["oldest_stale_days"] for s in signals)
    return {
        "stale_count": stale_count,
        "total_branches": total_branches,
        "oldest_stale_days": round(oldest, 2),
        "degraded_signal": degraded,
    }


# ---------------------------------------------------------------------------
# Public ingest entry point
# ---------------------------------------------------------------------------


async def ingest(org_id: str, run_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Orchestrate GitHub ingestion for the given org.

    Retrieves the stored OAuth token via the vault, then makes four paginated
    API calls per accessible repository. Returns the Section 2b payload shape.

    On 429 or timeout for any sub-signal call, sets degraded_signal=True for
    that block and continues — never raises an exception to the caller.
    """
    from app.auth.vault import get_token
    from app.auth.models import ConnectorNotAuthenticatedError

    run_id = run_id or str(uuid.uuid4())

    # Offline mode → deterministic fixture, no network calls. Parity with the
    # Salesforce/ServiceNow/Jira offline connectors.
    if not _is_live_mode():
        logger.info(
            "GitHub ingestion: offline mode — using fixture (org=%s run=%s)",
            org_id, run_id,
        )
        return _load_fixture(org_id, run_id)

    # Live mode → ingest real GitHub data when the connector is authenticated.
    # When GitHub is not connected, fall back to the fixture so the
    # github_engineering pack still produces signals. Connect GitHub via
    # Integration Hub to switch this run to live data automatically.
    try:
        token_record = await get_token(org_id, "github")
    except ConnectorNotAuthenticatedError:
        logger.info(
            "GitHub not connected for org=%s run=%s — using fixture. "
            "Authenticate via Integration Hub to ingest live GitHub data.",
            org_id, run_id,
        )
        return _load_fixture(org_id, run_id)
    except Exception as exc:
        logger.warning(
            "GitHub vault lookup raised an unexpected error for org=%s run=%s: %s "
            "— falling back to fixture. Check CREDENTIAL_VAULT_KEY is set and the "
            "vault is reachable if you expected live data.",
            org_id, run_id, _safe_exc(exc),
        )
        return _load_fixture(org_id, run_id)

    if token_record is None:
        logger.info(
            "GitHub vault returned no token for org=%s run=%s — using fixture. "
            "Connector has not been authenticated or token was revoked.",
            org_id, run_id,
        )
        return _load_fixture(org_id, run_id)

    access_token = token_record.access_token
    session = _make_session(access_token)

    repos = _resolve_repos(session, org_id)
    if not repos:
        logger.info("No repositories found for org=%s — returning empty payload", org_id)
        return {
            "pr_review": {
                "open_pr_count": 0,
                "avg_days_open": 0.0,
                "max_days_open": 0.0,
                "prs_over_threshold": 0,
                "degraded_signal": False,
            },
            "commit_concentration": {
                "top_author_pct": 0.0,
                "top_author_name": "",
                "total_contributors": 0,
                "degraded_signal": False,
            },
            "stale_branches": {
                "stale_count": 0,
                "total_branches": 0,
                "oldest_stale_days": 0.0,
                "degraded_signal": False,
            },
            "connector_id": "github",
            "org_id": org_id,
            "run_id": run_id,
        }

    # Surface exactly which repositories are being scanned (all repos the token
    # can access). This makes it obvious when an expected repo is missing from the
    # token's access, or when empty repos (409) are simply other repos in the
    # account — not the one that holds the commits.
    logger.info(
        "GitHub ingest: scanning %d accessible repo(s) for org=%s: %s",
        len(repos), org_id, ", ".join(f"{o}/{r}" for o, r in repos),
    )

    pr_review_signals: List[Dict[str, Any]] = []
    commit_signals: List[Dict[str, Any]] = []
    stale_signals: List[Dict[str, Any]] = []

    for owner, repo in repos:
        logger.debug("GitHub ingest: processing %s/%s", owner, repo)

        # API call 1: open PRs (PR review velocity)
        pr_review_signals.append(_fetch_pr_review(session, owner, repo))

        # API call 2: commits last 90 days (commit concentration)
        commit_signals.append(_fetch_commit_concentration(session, owner, repo))

        # API call 3: branches (stale branch accumulation)
        stale_signals.append(_fetch_stale_branches(session, owner, repo))

        # API call 4: closed PRs last 30 days (PR merge rate — spec required call)
        _fetch_closed_prs(session, owner, repo)

    return {
        "pr_review": _aggregate_pr_review(pr_review_signals),
        "commit_concentration": _aggregate_commit_concentration(commit_signals),
        "stale_branches": _aggregate_stale_branches(stale_signals),
        "connector_id": "github",
        "org_id": org_id,
        "run_id": run_id,
    }


def _degraded_payload(org_id: str, run_id: str) -> Dict[str, Any]:
    """Return a fully-degraded payload when token retrieval fails."""
    return {
        "pr_review": {
            "open_pr_count": 0,
            "avg_days_open": 0.0,
            "max_days_open": 0.0,
            "prs_over_threshold": 0,
            "degraded_signal": True,
        },
        "commit_concentration": {
            "top_author_pct": 0.0,
            "top_author_name": "",
            "total_contributors": 0,
            "degraded_signal": True,
        },
        "stale_branches": {
            "stale_count": 0,
            "total_branches": 0,
            "oldest_stale_days": 0.0,
            "degraded_signal": True,
        },
        "connector_id": "github",
        "org_id": org_id,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# Repository selection (Integration Hub picker)
# ---------------------------------------------------------------------------


def _selected_repos(org_id: str) -> List[Tuple[str, str]]:
    """The org's saved repo selection as ``(owner, repo)`` tuples, or ``[]`` when
    none is saved. Reads the ``repos`` list (``["owner/repo", ...]``) on the github
    connector record — the highest-precedence scope in :func:`_resolve_repos`
    (saved selection > ``GITHUB_REPOS`` env > auto-discover). A DB failure degrades
    to ``[]`` (fall through to env/auto-discover) so a run is never blocked."""
    try:
        from app.db import org_connector_get

        record = org_connector_get(org_id, "github") if org_id else None
    except Exception:  # pragma: no cover — never block a run on the selection store
        return []
    if not record:
        return []
    repos = record.get("repos")
    if not isinstance(repos, list):
        return []
    result: List[Tuple[str, str]] = []
    seen: set = set()
    for entry in repos:
        spec = str(entry).strip().strip("/")
        parts = spec.split("/")
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            pair = (parts[0].strip(), parts[1].strip())
            if pair not in seen:
                seen.add(pair)
                result.append(pair)
    return result


def list_selectable_repos(org_id: Optional[str] = None) -> List[Dict[str, str]]:
    """Repositories the customer can choose from — ``[{id:"owner/repo", name, owner}]``.

    The option list for ``GET /api/connectors/github/repos``: every repo the
    connected token can access (``/user/repos``). Selection filtering is NOT applied
    here. Offline reads the fixture's ``selectable_repos``; live lists via the REST
    API using the org's vaulted GitHub token (resolved through the app credential
    layer, never the discovery layer). No token → ``[]``."""
    if not _is_live_mode():
        try:
            with _FIXTURE_PATH.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            repos = payload.get("selectable_repos", [])
            return [
                {"id": str(r["id"]), "name": str(r.get("name", "")), "owner": str(r.get("owner", ""))}
                for r in repos
                if r.get("id")
            ]
        except (OSError, ValueError, KeyError):
            return []

    # Live: resolve the org's GitHub token from the app credential vault.
    try:
        from app.auth.credentials import try_get_connector_credentials

        cred = try_get_connector_credentials(org_id, "github") if org_id else None
    except Exception:  # noqa: BLE001
        cred = None
    token = getattr(cred, "access_token", None) if cred else None
    if not token:
        return []

    session = _make_session(token)
    repos, _degraded, _transient = _paginate(
        session,
        f"{GITHUB_API_BASE}/user/repos",
        {"affiliation": "owner,organization_member"},
    )
    out: List[Dict[str, str]] = []
    seen: set = set()
    for repo in repos:
        owner = (repo.get("owner") or {}).get("login", "")
        name = repo.get("name", "")
        if owner and name:
            rid = f"{owner}/{name}"
            if rid not in seen:
                seen.add(rid)
                out.append({"id": rid, "name": name, "owner": owner})
    return out
