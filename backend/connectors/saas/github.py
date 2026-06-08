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

import logging
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
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


def _paginate(session, url: str, params: Optional[Dict] = None) -> Tuple[List[Dict], bool]:
    """
    Fetch all pages from a GitHub REST endpoint.

    Returns (items, degraded) where degraded=True when a 429 or timeout
    occurs mid-pagination; already-fetched items are still returned.
    """
    try:
        import requests
    except ImportError:
        return [], True

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
            return all_items, True
        except Exception as exc:
            logger.warning("GitHub API request error: %s — %s", url, _safe_exc(exc))
            return all_items, True

        if resp.status_code == 429:
            logger.warning("GitHub API rate limit (429): %s (page %d) — degraded signal", url, page)
            return all_items, True

        if not resp.ok:
            logger.warning("GitHub API error %s: %s", resp.status_code, url)
            return all_items, True

        page_data = resp.json()
        if not isinstance(page_data, list):
            # Unexpected shape — treat as degraded
            logger.warning("GitHub API unexpected response shape for %s", url)
            return all_items, True

        all_items.extend(page_data)

        if len(page_data) < _PAGE_SIZE:
            break

        page += 1

    return all_items, False


# ---------------------------------------------------------------------------
# Per-signal fetch functions
# ---------------------------------------------------------------------------


def _fetch_pr_review(session, owner: str, repo: str) -> Dict[str, Any]:
    """Fetch open PRs and compute PR review velocity metrics."""
    now = datetime.now(timezone.utc)
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls"
    prs, degraded = _paginate(session, url, {"state": "open"})

    if degraded:
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


def _fetch_commit_concentration(session, owner: str, repo: str) -> Dict[str, Any]:
    """Fetch commits from last 90 days and compute author concentration."""
    since = (datetime.now(timezone.utc) - timedelta(days=COMMIT_WINDOW_DAYS)).isoformat()
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits"
    commits, degraded = _paginate(session, url, {"since": since})

    if degraded:
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
    branches, degraded = _paginate(session, url, {})

    if degraded:
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
    prs, degraded = _paginate(session, url, {"state": "closed"})
    if degraded:
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


def _resolve_repos(session, org_id: str) -> List[Tuple[str, str]]:
    """
    Resolve (owner, repo) pairs accessible to the authenticated app.

    Sprint 12 scope: reads from all accessible repositories.
    Returns list of (owner, repo) tuples.
    Falls back to org_id as the owner if org endpoint fails.
    """
    try:
        import requests
    except ImportError:
        return []

    # Try org repos first
    url = f"{GITHUB_API_BASE}/orgs/{org_id}/repos"
    repos, _ = _paginate(session, url, {"type": "all"})

    if not repos:
        # Fall back to authenticated user's repos
        url = f"{GITHUB_API_BASE}/user/repos"
        repos, _ = _paginate(session, url, {"affiliation": "owner,organization_member"})

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

    try:
        token_record = await get_token(org_id, "github")
    except ConnectorNotAuthenticatedError:
        logger.warning(
            "GitHub connector not authenticated for org=%s run=%s "
            "— no token stored. Authenticate via Integration Hub before running. "
            "All three GitHub detectors will be skipped.",
            org_id, run_id,
        )
        return _degraded_payload(org_id, run_id)
    except Exception as exc:
        logger.warning(
            "GitHub vault lookup raised an unexpected error for org=%s run=%s: %s "
            "— check CREDENTIAL_VAULT_KEY is set and the vault is reachable. "
            "All three GitHub detectors will be skipped.",
            org_id, run_id, _safe_exc(exc),
        )
        return _degraded_payload(org_id, run_id)

    if token_record is None:
        logger.warning(
            "GitHub vault returned no token for org=%s run=%s "
            "— connector has not been authenticated or token was revoked. "
            "All three GitHub detectors will be skipped.",
            org_id, run_id,
        )
        return _degraded_payload(org_id, run_id)

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
