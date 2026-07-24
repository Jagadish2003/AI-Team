"""
R17-A2 / AT-459 (T2) — Microsoft SharePoint change-based ingestor.

Implements the :class:`~discovery.ingest.base.ChangeBasedIngestor` contract from
R16-A1 for SharePoint document libraries. The single most important rule it
honours: SharePoint is NOT re-read in full on every discovery run. SharePoint's
native change signal is the Microsoft Graph **delta query** for a document
library's drive (``/drives/{drive-id}/root/delta``), which returns only the
driveItems (files and folders) that changed since the last call and hands back an
opaque *delta token* marking the new position. The connector encodes its position
as the opaque checkpoint value (R16-A1 §1) and, on an incremental run, asks each
library only for what changed since its stored token.

Scope (AT-459 / T2 — AC2 + AC3)
-------------------------------
This file is the change-based ingestor only: checkpointed incremental document
ingestion via Graph delta queries plus a resumable, checkpointed first load
(AC2 + AC3). Each record carries the structured document *metadata signal*
(id, name, file/folder kind, size, timestamps, author, web URL, parent path) plus
a fully-populated ``evidence_pointer`` (R16-B1, ``origin='observed'``) so every
SharePoint signal is traceable back to its source item. The remaining pieces of
R17-A2 are deliberately SEPARATE subtasks and are NOT done here:

  * Reach-phase signal *aggregation* (library activity/cadence, cross-references)
    into the corroboration payload — T3.
  * Microsoft Graph OAuth connect wiring (auth-url / callback / vault) — T5
    (AT-462, done). The SharePoint catalog tile already exists; this ingestor
    reads whatever OAuth token that flow lands in the per-run credential context.

``ingestion.artifact_changed`` event emission (T6 / AT-463) is handled by the
shared runner (``change_runner.py``, AT-381) with no connector-specific code:
every record this ingestor yields already carries ``artifact_id`` + ``change_kind``,
so when the connector is driven through :func:`change_runner.ingest_with_checkpoint`
the runner emits one event per changed driveItem in each fully-processed batch
(source system = ``connector_id`` ``'sharepoint'``, plus artifact id + timestamp),
and nothing on an empty delta. Validated end-to-end against the real connector in
``tests/test_sharepoint_artifact_changed_events.py``.

Per the reach/depth boundary (R17-A2 §2), this ingestor reads only document
activity and metadata *signal* — what exists, how active it is, who touches it.
It does NOT read document *body content*; reading page/document bodies into the
retrieval layer is the separate 1.8 deep-content story.

Paired with Teams by Microsoft Graph plumbing (R17-A2 §6): SharePoint and Teams
both authenticate via Microsoft Graph, so this connector deliberately mirrors the
:mod:`discovery.ingest.teams` connector's structure (per-artifact-container opaque
delta-token map, high-water change marker, resumable batched first load) rather
than reinventing the Graph auth layer.

Checkpoint shape (opaque to the runner)
---------------------------------------
A single ``(org_id, 'sharepoint')`` checkpoint row is persisted by the runner, but
an org has many document libraries (drives) across many sites, each with its own
Graph delta position. The connector therefore encodes a per-library delta-token
MAP as the opaque checkpoint value, keyed by ``"{site_id}/{drive_id}"``. Each
token is the high-water change marker (the last-modified/created timestamp) of the
newest driveItem seen in that library — opaque to the runner, owned by this
connector::

    {"v": 1, "drives": {"S-eng/b-docs": "2026-06-11T08:05:00Z",
                        "S-eng/b-specs": "2026-06-10T09:30:00Z"}}

The runner never interprets this — it persists and returns the string verbatim
(R16-A1 AC5). Only this connector, which owns the shape, parses it back. A library
absent from the map is read from the beginning (no delta token → full
enumeration), which is exactly what makes a first load resumable: if the streamed
first load fails partway, the next run finds a checkpoint (incremental mode) whose
map covers the libraries already loaded, resumes the partially-loaded library from
its last delta token, and loads any not-yet-started library in full. No records
are skipped and the load completes across runs.

Permissions / privacy (R17-A2 §3)
---------------------------------
Only sites and document libraries AgentIQ has been explicitly granted are read.
:meth:`_accessible_libraries` filters to granted sites, then to granted
(``is_accessible``), non-hidden libraries. In live mode the Microsoft Graph API
only returns the sites/drives the OAuth token is scoped to, so the filter is the
offline-fixture equivalent of that live least-privilege boundary.

Offline vs live
---------------
Offline (default, ``INGEST_MODE`` != ``live``): reads the deterministic fixture
``fixtures/sharepoint_sample.json`` — parity with the Teams/Slack/Salesforce
connectors. Live: calls the Microsoft Graph API (``/sites``,
``/sites/{id}/drives``, ``/drives/{id}/root/delta``) using the OAuth token from
the per-run credential context. Credentials are resolved exactly like the other
connectors — ``get_live_connector('sharepoint')`` first, then the env fallback for
CLI use.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

from . import get_ingest_org, get_live_connector, is_live, resolve_vault_connector
from .base import ChangeBasedIngestor, ChangeKind, Checkpoint, DeltaBatch
from .sharepoint_signals import build_item_signals

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sharepoint_sample.json"

#: Opaque-checkpoint schema version, so a future shape change can be detected.
_CHECKPOINT_VERSION = 1

#: Default number of driveItems emitted per :class:`DeltaBatch`. Kept modest so a
#: large initial load is streamed as many small, individually-checkpointed batches
#: (AC3 resumability) rather than one monolithic read.
_DEFAULT_BATCH_SIZE = 100

#: Microsoft Graph API base (live mode).
_GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
_REQUEST_TIMEOUT = 30


class SharePointIngestError(Exception):
    """Raised when live SharePoint ingestion fails with a clear, actionable message."""


def _library_key(site_id: str, drive_id: str) -> str:
    """Build the per-library checkpoint-map key ``"{site_id}/{drive_id}"``.

    A drive id is only unique within its site, so the delta-token map is keyed by
    the site/drive pair to keep two sites' libraries from colliding.
    """
    return f"{site_id}/{drive_id}"


def _encode_checkpoint(tokens: Dict[str, str]) -> str:
    """Encode the per-library delta-token map as the opaque checkpoint value.

    ``sort_keys`` keeps the encoding deterministic so two runs over identical state
    produce byte-identical checkpoints (testable, diff-friendly).
    """
    return json.dumps(
        {"v": _CHECKPOINT_VERSION, "drives": tokens},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_checkpoint(value: Optional[str]) -> Dict[str, str]:
    """Decode an opaque checkpoint value back into the per-library delta-token map.

    Tolerant by design: a missing, empty, or unparseable value yields an empty map
    (read every library from the beginning) rather than raising — a degenerate
    checkpoint must degrade to a safe full re-read, never crash the run.
    """
    if not value:
        return {}
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        logger.warning(
            "sharepoint: could not decode checkpoint value; treating as first run "
            "(full re-read). value=%r",
            value,
        )
        return {}
    drives = data.get("drives") if isinstance(data, dict) else None
    if not isinstance(drives, dict):
        return {}
    # Keep only string→string entries; ignore anything malformed.
    return {str(k): str(v) for k, v in drives.items() if v is not None}


def _change_marker(item: Dict[str, Any]) -> str:
    """Return a driveItem's change position: its last-modified (else created) time.

    Microsoft Graph stamps every driveItem with ``createdDateTime`` and, once
    edited, ``lastModifiedDateTime`` — both present on live Graph items AND in the
    offline fixture. This timestamp is the per-item change signal the connector
    advances its opaque delta token to. An edit moves the marker forward (newer
    ``lastModifiedDateTime``), so a re-modified document re-surfaces in the next
    delta.
    """
    return item.get("lastModifiedDateTime") or item.get("createdDateTime") or ""


def _marker_epoch(marker: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 change marker (Graph uses ``...Z``) to epoch seconds.

    Returns None for a missing/unparseable marker so callers can fall back to a
    string compare. The opaque checkpoint stores the ISO string verbatim; only
    comparison goes through epoch, so ordering is correct regardless of offset
    format.
    """
    if not marker:
        return None
    try:
        return datetime.fromisoformat(marker.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _marker_gt(marker: str, token: Optional[str]) -> bool:
    """True when a driveItem's change marker is strictly newer than the delta token.

    The delta token is opaque to the runner but owned by this connector: it is the
    ISO-8601 high-water change marker of the last item seen in a library. ``token``
    falsy (library absent from the checkpoint map) means read from the beginning —
    the first-load / resume-a-new-library case. Comparison is by parsed timestamp,
    falling back to a lexicographic compare if either side is unparseable.
    """
    if not token:
        return True
    me, te = _marker_epoch(marker), _marker_epoch(token)
    if me is None or te is None:
        return str(marker) > str(token)
    return me > te


def _build_evidence_pointer(
    site_id: str, drive_id: str, item_id: str, timestamp: Optional[str]
) -> Dict[str, Any]:
    """Build the R16-B1 EvidencePointer for one SharePoint document signal.

    Every SharePoint signal must be traceable back to its source item, so each
    record carries a fully-populated, OBSERVED provenance pointer:

      * ``source_system`` = ``'sharepoint'``
      * ``source_artifact`` = the item identity ``"{site_id}/{drive_id}:{item_id}"``
        — the unique id of the source driveItem (stable, so ``source_artifact_type``
        is ``'record_id'``).
      * ``source_timestamp`` = the item's own UTC ISO-8601 change timestamp; falls
        back to now only if the timestamp is missing/unparseable, so the mandatory
        spine is always populated and a signal is never dropped for provenance.
      * ``origin`` = ``'observed'`` — read directly from SharePoint via Microsoft
        Graph, never inferred, so no ``extraction_job_id`` is required.
    """
    return EvidencePointer.observed(
        source_system="sharepoint",
        source_artifact=f"{site_id}/{drive_id}:{item_id}",
        source_timestamp=timestamp or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


class SharePointIngestor(ChangeBasedIngestor):
    """Change-based Microsoft SharePoint ingestor (R17-A2 / AT-459).

    Encodes its position as a per-library Graph delta-token map (opaque to the
    runner) and yields only driveItems newer than that token per library. A first
    run (``since is None``) performs a full initial load of accessible document
    libraries, streamed as resumable, individually-checkpointed batches.

    Deletes / tombstones (R16-A1 §5)
    --------------------------------
    ``reports_deletes = False``: Microsoft Graph drive delta DOES surface removed
    items (via a ``deleted`` facet), but consuming and propagating that removal
    stream is out of scope for the reach phase — consistent with the paired Teams
    connector (R17-A2 §6 keeps the pairing coherent), which likewise defers its
    ``@removed`` stream. Items carrying a ``deleted`` facet are therefore skipped
    here rather than emitted as changes. The gap is declared explicitly rather than
    silently pretending deletes are caught; deletion/tombstone handling can be
    layered on later by reading the ``deleted`` facet and yielding a
    :func:`~discovery.ingest.base.tombstone` record.
    """

    connector_id = "sharepoint"
    reports_deletes = False

    def __init__(self, batch_size: int = _DEFAULT_BATCH_SIZE):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.batch_size = batch_size

    # ── ChangeBasedIngestor contract ────────────────────────────────────────
    def ingest_changes(
        self, org_id: str, since: Optional[Checkpoint]
    ) -> Iterator[DeltaBatch]:
        """Yield batches of changed SharePoint driveItems since ``since``.

        First run (``since is None``): full load of every accessible document
        library, streamed as checkpointed batches (resumable — AC3). Incremental
        run: a Graph delta query per library returns only items newer than the
        stored delta token (AC2). An unchanged estate yields a single empty
        :class:`DeltaBatch` whose ``next_checkpoint`` echoes the incoming position
        (AC2).
        """
        tokens: Dict[str, str] = _decode_checkpoint(since.value if since else None)
        # Working copy we advance as batches are emitted; each yielded
        # next_checkpoint encodes the cumulative map so any single batch is a valid
        # resume point on the next run.
        running = dict(tokens)

        libraries = self._accessible_libraries(org_id)
        logger.info(
            "sharepoint: org=%s %s — %d accessible document library(ies)",
            org_id,
            "first run (full load)" if since is None else "incremental run",
            len(libraries),
        )

        # Run each library's delta query first so we know which batch is the final
        # one overall and can flag is_complete=True on exactly that batch (the
        # runner needs one terminal batch to advance).
        pending: List[tuple] = []  # (library, [items]) for libraries with changes
        for library in libraries:
            key = _library_key(library["site_id"], library["id"])
            token = tokens.get(key)
            changed = self._items_delta(org_id, library, token)
            if changed:
                pending.append((library, changed))

        if not pending:
            # Unchanged estate → empty delta that echoes the incoming position (no
            # regression). On a first run with no accessible libraries this records
            # an empty delta-token map.
            yield DeltaBatch(
                records=[],
                next_checkpoint=_encode_checkpoint(running),
                is_complete=True,
            )
            return

        # Total number of batches across all libraries, so the very last one is
        # marked terminal.
        total_batches = sum(
            (len(items) + self.batch_size - 1) // self.batch_size
            for _, items in pending
        )
        emitted = 0
        for library, items in pending:
            key = _library_key(library["site_id"], library["id"])
            for start in range(0, len(items), self.batch_size):
                page = items[start : start + self.batch_size]
                records = [self._to_record(library, it) for it in page]
                # Advance this library's delta token to the newest change marker in
                # the page — the high-water last-modified/created timestamp. This is
                # the opaque position the next run resumes the delta query from.
                running[key] = _change_marker(page[-1])
                emitted += 1
                yield DeltaBatch(
                    records=records,
                    next_checkpoint=_encode_checkpoint(running),
                    is_complete=(emitted == total_batches),
                )

    # ── Library access (R17-A2 §3) ───────────────────────────────────────────
    def _selected_site_ids(self, org_id: str) -> Optional[set]:
        """The org's saved site selection (site ids), or ``None`` when none is
        saved (read every granted site — backward compatible).

        Mirrors slack ``_selected_channel_ids``: a DB failure degrades to ``None``
        (read all granted) so a run is never blocked by the selection store. Both
        the reach path (:meth:`_accessible_libraries`) and the R18-A5 deep-content
        path (``sharepoint_content._accessible_sites``) narrow to this selection.
        """
        try:
            from app.db import org_connector_get

            record = org_connector_get(org_id, "sharepoint") if org_id else None
            if record:
                sites = record.get("sites")
                if isinstance(sites, list):
                    ids = {str(s).strip() for s in sites if str(s).strip()}
                    if ids:
                        return ids
        except Exception:  # pragma: no cover — never block a run on the selection store
            logger.debug("sharepoint: could not read saved site selection; reading all granted")
        return None

    def _accessible_libraries(self, org_id: str) -> List[Dict[str, Any]]:
        """Return only document libraries AgentIQ is granted and that are live.

        Ungranted sites (``is_accessible == False``), ungranted libraries
        (``is_accessible == False``), and hidden/system libraries
        (``is_hidden == True``) are excluded. In live mode Microsoft Graph only
        returns resources the OAuth token is scoped to, so this filter is the
        offline-fixture equivalent of that least-privilege boundary. When the org
        has saved a site selection (Integration Hub), the granted sites are further
        narrowed to that selection.

        Each returned library dict carries its owning ``site_id`` / ``site_name``
        so records and checkpoint keys can be site-scoped.
        """
        selected = self._selected_site_ids(org_id)
        accessible: List[Dict[str, Any]] = []
        for site in self._raw_sites(org_id):
            if not site.get("is_accessible", True):
                continue  # AgentIQ was not granted this site
            site_id = site.get("id", "")
            if selected is not None and str(site_id) not in selected:
                continue  # site not in the org's saved selection
            site_name = site.get("displayName", site.get("name", ""))
            for d in self._raw_drives(org_id, site_id):
                if not d.get("is_accessible", False):
                    continue  # AgentIQ was not granted this library
                if d.get("is_hidden", False):
                    continue  # hidden / system library — excluded
                accessible.append({**d, "site_id": site_id, "site_name": site_name})
        return accessible

    def _items_delta(
        self, org_id: str, library: Dict[str, Any], token: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Return this library's driveItems changed since ``token``, oldest-first.

        This is the Graph delta query: ``token`` falsy (library absent from the
        checkpoint map) means a full enumeration from the beginning — the
        first-load / resume-a-new-library case; otherwise only items whose change
        marker is strictly newer than the stored delta token are returned. Items
        carrying a ``deleted`` facet are dropped (``reports_deletes = False``).
        Sorting oldest-first (by change marker) guarantees the checkpoint advances
        monotonically as batches are emitted.
        """
        items = self._raw_items(org_id, library)
        live = [it for it in items if not it.get("deleted")]
        fresh = [it for it in live if _marker_gt(_change_marker(it), token)]
        fresh.sort(key=lambda it: _marker_epoch(_change_marker(it)) or float("-inf"))
        return fresh

    def _to_record(
        self, library: Dict[str, Any], item: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Shape one SharePoint driveItem into a change-delta record.

        Carries structured document *metadata signal* only (R17-A2 §2 reach phase):
        identity, site, library, name, file/folder kind, size, timestamps, author,
        web URL, and parent path. NO document body content is read — that is the
        separate 1.8 deep-content story. ``artifact_id`` + ``change_kind`` let the
        shared runner emit ``ingestion.artifact_changed`` events, and every record
        carries a fully-populated OBSERVED ``evidence_pointer`` (R16-B1) so no
        SharePoint signal enters the system without a verifiable source reference.
        """
        site_id = library["site_id"]
        drive_id = library["id"]
        item_id = item.get("id", "")
        created = item.get("createdDateTime")
        last_modified = item.get("lastModifiedDateTime")
        # An item whose last edit differs from its creation is an update to an
        # artifact we may already have seen; everything else newly appearing is a
        # creation. (Pure metadata — no content inspection.)
        change_kind = (
            ChangeKind.UPDATED
            if last_modified and created and last_modified != created
            else ChangeKind.CREATED
        )
        if "folder" in item:
            item_type = "folder"
        elif "file" in item:
            item_type = "file"
        else:
            item_type = "item"
        created_by = (item.get("createdBy") or {}).get("user") or {}
        modified_by = (item.get("lastModifiedBy") or {}).get("user") or {}
        parent = item.get("parentReference") or {}
        record = {
            "artifact_id": f"{site_id}/{drive_id}:{item_id}",
            "change_kind": change_kind,
            "source_system": "sharepoint",
            "site_id": site_id,
            "site_name": library.get("site_name", ""),
            "drive_id": drive_id,
            "library_name": library.get("name", ""),
            "item_id": item_id,
            "item_name": item.get("name", ""),
            "item_type": item_type,
            "size": item.get("size"),
            "web_url": item.get("webUrl"),
            "parent_path": parent.get("path"),
            "created_at": created,
            "last_modified_at": last_modified,
            "created_by": created_by.get("id"),
            "created_by_display_name": created_by.get("displayName"),
            "last_modified_by": modified_by.get("id"),
            "last_modified_by_display_name": modified_by.get("displayName"),
            # R16-B1: observed provenance pointer back to this exact driveItem.
            "evidence_pointer": _build_evidence_pointer(
                site_id, drive_id, item_id, last_modified or created
            ),
        }
        # R17-A2 / AT-460 (T3): reach-phase signal (cross-reference markers from
        # the item name + structural metadata) travels with the delta. Library-
        # level activity + active/dormant estates are derived across records by
        # sharepoint_signals.build_sharepoint_signal.
        record["signals"] = build_item_signals(record)
        return record

    # ── Source access: offline fixture vs live Microsoft Graph API ───────────
    def _raw_sites(self, org_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("sites", []))
        return self._client(org_id).list_sites()

    def _raw_drives(self, org_id: str, site_id: str) -> List[Dict[str, Any]]:
        if not is_live():
            return list(self._fixture().get("drives", {}).get(site_id, []))
        return self._client(org_id).list_drives(site_id)

    def _raw_items(
        self, org_id: str, library: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        if not is_live():
            key = _library_key(library["site_id"], library["id"])
            return list(self._fixture().get("items", {}).get(key, []))
        return self._client(org_id).items_delta(library["id"])

    def _fixture(self) -> Dict[str, Any]:
        if not FIXTURE_PATH.exists():
            raise SharePointIngestError(
                f"SharePoint fixture not found: {FIXTURE_PATH}"
            )
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _client(self, org_id: str) -> "SharePointGraphClient":
        """Build (once per org, then reuse) a Microsoft Graph client from the
        per-run OAuth credentials.

        Resolution mirrors the other connectors: the per-run credential context
        (DB-sourced vault token, isolated per org/run) first, then the
        ``SHAREPOINT_GRAPH_TOKEN`` env var as a CLI/standalone fallback. The OAuth
        connect flow that lands the token in the vault is T5.

        The client is memoised per org_id on this (per-run) ingestor instance so a
        single ingest reuses ONE HTTP session — and its pooled TCP/TLS connection
        — across the sites → drives → per-library delta calls, instead of opening
        a fresh session on every read.
        """
        cache = getattr(self, "_client_cache", None)
        if cache is None:
            cache = {}
            self._client_cache = cache
        client = cache.get(org_id)
        if client is not None:
            return client

        cred = get_live_connector("sharepoint") or resolve_vault_connector("sharepoint", org_id)
        token = cred.get("token") if cred else None
        if not token:
            raise SharePointIngestError(
                "Live mode requires a Microsoft Graph OAuth token from the "
                "credential vault. Connect SharePoint in the Integration Hub, or "
                "set INGEST_MODE=offline to run without credentials."
            )
        client = SharePointGraphClient(token.strip())
        cache[org_id] = client
        return client


class SharePointGraphClient:
    """Thin wrapper around the Microsoft Graph API for document-library signal reads.

    Only the three read endpoints the reach phase needs are implemented:
    ``/sites`` (granted sites), ``/sites/{id}/drives`` (document libraries), and
    ``/drives/{id}/root/delta`` (changed driveItems via the native delta query).
    No document-body content endpoint (``/content``) is ever requested — that is
    the 1.8 deep-content story.
    """

    def __init__(self, token: str):
        self.token = token
        self._session = None

    def _sess(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - requests ships in requirements
            raise SharePointIngestError(
                "requests library required for live SharePoint mode: pip install requests"
            )
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({"Authorization": f"Bearer {self.token}"})
        return self._session

    def _get(self, url: str) -> Dict[str, Any]:
        resp = self._sess().get(url, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise SharePointIngestError(
                f"Microsoft Graph GET {url} HTTP {resp.status_code}"
            )
        return resp.json()

    def _get_all(self, url: str) -> List[Dict[str, Any]]:
        """Follow Graph ``@odata.nextLink`` pagination, collecting ``value`` rows.

        A delta query's final page carries ``@odata.deltaLink`` (not
        ``@odata.nextLink``), so pagination terminates naturally once the whole
        current delta is collected.
        """
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = url
        while next_url:
            data = self._get(next_url)
            items.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
        return items

    def list_sites(self) -> List[Dict[str, Any]]:
        """Return the SharePoint sites AgentIQ is granted (``/sites?search=*``).

        Graph only returns sites the OAuth token can see, so a returned site is one
        AgentIQ is granted — mark it accessible for the ingestor's access filter.
        """
        raw = self._get_all(f"{_GRAPH_API_BASE}/sites?search=*")
        return [{**s, "is_accessible": True} for s in raw]

    def list_drives(self, site_id: str) -> List[Dict[str, Any]]:
        """Return a site's document libraries, normalised to the fixture's shape.

        Graph only returns drives the caller can see, so a returned drive is one
        AgentIQ is granted (``is_accessible=True``); document libraries have no
        hidden flag on the drive resource, so ``is_hidden`` defaults False.
        """
        raw = self._get_all(f"{_GRAPH_API_BASE}/sites/{site_id}/drives")
        drives: List[Dict[str, Any]] = []
        for d in raw:
            drives.append(
                {
                    **d,
                    "is_accessible": True,
                    "is_hidden": False,
                }
            )
        return drives

    def items_delta(self, drive_id: str) -> List[Dict[str, Any]]:
        """Return changed driveItems for a library via the Graph delta query.

        Follows ``@odata.nextLink`` pagination to the final ``@odata.deltaLink``,
        collecting all changed items. (The delta token itself is threaded through
        the opaque checkpoint by the ingestor; this client returns the item rows.)
        """
        url = f"{_GRAPH_API_BASE}/drives/{drive_id}/root/delta"
        return self._get_all(url)

    def download_item_content(self, drive_id: str, item_id: str) -> bytes:
        """Download one driveItem's raw bytes (R18-A1 / T5 — the document path).

        The ``/drives/{id}/items/{id}/content`` endpoint the reach-phase connector
        deliberately never called (content was the 1.8 deep-content story). Graph
        answers with a 302 to a short-lived download URL; ``requests`` follows it by
        default. Returns the file bytes for the DocumentIngestor to extract — this
        is the ONLY place SharePoint file BYTES are fetched, and only for a file the
        ingestor has already determined is new/changed.
        """
        url = f"{_GRAPH_API_BASE}/drives/{drive_id}/items/{item_id}/content"
        resp = self._sess().get(url, timeout=_REQUEST_TIMEOUT)
        if not resp.ok:
            raise SharePointIngestError(
                f"Microsoft Graph GET {url} HTTP {resp.status_code}"
            )
        return resp.content

    # ── Page-native content reads (R18-A5 / AT-601 — the deep-content path) ────
    # These three read the PAGE-NATIVE content surface (site pages + list text),
    # which is DISTINCT from the driveItem/binary-file surface above: library files
    # route to the R18-A1 document path, page-native content routes here. Graph only
    # returns resources the OAuth token is granted, so every read is site-scoped.
    def list_site_pages(self, site_id: str) -> List[Dict[str, Any]]:
        """Return a site's modern site pages WITH their canvas content (AT-601).

        ``GET /sites/{id}/pages/microsoft.graph.sitePage?$expand=canvasLayout`` — the
        page id/title/webUrl/timestamps plus the ``canvasLayout`` whose text
        webparts carry the page body (rendered to structure-preserving text by
        :func:`discovery.ingest.sharepoint_content.render_page_text`). Site pages are
        page-native content, never library files — the two never overlap.
        """
        url = (
            f"{_GRAPH_API_BASE}/sites/{site_id}/pages/microsoft.graph.sitePage"
            "?$expand=canvasLayout"
        )
        return self._get_all(url)

    def list_site_lists(self, site_id: str) -> List[Dict[str, Any]]:
        """Return a site's lists (metadata) — ``GET /sites/{id}/lists`` (AT-601)."""
        return self._get_all(f"{_GRAPH_API_BASE}/sites/{site_id}/lists")

    def list_item_fields(self, site_id: str, list_id: str) -> List[Dict[str, Any]]:
        """Return a list's items with their field text (AT-601).

        ``GET /sites/{id}/lists/{id}/items?$expand=fields`` — the ``fields`` map on
        each item holds the page-native list text (Title + text columns) rendered by
        :func:`discovery.ingest.sharepoint_content.render_list_text`.
        """
        url = (
            f"{_GRAPH_API_BASE}/sites/{site_id}/lists/{list_id}/items?$expand=fields"
        )
        return self._get_all(url)


# ─────────────────────────────────────────────────────────────────────────────
# Site selection (Integration Hub picker)
# ─────────────────────────────────────────────────────────────────────────────


def list_selectable_sites(org_id: Optional[str] = None) -> List[Dict[str, str]]:
    """SharePoint sites the customer can choose from — ``[{id, name}]``.

    The option list for ``GET /api/connectors/sharepoint/sites``: every GRANTED
    site (the selection narrowing is deliberately NOT applied here). Offline reads
    the fixture; live lists sites via Microsoft Graph. Resolves identically to what
    a discovery run sees, so a saved id is always among the options.
    """
    ingestor = SharePointIngestor()
    org = org_id or get_ingest_org()
    out: List[Dict[str, str]] = []
    for site in ingestor._raw_sites(org):
        if not site.get("is_accessible", True):
            continue
        site_id = site.get("id")
        if not site_id:
            continue
        name = site.get("displayName") or site.get("name") or str(site_id)
        out.append({"id": str(site_id), "name": str(name)})
    return out
