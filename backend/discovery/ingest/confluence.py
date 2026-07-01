"""
R17-A2 / AT-457 (T1) — Confluence change-based ingestor.

Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract from
R16-A1 for Atlassian Confluence. The single most important rule it honours:
Confluence is NOT re-read in full on every discovery run. Confluence's native
change signal is content **modification timestamps** — every page / blog post
carries a ``version.when`` (last-modified) timestamp and Confluence's content
search can return only what changed since a given time. The connector encodes
its last-seen content-modified position as the opaque checkpoint value and asks
each accessible space only for content modified after that position.

Scope (this subtask — AT-457 / T1, AC2 + AC3)
---------------------------------------------
This file is *only* the change-based ingestor: checkpointed incremental content
ingestion via modification timestamps (AC2) plus a resumable, checkpointed first
load (AC3). Each delta record carries the page/blogpost **metadata** (id, space,
type, title, version, last-modifier, modified timestamp, web link) that a later
signal pass consumes. The downstream pieces are deliberately SEPARATE stories
and are NOT done here:

  * Reach-phase signal extraction (space/page activity, cadence, churn,
    contributor patterns, cross-reference markers) — T3 / AT-460. This ingestor
    only carries the raw metadata fields through.
  * EvidencePointer on every signal (R16-B1, ``origin='observed'``) — T4.
  * ``ingestion.artifact_changed`` event emission — handled by the shared runner
    (``change_runner.py``, AT-381); every record this ingestor yields already
    carries ``artifact_id`` + ``change_kind`` so the runner can emit them.

Reach vs depth (AC7): this ingestor reads only content **metadata and activity**
signal. It deliberately does NOT read the page/document *body* — that is the
separate 1.8 deep-content story. The page title is metadata (like a Slack channel
name), but ``body.storage`` / rendered content is never fetched here.

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, 'confluence')`` checkpoint row is persisted by the runner,
but a Confluence instance has many spaces each advancing independently. The
connector therefore encodes a per-space cursor MAP as the opaque checkpoint
value, keyed by space key; each cursor is the high-water content-modified
timestamp (ISO-8601) seen in that space::

    {"v": 1, "spaces": {"ENG": "2026-06-11T08:05:00.000Z", "OPS": "2026-06-10T09:30:00.000Z"}}

The runner never interprets this — it persists and returns the string verbatim
(R16-A1 AC5). Only this connector, which owns the shape, parses it back. A space
absent from the map is read from the beginning, which is exactly what makes a
first load resumable: if the streamed first load fails partway, the next run
finds a checkpoint (incremental mode) whose map covers the spaces already loaded,
resumes the partially-loaded space from its last content-modified timestamp, and
loads any not-yet-started space in full. No content is skipped and the load
completes across runs.

Permissions / privacy (AC4)
---------------------------
Only spaces AgentIQ is granted are read, and the source's own space permissions
are respected: :meth:`_accessible_spaces` filters to ``is_accessible == True``
and excludes archived spaces. Content in an ungranted space is never fetched.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/confluence_sample.json`` — parity with the Salesforce/ServiceNow/Jira/
Slack/Teams connectors. Live: calls the Confluence Cloud REST API through the
Atlassian ``api.atlassian.com`` gateway (``/wiki/rest/api``) using the OAuth
token from the per-run credential context — resolved exactly like the other
connectors (``get_live_connector('confluence')`` first, then env fallback).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from . import get_live_connector, is_live
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .confluence_signals import build_page_signals

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "confluence_sample.json"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of content items emitted per :class:`DeltaBatch`. Kept modest
#: so a large initial load is streamed as many small, individually-checkpointed
#: batches (AC3 resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Content types the reach phase ingests (pages + blog posts). Attachments,
#: comments, etc. are out of scope.
_CONTENT_TYPES = ("page", "blogpost")

_REQUEST_TIMEOUT = 30


class ConfluenceIngestError(Exception):
    """Raised when live Confluence ingestion fails with a clear, actionable message."""


def _encode_checkpoint(cursors: Dict[str, str]) -> str:
    """Encode the per-space content-modified cursor map as the opaque checkpoint.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical
    state produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "spaces": cursors},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-space cursor map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty
    map (read every space from the beginning) rather than raising — a degenerate
    checkpoint must degrade to a safe full re-read, never crash the run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "confluence: could not decode checkpoint value; treating as first run "
            "(full re-read). value=%r",
            value,
        )
        return {}
    spaces = data.get("spaces") if isinstance(data, dict) else None
    if not isinstance(spaces, dict):
        return {}
    # Keep only string→string entries; ignore anything malformed.
    return {str(k): str(v) for k, v in spaces.items() if v is not None}


