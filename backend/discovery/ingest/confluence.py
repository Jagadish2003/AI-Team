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
This file is the change-based ingestor: checkpointed incremental content
ingestion via modification timestamps (AC2) plus a resumable, checkpointed first
load (AC3). Each delta record carries the page/blogpost **metadata** (id, space,
type, title, version, last-modifier, modified timestamp, web link) that the
signal pass consumes, plus:

  * Reach-phase signal extraction (space/page activity, cadence, churn,
    contributor patterns, cross-reference markers) — T3 / AT-460, in the
    ``signals`` block via :func:`confluence_signals.build_page_signals`.
  * A fully-populated OBSERVED ``EvidencePointer`` (R16-B1) on every record —
    T4 / AT-461, via :func:`_build_evidence_pointer` (``source_system='confluence'``,
    page/blogpost id, timestamp, ``origin='observed'``) so any finding is traceable
    back to its exact source page (AC5).

``ingestion.artifact_changed`` event emission (T6 / AT-463) is handled by the
shared runner (``change_runner.py``, AT-381) with no connector-specific code:
every record this ingestor yields already carries ``artifact_id`` + ``change_kind``,
so when the connector is driven through :func:`change_runner.ingest_with_checkpoint`
the runner emits one event per changed page/blogpost in each fully-processed batch
(source system = ``connector_id`` ``'confluence'``, plus artifact id + timestamp),
and nothing on an empty delta. Validated end-to-end against the real connector in
``tests/test_confluence_artifact_changed_events.py``.

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

    {"v": 2, "spaces": {"ENG": "2026-06-11T08:05:00.000Z", "OPS": "2026-06-10T09:30:00.000Z"},
     "known_ids": {"ENG": ["100", "200", "300", "400"], "OPS": ["500", "600"]}}

The runner never interprets this — it persists and returns the string verbatim
(R16-A1 AC5). Only this connector, which owns the shape, parses it back. A space
absent from the ``spaces`` map is read from the beginning, which is exactly what
makes a first load resumable: if the streamed first load fails partway, the next
run finds a checkpoint (incremental mode) whose map covers the spaces already
loaded, resumes the partially-loaded space from its last content-modified
timestamp, and loads any not-yet-started space in full. No content is skipped
and the load completes across runs.

``known_ids`` (R18-A5 / AT-603, T4) is the per-space set of page/blogpost ids
this connector has confirmed CURRENT as of its last full-inventory read of that
space — schema version bumped to 2 to mark the shape change (a v1 checkpoint,
lacking ``known_ids``, decodes to an empty map and simply skips deletion
detection until the next run repopulates it — a safe, self-healing bootstrap).
See :meth:`ConfluenceIngestor.ingest_changes` for how it drives deletion/
archival propagation.

Permissions / privacy (AC4)
---------------------------
Only spaces AgentIQ is granted are read, and the source's own space permissions
are respected: :meth:`_accessible_spaces` filters to ``is_accessible == True``
and excludes archived spaces. Content in an ungranted space is never fetched.

Deletion / archival propagation (R18-A5 / AT-603, T4 — AC3 + AC5)
-------------------------------------------------------------------
Confluence's default content listing returns only ``status='current'`` content
(``content_for_space`` never passes a ``status`` filter), so a page that is
trashed or archived simply STOPS appearing in the space's listing — the exact
"disappeared from a full-inventory read" signal :mod:`documents.py` already uses
to infer deletions. Each run, alongside the modified-cursor delta, this
connector re-derives the space's CURRENT page/blogpost id set from the SAME
``_raw_content`` read (no extra fetch) and diffs it against the previous run's
``known_ids`` (see the checkpoint shape above): an id that dropped out of the
current set — because it was deleted/trashed/archived, OR because its own
``status`` field flipped away from ``current`` while still listed — is emitted
as a :func:`~discovery.ingest.base.tombstone` (``change_kind='deleted'``, no
content). A space that disappears from :meth:`_accessible_spaces` entirely
(revoked grant, or newly archived at the space level) tombstones every id
previously known for it WITHOUT reading that now-inaccessible space (AC4 holds
at the deletion path too — never fetch what is no longer granted, not even to
check whether it still exists).

Every tombstone flows through the SAME shared runner
(:mod:`discovery.ingest.change_runner`) as ordinary changes, so it emits a
``change_kind='deleted'`` ``ingestion.artifact_changed`` event exactly like any
other connector's deletion (R16-A1 §5) — the retrieval-freshness subscriber
(R18-B2) turns that into an immediate ``store.purge_artifact`` for
``(source_system='confluence', source_artifact='{space_key}:{content_id}')``,
the SAME identity :mod:`confluence_content` ingests page bodies under, so the
exact chunks a page produced are the ones removed (AC3's "removes its content
from retrieval immediately"). :mod:`confluence_content` additionally routes
these tombstones straight to ``retrieval.ingest.remove_content`` in the SAME
hand-off call, synchronously, so removal does not depend solely on the
fire-and-forget telemetry/freshness path (belt-and-braces, mirroring
``git_content.py``'s AT-533 deletion propagation).

``known_ids`` is a per-run FULL snapshot of a space's current ids, computed
independently of how far this run's upsert batches progress — it answers "does
this id still exist in the source", not "was its content re-ingested this
run" — so it is safe to finalise up front, even on an interrupted first load;
the per-space modified cursor (unchanged, resumable exactly as before) is what
governs re-emission of un-ingested content changes. The two concerns are
orthogonal and do not interfere with each other's correctness.

``reports_deletes = True``: unlike the reach-phase-only v1 connector, deletion
IS now detected (via the disappearance/status-flip diff above) for any page
this connector has previously observed. The one residual gap: a page deleted
before this connector ever saw it (never entered ``known_ids``) leaves no
trace to tombstone — but it was also never ingested, so there is nothing stale
to remove.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/confluence_sample.json`` — parity with the Salesforce/ServiceNow/Jira/
Slack/Teams connectors. Live: calls the Confluence Cloud v2 REST API through the
Atlassian ``api.atlassian.com`` gateway (``/wiki/api/v2`` — the v1 ``/wiki/rest/api``
API is deprecated and returns HTTP 410) using the OAuth token from the per-run
credential context — resolved exactly like the other connectors
(``get_live_connector('confluence')`` first, then env fallback).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

from . import get_ingest_org, get_live_connector, is_live, resolve_vault_connector
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch, tombstone
from .confluence_signals import build_page_signals

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "confluence_sample.json"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
#: Bumped 1 -> 2 (R18-A5 / AT-603, T4) to add the per-space ``known_ids`` set
#: deletion/archival detection diffs against.
_CHECKPOINT_VERSION = 2

#: Default number of content items emitted per :class:`DeltaBatch`. Kept modest
#: so a large initial load is streamed as many small, individually-checkpointed
#: batches (AC3 resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Content types the reach phase ingests (pages + blog posts). Attachments,
#: comments, etc. are out of scope.
_CONTENT_TYPES = ("page", "blogpost")

#: Confluence content ``status`` values that mean "no longer live content" —
#: treated as a deletion for retrieval-freshness purposes (R18-A5 / AT-603, T4).
#: A page usually stops appearing in the listing entirely once trashed/archived
#: (Confluence's default content query returns only ``status='current'``); this
#: set also catches the rarer case where a non-current item is still listed.
_REMOVED_STATUSES = frozenset({"trashed", "archived"})

_REQUEST_TIMEOUT = 30


class ConfluenceIngestError(Exception):
    """Raised when live Confluence ingestion fails with a clear, actionable message."""


def _encode_checkpoint(
    cursors: Dict[str, str], known_ids: Optional[Dict[str, Any]] = None
) -> str:
    """Encode the per-space cursor map + known-id sets as the opaque checkpoint.

    ``known_ids`` (R18-A5 / AT-603) defaults to empty — existing callers that
    pass only ``cursors`` (e.g. hand-built ``since`` checkpoints in tests) still
    work exactly as before, simply with no prior known-id state to diff
    deletions against (a safe no-op, not an error). ``sort_keys`` keeps the
    encoding deterministic so two runs over identical state produce
    byte-identical checkpoints (testable, diff-friendly).
    """
    payload = {
        "v": _CHECKPOINT_VERSION,
        "spaces": cursors,
        "known_ids": {k: sorted(set(v)) for k, v in (known_ids or {}).items()},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_checkpoint(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-space cursor map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty
    map (read every space from the beginning) rather than raising — a degenerate
    checkpoint must degrade to a safe full re-read, never crash the run. Ignores
    a ``known_ids`` key if present (see :func:`_decode_known_ids`) — this reader
    is scoped to the cursor map only.
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


def _decode_known_ids(value: Optional[str]) -> Dict[str, set]:
    """Decode the per-space known-page-id sets (R18-A5 / AT-603, T4).

    Tolerant exactly like :func:`_decode_checkpoint`: a missing/unparseable
    value, or a v1 checkpoint predating this key, yields an empty map — the
    connector simply has no prior inventory to diff against yet, so deletion
    detection quietly sits out this one run and resumes from the next (a safe,
    self-healing bootstrap, never a crash).
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return {}
    known = data.get("known_ids") if isinstance(data, dict) else None
    if not isinstance(known, dict):
        return {}
    result: Dict[str, set] = {}
    for k, v in known.items():
        if isinstance(v, list):
            result[str(k)] = {str(x) for x in v}
    return result


def _is_current(content: Dict[str, Any]) -> bool:
    """True unless a content item's ``status`` marks it removed (AT-603).

    Missing ``status`` defaults to ``current`` (the common case; the offline
    fixture and most live responses always carry it, but a defensive default
    avoids ever mistaking an absent field for a removal).
    """
    status = str(content.get("status") or "current").strip().lower()
    return status not in _REMOVED_STATUSES


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


def _build_evidence_pointer(
    space_key: str, content_id: str, timestamp: Optional[str]
) -> Dict[str, Any]:
    """Build the R16-B1 EvidencePointer for one Confluence content signal (AT-461).

    Every Confluence signal must be traceable back to its exact source page /
    blog post, so each record carries a fully-populated, OBSERVED provenance
    pointer (mirrors the paired SharePoint connector's ``_build_evidence_pointer``):

      * ``source_system`` = ``'confluence'``
      * ``source_artifact`` = the content identity ``"{space_key}:{content_id}"`` —
        the stable id of the source page/blogpost (so ``source_artifact_type`` is
        ``'record_id'``), identical to the record's ``artifact_id``.
      * ``source_timestamp`` = the content's own last-modified (``version.when``) UTC
        ISO-8601 timestamp; falls back to now only if it is missing/unparseable, so
        the mandatory spine is always populated and a signal is never dropped for
        provenance.
      * ``origin`` = ``'observed'`` — read directly from Confluence, never inferred,
        so no ``extraction_job_id`` is required.
    """
    return EvidencePointer.observed(
        source_system="confluence",
        source_artifact=f"{space_key}:{content_id}",
        source_timestamp=timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


class ConfluenceIngestor(ChangeBasedIngestor):
    """Change-based Confluence ingestor (R17-A2 / AT-457).

    Encodes its position as a per-space content-modified cursor map (opaque to
    the runner) and yields only content modified after that cursor per space. A
    first run (``since is None``) performs a full initial load of accessible
    spaces, streamed as resumable, individually-checkpointed batches.

    Deletes / tombstones (R16-A1 §5; R18-A5 / AT-603 T4)
    -----------------------------------------------------
    ``reports_deletes = True``: alongside the last-modified-forward delta, each
    run also diffs a space's current page/blogpost id set against the previous
    run's known ids (the checkpoint's ``known_ids``) and emits a
    :func:`~discovery.ingest.base.tombstone` for any id that disappeared or
    whose status flipped to trashed/archived — see the module docstring's
    "Deletion / archival propagation" section for the full mechanism. The one
    residual gap: a page deleted before this connector ever observed it leaves
    no trace to tombstone (nothing was ingested, so nothing is stale).
    """

    connector_id = "confluence"
    reports_deletes = True

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
        content modified after the stored per-space cursor (AC2) — PLUS any
        deletion/archival tombstones the known-id diff surfaces (AT-603, AC3 /
        AC5; see the module docstring). An unchanged source yields a single
        empty :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming
        position (AC2).
        """
        cursors: Dict[str, str] = _decode_checkpoint(since.value if since else None)
        known_prev: Dict[str, set] = _decode_known_ids(since.value if since else None)
        # Working copies advanced as batches are emitted; each yielded
        # next_checkpoint encodes the cumulative state so any single batch is a
        # valid resume point on the next run.
        running = dict(cursors)
        running_known: Dict[str, set] = {k: set(v) for k, v in known_prev.items()}

        spaces = self._accessible_spaces(org_id)
        accessible_keys = {s["key"] for s in spaces}
        logger.info(
            "confluence: org=%s %s — %d accessible space(s)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(spaces),
        )

        # Gather each space's changed content first so we know which batch is the
        # final one overall and can flag is_complete=True on exactly that batch
        # (the runner needs one terminal batch to advance). One read of a
        # space's raw content serves BOTH the upsert delta and the known-id
        # snapshot used for deletion diffing (AT-603) — no extra fetch.
        pending: List[tuple] = []  # (space, [content]) for spaces with changes
        removed: List[tuple] = []  # (space_key, content_id) tombstones
        for space in spaces:
            key = space["key"]
            cursor = cursors.get(key)
            raw = self._raw_content(org_id, space)
            changed = self._changed_content(org_id, space, cursor, raw_content=raw)
            if changed:
                pending.append((space, changed))

            current_ids = self._current_page_ids(raw)
            removed.extend(
                (key, cid) for cid in sorted(known_prev.get(key, set()) - current_ids)
            )
            running_known[key] = current_ids

        # A space that dropped out of accessibility entirely (grant revoked, or
        # archived at the space level) tombstones everything previously known
        # for it — WITHOUT reading that now-ungranted space (AC4 holds at the
        # deletion path too).
        for key, ids in known_prev.items():
            if key in accessible_keys:
                continue
            removed.extend((key, cid) for cid in sorted(ids))
            running_known.pop(key, None)

        deletions = [self._tombstone(key, cid) for key, cid in removed]

        if not pending and not deletions:
            # Unchanged source → empty delta that echoes the incoming position
            # (no regression). On a first run with no accessible spaces this
            # records an empty cursor map.
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running, running_known),
                is_complete=True,
            )
            return

        # Total number of batches across all spaces (upserts + deletions), so
        # the very last one is marked terminal.
        total_upsert_batches = sum(
            (len(items) + self.batch_size - 1) // self.batch_size
            for _, items in pending
        )
        deletion_pages = [
            deletions[start : start + self.batch_size]
            for start in range(0, len(deletions), self.batch_size)
        ]
        total_batches = total_upsert_batches + len(deletion_pages)
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
                    next_checkpoint=_encode_checkpoint(running, running_known),
                    is_complete=(emitted == total_batches),
                )

        for page in deletion_pages:
            emitted += 1
            yield DeltaBatch(
                records=page,
                next_checkpoint=_encode_checkpoint(running, running_known),
                is_complete=(emitted == total_batches),
            )

    # ── Space access (AC4) ────────────────────────────────────────────────────
    def _selected_space_keys(self, org_id: str) -> Optional[set]:
        """The org's saved space selection (space keys), or ``None`` when none is
        saved (read every granted space — backward compatible).

        Mirrors slack ``_selected_channel_ids`` / jira ``resolve_jira_projects``:
        a DB failure degrades to ``None`` (read all granted) so a run is never
        blocked by the selection store.
        """
        try:
            from app.db import org_connector_get

            record = org_connector_get(org_id, "confluence") if org_id else None
            if record:
                spaces = record.get("spaces")
                if isinstance(spaces, list):
                    keys = {str(s).strip() for s in spaces if str(s).strip()}
                    if keys:
                        return keys
        except Exception:  # pragma: no cover — never block a run on the selection store
            logger.debug("confluence: could not read saved space selection; reading all granted")
        return None

    def _accessible_spaces(self, org_id: str) -> List[Dict[str, Any]]:
        """Return only spaces AgentIQ is granted and that are live.

        Spaces AgentIQ was not granted (``is_accessible == False``) and archived
        spaces are excluded — the source's own space permissions are respected
        (AC4). Content in an ungranted space is never fetched. When the org has
        saved a space selection (Integration Hub), the granted set is further
        narrowed to that selection; no selection means read every granted space.
        """
        selected = self._selected_space_keys(org_id)
        return [
            s
            for s in self._raw_spaces(org_id)
            if s.get("is_accessible", False)
            and str(s.get("status", "current")).lower() != "archived"
            and (selected is None or str(s.get("key")) in selected)
        ]

    def _changed_content(
        self,
        org_id: str,
        space: Dict[str, Any],
        cursor: Optional[str],
        raw_content: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """Return this space's content modified after ``cursor``, oldest-first.

        ``cursor`` falsy (space absent from the checkpoint map) means read from
        the beginning — the first-load / resume-a-new-space case; otherwise only
        content whose modified timestamp is strictly newer than the stored cursor
        is returned. Sorting oldest-first (by modified timestamp) guarantees the
        checkpoint advances monotonically as batches are emitted. Only page /
        blog-post content types with ``status='current'`` are considered (reach
        phase) — a non-current item (trashed/archived) is deletion-path territory
        (AT-603), never an upsert. ``raw_content``, when given, is used instead of
        re-fetching (``ingest_changes`` already reads it once for the known-id
        diff — AT-603).
        """
        content = raw_content if raw_content is not None else self._raw_content(org_id, space)
        fresh = [
            c
            for c in content
            if c.get("type", "page") in _CONTENT_TYPES
            and _is_current(c)
            and _modified_gt(_modified_iso(c), cursor)
        ]
        fresh.sort(key=lambda c: _iso_to_epoch(_modified_iso(c)) or float("-inf"))
        return fresh

    @staticmethod
    def _current_page_ids(raw_content: List[Dict[str, Any]]) -> set:
        """Return the set of page/blogpost ids currently ``status='current'``.

        The known-id snapshot deletion detection (AT-603) diffs the PREVIOUS
        run's set against. Comments and other non-page content types never
        entered ``known_ids`` in the first place, so they are excluded here too.
        """
        return {
            str(c["id"])
            for c in raw_content
            if c.get("type", "page") in _CONTENT_TYPES and _is_current(c) and c.get("id")
        }

    @staticmethod
    def _tombstone(space_key: str, content_id: str) -> Dict[str, Any]:
        """Build a deletion/archival tombstone (R18-A5 / AT-603, AC3).

        Carries no content — the shared runner emits it as a
        ``change_kind='deleted'`` ``ingestion.artifact_changed`` event, which the
        retrieval-freshness subscriber (R18-B2) turns into an immediate
        ``store.purge_artifact`` for this exact ``(source_system='confluence',
        source_artifact='{space_key}:{content_id}')`` identity — the same one
        :mod:`confluence_content` ingests page bodies under.
        """
        return tombstone(
            f"{space_key}:{content_id}",
            source_system="confluence",
            space_key=space_key,
            content_id=content_id,
        )

    def _to_record(self, space: Dict[str, Any], content: Dict[str, Any]) -> Dict[str, Any]:
        """Shape one Confluence content item into a change-delta record.

        Carries content **metadata and activity** signal only (AC7): identity,
        space, type, title, version, last-modifier, and the modified timestamp.
        The page/document *body* is deliberately NOT read here — deep content is
        the separate 1.8 story. ``artifact_id`` + ``change_kind`` let the shared
        runner emit ``ingestion.artifact_changed`` events (AC6), and every record
        carries a fully-populated OBSERVED ``evidence_pointer`` (R16-B1 / AT-461)
        so no Confluence signal enters the system without a verifiable source
        reference (AC5).

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
        modified_at = _modified_iso(content)
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
            "modified_at": modified_at,
            "modified_by": by.get("accountId") or by.get("displayName"),
            "url": links.get("webui"),
            # R17-A2 / AT-461 (T4): observed provenance pointer back to this exact
            # page/blogpost, so any finding is traceable to its source (AC5).
            "evidence_pointer": _build_evidence_pointer(space_key, content_id, modified_at),
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

    def _raw_page_body(self, org_id: str, space_key: str, content_id: str) -> Dict[str, Any]:
        """Return one page/blogpost's rendered body + labels (R18-A5 / T1 — depth).

        The ONE seam that crosses the reach/depth boundary this ingestor otherwise
        enforces (AC7): ``_raw_content``/``content_for_space`` above never expand
        ``body.*``. Only called for a page already known changed (from the same
        delta batch the reach phase produced) — never a full re-scan. A missing
        fixture entry (or a page the depth caller has no business reading) degrades
        to ``{}`` rather than raising, so one bad page never aborts the batch.
        """
        if not is_live():
            bodies = self._fixture().get("bodies", {}).get(space_key, {})
            return dict(bodies.get(content_id) or {})
        return self._client(org_id).page_body(content_id)

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise ConfluenceIngestError(f"Confluence fixture not found: {FIXTURE_PATH}")
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str) -> "ConfluenceClient":
        """Build (once per org, then reuse) a Confluence Cloud client from the
        per-run OAuth credentials.

        Resolution mirrors the other connectors: the per-run credential context
        (DB-sourced vault token + captured api.atlassian.com gateway base) first,
        then the ``CONFLUENCE_URL`` / ``CONFLUENCE_TOKEN`` env vars as a
        CLI/standalone fallback. Like Jira Cloud, live Confluence is reached
        through the ``api.atlassian.com/ex/confluence/{cloudId}`` gateway with the
        OAuth Bearer token.

        The client is memoised per org_id on this (per-run) ingestor instance so a
        single ingest reuses ONE HTTP session across the list-spaces → per-space
        content calls, instead of opening a fresh session on every read.
        """
        cache = getattr(self, "_client_cache", None)
        if cache is None:
            cache = {}
            self._client_cache = cache
        client = cache.get(org_id)
        if client is not None:
            return client

        cred = get_live_connector("confluence") or resolve_vault_connector("confluence", org_id)
        # Token resolves from the per-org vault only (never env — AC8/AC11); the
        # gateway URL is instance config and keeps its CONFLUENCE_URL env fallback.
        base_url = (cred.get("url") if cred else None) or os.getenv("CONFLUENCE_URL")
        token = cred.get("token") if cred else None

        # An on-demand API call (e.g. the space picker) has NO per-run context, so
        # the api.atlassian.com gateway base is not on `cred` (resolve_vault_connector
        # returns the OAuth token only — the gateway is instance config captured
        # separately). Resolve it the SAME way resolve_live_systems does: the
        # persisted per-org instance URL, then derive-and-persist from the token via
        # the accessible-resources endpoint — reusing the single-source helpers so
        # the logic can't drift (mirrors the Jira project picker).
        if not base_url and token:
            org = org_id or get_ingest_org()
            try:
                from app.live_ingest_credentials import (
                    _derive_oauth_instance_url,
                    get_connector_instance_url,
                    store_connector_instance_url,
                )

                base_url = (get_connector_instance_url(org, "confluence") or "").rstrip("/")
                if not base_url:
                    derived = _derive_oauth_instance_url("confluence", token)
                    if derived:
                        base_url = derived.rstrip("/")
                        try:
                            store_connector_instance_url(org, "confluence", base_url)
                        except Exception:  # pragma: no cover — best-effort cache
                            pass
            except Exception:  # pragma: no cover — degrade to the error below
                base_url = base_url or None

        if not base_url or not token:
            raise ConfluenceIngestError(
                "Live mode requires a Confluence OAuth token (from the credential "
                "vault) and gateway URL. Connect Confluence in the Integration Hub, "
                "or set INGEST_MODE=offline to run without credentials."
            )
        client = ConfluenceClient(base_url.rstrip("/"), token.strip())
        cache[org_id] = client
        return client


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
        # v2 lists content by numeric space id, not key; cache key -> id as
        # list_spaces() sees them so content_for_space() need not re-resolve.
        self._space_id_by_key: Dict[str, str] = {}

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

    # ── Confluence Cloud v2 REST API ─────────────────────────────────────────
    # The v1 REST API (``/wiki/rest/api/*``) is deprecated by Atlassian and now
    # returns HTTP 410 Gone, so this client speaks the v2 API (``/wiki/api/v2/*``).
    # Every public method still returns the SAME normalised dict shape the rest
    # of the connector consumes (``id``/``type``/``title``/``status``/
    # ``version.when``/``version.by.accountId``/``_links.webui`` for a content
    # item; ``body.storage.value`` + ``metadata.labels.results[].name`` for a page
    # body; ``version`` + ``_links.download`` for an attachment), so nothing
    # downstream — ``_to_record``, ``confluence_content``, ``documents_attachments``
    # — changes with the API version. v2 differs in three ways this layer hides:
    # it lists content by numeric space id (not key), paginates by an opaque
    # ``_links.next`` cursor (not ``start``/``limit`` offsets), and serves pages
    # and blog posts on separate endpoints.

    def _get_v2(self, path_or_url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET one v2 endpoint (or follow a cursor ``_links.next``) and return JSON.

        ``path_or_url`` is a bare v2 path (``"spaces"``), an absolute wiki path
        (the ``_links.next`` cursor link, ``"/wiki/api/v2/..."``), or a full URL.
        A cursor link already carries its query string, so ``params`` is passed
        only for the first page.
        """
        if path_or_url.startswith("http"):
            url = path_or_url
        elif path_or_url.startswith("/"):
            url = f"{self.base_url}{path_or_url}"
        else:
            url = f"{self.base_url}/wiki/api/v2/{path_or_url}"
        resp = self._sess().get(url, params=params, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise ConfluenceIngestError(
                f"Confluence GET {path_or_url} HTTP {resp.status_code}"
            )
        return resp.json()

    def _paged_v2(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Return all results across v2 cursor pagination (follows ``_links.next``)."""
        out: List[Dict[str, Any]] = []
        data = self._get_v2(path, params)
        while True:
            out.extend(data.get("results", []))
            nxt = ((data.get("_links") or {}).get("next")) or ""
            if not nxt:
                break
            data = self._get_v2(nxt)  # cursor link already carries the params
        return out

    def _space_id(self, space_key: str) -> Optional[str]:
        """Resolve a space key to its numeric v2 id (cache first, else look up)."""
        cached = self._space_id_by_key.get(str(space_key))
        if cached:
            return cached
        data = self._get_v2("spaces", {"keys": space_key, "limit": 1})
        results = data.get("results", [])
        if results and results[0].get("id") is not None:
            sid = str(results[0]["id"])
            self._space_id_by_key[str(space_key)] = sid
            return sid
        return None

    @staticmethod
    def _normalise_content(item: Dict[str, Any], content_type: str) -> Dict[str, Any]:
        """Map a v2 page/blogpost object to the connector's v1-shaped content dict."""
        version = item.get("version") or {}
        return {
            "id": str(item.get("id", "")),
            "type": content_type,
            "title": item.get("title", ""),
            "status": item.get("status", "current"),
            "version": {
                "number": version.get("number", 1),
                # v2 stamps the last-modified time on version.createdAt.
                "when": version.get("createdAt") or version.get("when") or "",
                "by": {"accountId": version.get("authorId")},
            },
            "_links": {"webui": (item.get("_links") or {}).get("webui")},
        }

    def list_spaces(self) -> List[Dict[str, Any]]:
        """Return spaces the token can see, normalised to the fixture field shape.

        Confluence returns spaces the caller is permitted to see, so a returned
        space is one AgentIQ is granted (``is_accessible`` True). Archived spaces
        are surfaced with their status so the ingestor's filter can exclude them.
        The numeric v2 space id is kept (and cached key->id) because v2 lists
        content by space id, not key.
        """
        spaces: List[Dict[str, Any]] = []
        for s in self._paged_v2("spaces", {"status": "current", "limit": 100}):
            key = s.get("key")
            space_id = s.get("id")
            if key and space_id is not None:
                self._space_id_by_key[str(key)] = str(space_id)
            spaces.append(
                {
                    "id": space_id,
                    "key": key,
                    "name": s.get("name", ""),
                    "status": s.get("status", "current"),
                    "is_accessible": True,
                }
            )
        return spaces

    def content_for_space(self, space_key: str) -> List[Dict[str, Any]]:
        """Return a space's page + blog-post content with version metadata only.

        v2 lists pages and blog posts on SEPARATE endpoints
        (``/spaces/{id}/pages`` and ``/spaces/{id}/blogposts``); both are queried
        and normalised to the single content shape ``_to_record`` consumes. No
        body is fetched (AC7) — v2 omits the body unless ``body-format`` is asked
        for, which this call never does.
        """
        space_id = self._space_id(space_key)
        if not space_id:
            return []
        items: List[Dict[str, Any]] = []
        for page in self._paged_v2(
            f"spaces/{space_id}/pages", {"status": "current", "limit": 100}
        ):
            items.append(self._normalise_content(page, "page"))
        for blog in self._paged_v2(
            f"spaces/{space_id}/blogposts", {"status": "current", "limit": 100}
        ):
            items.append(self._normalise_content(blog, "blogpost"))
        return items

    def page_body(self, content_id: str) -> Dict[str, Any]:
        """Fetch one page/blogpost's storage body + labels (R18-A5 / T1).

        The ONLY place this client reads a body. The reach-phase
        ``content_for_space`` above deliberately never does (AC7 of R17-A2).
        Called only for content already known changed via the delta feed, never a
        full re-scan. v2 splits body and labels across endpoints and by content
        type; this tries the page endpoints first and falls back to blogpost, then
        normalises to the v1 ``body.storage.value`` +
        ``metadata.labels.results[].name`` shape the depth renderer consumes.
        """
        body_value = ""
        labels: List[Dict[str, Any]] = []
        for kind in ("pages", "blogposts"):
            try:
                doc = self._get_v2(f"{kind}/{content_id}", {"body-format": "storage"})
            except ConfluenceIngestError:
                continue  # wrong content type for this endpoint — try the next
            body_value = (
                ((doc.get("body") or {}).get("storage") or {}).get("value")
            ) or ""
            try:
                lab = self._get_v2(f"{kind}/{content_id}/labels", {"limit": 100})
                labels = [
                    {"name": l.get("name")}
                    for l in lab.get("results", [])
                    if l.get("name")
                ]
            except ConfluenceIngestError:
                labels = []
            break
        return {
            "body": {"storage": {"value": body_value}},
            "metadata": {"labels": {"results": labels}},
        }

    def list_attachments(self, page_id: str) -> List[Dict[str, Any]]:
        """Return a page/blogpost's attachments with version + download link (v2).

        Attachments (files) are the document-path surface. v2
        ``/pages/{id}/attachments`` (falling back to ``/blogposts/{id}/...``),
        normalised to ``version`` (the change marker the DocumentIngestor uses so
        an unchanged attachment is not re-downloaded — AC2) + ``_links.download``
        + ``title``/``mediaType``. No attachment BYTES are fetched here (that is
        :meth:`download_attachment`).
        """
        raw: List[Dict[str, Any]] = []
        for kind in ("pages", "blogposts"):
            try:
                raw = self._paged_v2(f"{kind}/{page_id}/attachments", {"limit": 100})
                break
            except ConfluenceIngestError:
                continue  # wrong content type for this endpoint — try the next
        attachments: List[Dict[str, Any]] = []
        for a in raw:
            version = a.get("version") or {}
            download = a.get("downloadLink") or (a.get("_links") or {}).get("download")
            attachments.append(
                {
                    "id": str(a.get("id", "")),
                    "type": "attachment",
                    "title": a.get("title", ""),
                    "mediaType": a.get("mediaType"),
                    "fileSize": a.get("fileSize"),
                    "version": {
                        "number": version.get("number", 1),
                        "when": version.get("createdAt") or version.get("when"),
                    },
                    "_links": {
                        "download": download,
                        "webui": (a.get("_links") or {}).get("webui"),
                    },
                }
            )
        return attachments

    def download_attachment(self, download_path: str) -> bytes:
        """Download one attachment's raw bytes (R18-A1 / T5 — the document path).

        Confluence gives each attachment a relative ``_links.download`` path under
        the wiki root (e.g. ``/download/attachments/{page}/{name}?version=…``); this
        fetches it with the authenticated session and returns the file bytes for the
        DocumentIngestor to extract. Only called for an attachment already
        determined new/changed. This is the ONLY place attachment BYTES are read.
        """
        if not download_path:
            raise ConfluenceIngestError("attachment has no download link")
        url = f"{self.base_url}/wiki{download_path}"
        resp = self._sess().get(url, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise ConfluenceIngestError(
                f"Confluence GET attachment HTTP {resp.status_code}"
            )
        return resp.content


# ─────────────────────────────────────────────────────────────────────────────
# Space selection (Integration Hub picker)
# ─────────────────────────────────────────────────────────────────────────────


def list_selectable_spaces(org_id: Optional[str] = None) -> List[Dict[str, str]]:
    """Confluence spaces the customer can choose from — ``[{key, name}]``.

    The option list for ``GET /api/connectors/confluence/spaces``: every GRANTED,
    non-archived space (the selection narrowing is deliberately NOT applied here —
    the customer picks from all granted spaces). Offline reads the fixture; live
    lists spaces via the v2 REST client. Resolves identically to what a discovery
    run sees, so a saved key is always among the options.
    """
    ingestor = ConfluenceIngestor()
    org = org_id or get_ingest_org()
    out: List[Dict[str, str]] = []
    for s in ingestor._raw_spaces(org):
        if not s.get("is_accessible", False):
            continue
        if str(s.get("status", "current")).lower() == "archived":
            continue
        key = s.get("key")
        if key:
            out.append({"key": str(key), "name": str(s.get("name", "") or key)})
    return out