def _modified_iso(content: Dict[str, Any]) -> str:
    """Return a content item's last-modified timestamp (ISO-8601).

    Confluence stamps this on ``version.when``; a top-level ``lastModified`` is
    accepted as a fallback. This is the per-item change signal the connector
    advances its opaque checkpoint to.
    """
    version = content.get("version") or {}
    return version.get("when") or content.get("lastModified") or ""


def _version_number(content: Dict[str, Any]) -> int:
    version = content.get("version") or {}
    try:
        return int(version.get("number", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _iso_to_epoch(value: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp (Confluence uses ``...Z``) to epoch seconds.

    Returns None for a missing/unparseable value so callers can fall back to a
    string compare. The opaque checkpoint stores the ISO string verbatim; only
    comparison goes through epoch, so ordering is correct regardless of format.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _modified_gt(when: str, cursor: Optional[str]) -> bool:
    """True when a content item's modified time is strictly newer than ``cursor``.

    ``cursor`` falsy (space absent from the checkpoint map) means read from the
    beginning — the first-load / resume-a-new-space case. Comparison is by parsed
    timestamp, falling back to a lexicographic compare if either side is
    unparseable.
    """
    if not cursor:
        return True
    w, c = _iso_to_epoch(when), _iso_to_epoch(cursor)
    if w is None or c is None:
        return str(when) > str(cursor)
    return w > c


class ConfluenceIngestor(ChangeBasedIngestor):
    """Change-based Confluence ingestor (R17-A2 / AT-457).

    Encodes its position as a per-space content-modified cursor map (opaque to
    the runner) and yields only content modified after that cursor per space. A
    first run (``since is None``) performs a full initial load of accessible
    spaces, streamed as resumable, individually-checkpointed batches.

    Deletes / tombstones (R16-A1 §5)
    --------------------------------
    ``reports_deletes = False``: this connector polls content by
    last-modified-forward ordering, which does not surface deletions of
    previously-seen pages — a deleted/trashed page simply stops appearing in
    future results. Confluence does expose content-restriction / trash events,
    but consuming that stream is out of scope for the reach phase. The gap is
    declared explicitly here rather than silently pretending deletes are caught.
    """

    connector_id = "confluence"
    reports_deletes = False

    def __init__(self, batch_size: int = _DEFAULT_BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed Confluence content since ``since``.

        First run (``since is None``): full load of every accessible space,
        streamed as checkpointed batches (resumable — AC3). Incremental run: only
        content modified after the stored per-space cursor (AC2). An unchanged
        source yields a single empty :class:`DeltaBatch` whose ``next_checkpoint``
        echoes the incoming position (AC2).
        """
        cursors: Dict[str, str] = _decode_checkpoint(since.value if since else None)
        # Working copy we advance as batches are emitted; each yielded
        # next_checkpoint encodes the cumulative map so any single batch is a
        # valid resume point on the next run.
        running = dict(cursors)

        spaces = self._accessible_spaces(org_id)
        logger.info(
            "confluence: org=%s %s — %d accessible space(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(spaces),
        )

        # Gather each space's changed content first so we know which batch is the
        # final one overall and can flag is_complete=True on exactly that batch
        # (the runner needs one terminal batch to advance).
        pending: List[tuple] = []  # (space, [content]) for spaces with changes
        for space in spaces:
            cursor = cursors.get(space["key"])
            changed = self._changed_content(org_id, space, cursor)
            if changed:
                pending.append((space, changed))

        if not pending:
            # Unchanged source → empty delta that echoes the incoming position
            # (no regression). On a first run with no accessible spaces this
            # records an empty cursor map.
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running),
                is_complete=True,
            )
            return

        # Total number of batches across all spaces, so the very last one is
        # marked terminal.
        total_batches = sum(
            (len(items) + self.batch_size - 1) // self.batch_size
            for _, items in pending
        )
        emitted = 0
        for space, items in pending:
            key = space["key"]
            for start in range(0, len(items), self.batch_size):
                page = items[start : start + self.batch_size]
                records = [self._to_record(space, c) for c in page]
                # Advance this space's cursor to the newest modified timestamp in
                # the page — the high-water content-modified position the next run
                # resumes from.
                running[key] = _modified_iso(page[-1])
                emitted += 1
                yield DeltaBatch(
                    records=records,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )

    # ── Space access (AC4) ────────────────────────────────────────────────────
    def _accessible_spaces(self, org_id: str) -> List[Dict[str, Any]]:
        """Return only spaces AgentIQ is granted and that are live.

        Spaces AgentIQ was not granted (``is_accessible == False``) and archived
        spaces are excluded — the source's own space permissions are respected
        (AC4). Content in an ungranted space is never fetched.
        """
        return [
            s
            for s in self._raw_spaces(org_id)
            if s.get("is_accessible", False)
            and str(s.get("status", "current")).lower() != "archived"
        ]

    def _changed_content(
        self, org_id: str, space: Dict[str, Any], cursor: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Return this space's content modified after ``cursor``, oldest-first.

        ``cursor`` falsy (space absent from the checkpoint map) means read from
        the beginning — the first-load / resume-a-new-space case; otherwise only
        content whose modified timestamp is strictly newer than the stored cursor
        is returned. Sorting oldest-first (by modified timestamp) guarantees the
        checkpoint advances monotonically as batches are emitted. Only page /
        blog-post content types are considered (reach phase).
        """
        content = self._raw_content(org_id, space)
        fresh = [
            c
            for c in content
            if c.get("type", "page") in _CONTENT_TYPES
            and _modified_gt(_modified_iso(c), cursor)
        ]
        fresh.sort(key=lambda c: _iso_to_epoch(_modified_iso(c)) or float("-inf"))
        return fresh

    def _to_record(self, space: Dict[str, Any], content: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one Confluence content item into a change-delta record.

        Carries content **metadata and activity** signal only (AC7): identity,
        space, type, title, version, last-modifier, and the modified timestamp.
        The page/document *body* is deliberately NOT read here — deep content is
        the separate 1.8 story. ``artifact_id`` + ``change_kind`` let the shared
        runner emit ``ingestion.artifact_changed`` events (AC6).

        R17-A2 / AT-460 (T3): each record also carries a ``signals`` block — the
        per-page cross-reference markers (from the title, metadata) and edit
        activity — so the reach-phase signal travels with the delta to downstream
        corroboration. Space-level activity (inventory/cadence/churn) and the
        stale-but-load-bearing set are derived across records by
        :func:`confluence_signals.build_confluence_signal`.

        A version number > 1 is an update to a page we may already have seen;
        version 1 is a creation. (Pure metadata — no body inspection.)
        """
        space_key = space["key"]
        content_id = str(content.get("id", ""))
        number = _version_number(content)
        change_kind = ChangeKind.UPDATED if number > 1 else ChangeKind.CREATED
        version = content.get("version") or {}
        by = version.get("by") or {}
        links = content.get("_links") or {}
        record = {
            "artifact_id": f"{space_key}:{content_id}",
            "change_kind": change_kind,
            "source_system": "confluence",
            "space_key": space_key,
            "space_name": space.get("name", ""),
            "content_id": content_id,
            "content_type": content.get("type", ""),
            "title": content.get("title", ""),
            "status": content.get("status"),
            "version_number": number,
            "modified_at": _modified_iso(content),
            "modified_by": by.get("accountId") or by.get("displayName"),
            "url": links.get("webui"),
        }
        # R17-A2 / AT-460 (T3): reach-phase signal travels with the delta.
        record["signals"] = build_page_signals(record)
        return record

    # ── Source access: offline fixture vs live Confluence Cloud REST API ─────
    def _raw_spaces(self, org_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("spaces", []))
        return self._client(org_id).list_spaces()

    def _raw_content(self, org_id: str, space: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("content", {}).get(space["key"], []))
        return self._client(org_id).content_for_space(space["key"])

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise ConfluenceIngestError(f"Confluence fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str) -> "ConfluenceClient":
        """Build a Confluence Cloud client from the per-run OAuth credentials.

        Resolution mirrors the other connectors: the per-run credential context
        (DB-sourced vault token + captured api.atlassian.com gateway base) first,
        then the ``CONFLUENCE_URL`` / ``CONFLUENCE_TOKEN`` env vars as a
        CLI/standalone fallback. Like Jira Cloud, live Confluence is reached
        through the ``api.atlassian.com/ex/confluence/{cloudId}`` gateway with the
        OAuth Bearer token.
        """
        cred = get_live_connector("confluence")
        base_url = cred.get("url") if cred else os.getenv("CONFLUENCE_URL")
        token = cred.get("token") if cred else os.getenv("CONFLUENCE_TOKEN")
        if not base_url or not token:
            raise ConfluenceIngestError(
                "Live mode requires a Confluence OAuth token and gateway URL, "
                "provided by the Confluence Connect flow (credential vault). Set "
                "INGEST_MODE=offline to run without credentials."
            )
        return ConfluenceClient(base_url.rstrip("/"), token.strip())


class ConfluenceClient:
    """Thin wrapper around the Confluence Cloud REST API for metadata reads.

    Only the reach-phase reads are implemented: list accessible spaces and list a
    space's page/blog-post content with version metadata. Content bodies are
    never requested (``expand`` asks for ``version,space`` only — no ``body.*``),
    keeping the reach/depth boundary (AC7) enforced at the API-call level.
    """

    def __init__(self, base_url: str, token: str):
        self.base_url = base_url  # api.atlassian.com/ex/confluence/{cloudId}
        self.token = token
        self._session = None

    def _sess(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships in requirements
            raise ConfluenceIngestError(
                "requests library required for live Confluence mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(
                {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            )
        return self._session

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._sess().get(
            f"{self.base_url}/wiki/rest/api/{path}",
            params=params,
            timeout=_REQUEST_TIMEOUT,
        )
        if not resp.ok:
            raise ConfluenceIngestError(
                f"Confluence GET {path} HTTP {resp.status_code}"
            )
        return resp.json()

    def list_spaces(self) -> List[Dict[str, Any]]:
        """Return spaces the token can see, normalised to the fixture field shape.

        Confluence returns spaces the caller is permitted to see, so a returned
        space is one AgentIQ is granted (``is_accessible`` True). Archived spaces
        are surfaced with their status so the ingestor's filter can exclude them.
        """
        spaces: List[Dict[str, Any]] = []
        start = 0
        while True:
            data = self._get("space", {"limit": 100, "start": start, "status": "current"})
            results = data.get("results", [])
            for s in results:
                spaces.append(
                    {
                        "key": s.get("key"),
                        "name": s.get("name", ""),
                        "status": s.get("status", "current"),
                        "is_accessible": True,
                    }
                )
            if len(results) < 100:
                break
            start += 100
        return spaces

    def content_for_space(self, space_key: str) -> List[Dict[str, Any]]:
        """Return a space's page/blog-post content with version metadata only.

        ``expand=version,space`` fetches the last-modified metadata WITHOUT the
        body (no ``body.storage`` expand), so no document content is read (AC7).
        """
        items: List[Dict[str, Any]] = []
        start = 0
        while True:
            data = self._get(
                "content",
                {
                    "spaceKey": space_key,
                    "type": "page",
                    "expand": "version,space",
                    "limit": 100,
                    "start": start,
                },
            )
            results = data.get("results", [])
            items.extend(results)
            if len(results) < 100:
                break
            start += 100
        return items
