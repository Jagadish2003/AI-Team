"""
R18-A1 / T1 — Where the document ingestor's files come from.

The :class:`~discovery.ingest.documents.DocumentIngestor` reads only what CHANGED
since its checkpoint. To decide "changed" without reading every file's bytes, a
source must first tell the ingestor what documents EXIST and give each a cheap
change ``signature``; the ingestor then reads bytes ONLY for the new/changed ones
(AC2). This module owns that source contract and its offline/live implementations.

The contract (:class:`DocumentSource`)
--------------------------------------
  * :meth:`DocumentSource.list_documents` returns the FULL current inventory of
    in-scope documents as lightweight :class:`DocumentRef`\\ s (id + change
    signature + provenance — NO bytes). "Full inventory" is load-bearing: the
    ingestor derives deletions by diffing this against its checkpoint, so a source
    that can only return a partial/changed set MUST declare
    ``reports_deletes = False`` (else unchanged files look deleted).
  * :meth:`DocumentSource.read` returns one document's raw bytes, and is called
    ONLY for a new/changed ref — this is what keeps a run from re-reading files
    that have not changed.

Phase-one sources
-----------------
  * :class:`FixtureDocumentSource` — deterministic offline fixture
    (``fixtures/documents_sample.json``), parity with every other connector so the
    whole pipeline runs with no credentials.
  * :class:`ConfiguredLocationSource` — a per-deployment configured-location scan
    (``DOCUMENT_LOCATIONS``): the direct on-disk / mounted document locations an
    org points AgentIQ at. Attachments surfaced by the SharePoint/Confluence 1.7
    connectors are a SEPARATE source, wired in T5 — not here.

Change signature
----------------
The ``signature`` is an opaque per-file change token owned by the source: the
fixture supplies it explicitly (falling back to a content hash), and the
configured-location scan uses ``"{mtime_ns}:{size}"``. The ingestor never
interprets it beyond equality — an unchanged signature means "not re-read".
"""
from __future__ import annotations

import abc
import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import is_live

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "documents_sample.json"

#: Env var (live mode) holding a JSON array of configured document locations:
#: ``[{"location": "handbook", "path": "/mnt/docs/handbook"}, ...]``.
_LOCATIONS_ENV = "DOCUMENT_LOCATIONS"


class DocumentSourceError(Exception):
    """Raised when a document source cannot be read (config/IO problem)."""


@dataclass(frozen=True)
class DocumentRef:
    """A lightweight handle to one in-scope document — metadata, never bytes.

    ``artifact_id``      stable identity of the file, used as the checkpoint key
                         and the ``source_artifact`` of the evidence pointer
                         (``"{location}/{path}"``). Stable ⇒ ``record_id``.
    ``filename``         the file's own name, used for format detection.
    ``location``         the logical location/library it lives in (for grouping
                         and provenance).
    ``signature``        opaque change token (content hash / etag / mtime:size);
                         equality is all the ingestor needs (changed ⇒ re-read).
    ``source_timestamp`` the file's own last-changed time (UTC ISO-8601), used to
                         stream oldest-first and to stamp the evidence pointer.
    ``content_type``     optional MIME hint from the source, assisting format
                         detection when the extension is ambiguous/absent.
    ``size_bytes``       the file's size in bytes when the source knows it up front
                         (a scan stat, a SharePoint/Confluence size field). Lets the
                         ingestor apply the per-file size cap WITHOUT downloading an
                         enormous file (R18-A1 T4 / AC4). ``None`` when unknown — the
                         cap is then applied after the read.
    ``provenance``       optional extra non-secret provenance (author, url, …)
                         carried onto every record.
    """

    artifact_id: str
    filename: str
    location: str
    signature: str
    source_timestamp: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.artifact_id or not isinstance(self.artifact_id, str):
            raise DocumentSourceError("DocumentRef.artifact_id must be a non-empty string")
        if not self.signature or not isinstance(self.signature, str):
            raise DocumentSourceError(
                f"DocumentRef '{self.artifact_id}' must carry a non-empty change signature"
            )


class DocumentSource(abc.ABC):
    """A source of in-scope documents for one org (see module docstring).

    ``reports_deletes`` declares whether :meth:`list_documents` returns the FULL
    current inventory (so the ingestor may treat a checkpoint entry absent from the
    listing as a deletion). A scan-style source leaves it True; a change-event
    source that returns only changed refs MUST set it False so unchanged files are
    never mistaken for deletions.
    """

    reports_deletes: bool = True

    @abc.abstractmethod
    def list_documents(self, org_id: str) -> List[DocumentRef]:
        """Return the current in-scope documents for ``org_id`` (metadata only)."""
        raise NotImplementedError

    @abc.abstractmethod
    def read(self, org_id: str, ref: DocumentRef) -> bytes:
        """Return one document's raw bytes. Called only for a new/changed ref."""
        raise NotImplementedError


def _content_bytes(entry: Dict[str, Any]) -> bytes:
    """Materialise a fixture entry's bytes from inline ``text`` or ``content_b64``."""
    if entry.get("content_b64") is not None:
        return base64.b64decode(entry["content_b64"])
    text = entry.get("text")
    if text is None:
        return b""
    return str(text).encode("utf-8")


class FixtureDocumentSource(DocumentSource):
    """Deterministic offline document source (``documents_sample.json``).

    The fixture is a JSON object ``{"documents": [ {..} ]}``; each entry declares
    ``artifact_id``, ``filename``, ``location``, an optional explicit ``signature``
    (else a content hash is used), ``source_timestamp``, ``content_type``, and its
    bytes as inline ``text`` or base64 ``content_b64``. An entry may set
    ``"raise_on_read": true`` to simulate an unreadable/corrupt file so per-file
    failure isolation (AC5) can be exercised offline.
    """

    def __init__(self, fixture_path: Path = FIXTURE_PATH):
        self._fixture_path = fixture_path

    def _load(self) -> List[Dict[str, Any]]:
        if not self._fixture_path.exists():
            return []
        with open(self._fixture_path, encoding="utf-8") as fh:
            data = json.load(fh)
        docs = data.get("documents", []) if isinstance(data, dict) else []
        return [d for d in docs if isinstance(d, dict)]

    def _entry(self, org_id: str, artifact_id: str) -> Optional[Dict[str, Any]]:
        for entry in self._load():
            if str(entry.get("artifact_id", "")) == artifact_id:
                return entry
        return None

    def list_documents(self, org_id: str) -> List[DocumentRef]:
        refs: List[DocumentRef] = []
        for entry in self._load():
            artifact_id = str(entry.get("artifact_id", "")).strip()
            if not artifact_id:
                continue
            raw_bytes = _content_bytes(entry)
            signature = str(entry.get("signature") or "").strip()
            if not signature:
                signature = hashlib.sha256(raw_bytes).hexdigest()
            # Explicit size_bytes wins (lets a fixture declare an oversized file
            # without carrying huge bytes); otherwise use the inline content length.
            size_bytes = entry.get("size_bytes")
            if size_bytes is None:
                size_bytes = len(raw_bytes)
            refs.append(
                DocumentRef(
                    artifact_id=artifact_id,
                    filename=str(entry.get("filename", artifact_id)),
                    location=str(entry.get("location", "")),
                    signature=signature,
                    source_timestamp=entry.get("source_timestamp"),
                    content_type=entry.get("content_type"),
                    size_bytes=int(size_bytes),
                    provenance=dict(entry.get("provenance") or {}),
                )
            )
        return refs

    def read(self, org_id: str, ref: DocumentRef) -> bytes:
        entry = self._entry(org_id, ref.artifact_id)
        if entry is None:
            raise DocumentSourceError(f"document {ref.artifact_id!r} not found in fixture")
        if entry.get("raise_on_read"):
            # Simulated unreadable/corrupt file — exercises per-file isolation (AC5).
            raise DocumentSourceError(f"simulated read failure for {ref.artifact_id!r}")
        return _content_bytes(entry)


class ConfiguredLocationSource(DocumentSource):
    """Per-deployment configured-location scan of direct document locations.

    Reads ``DOCUMENT_LOCATIONS`` (a JSON array of ``{"location", "path"}``) and
    scans each directory for files — the "configured-location scan" half of R18-A1
    §1. AgentIQ never discovers locations on its own; a deployment declares exactly
    the directories in scope. The change signature is ``"{mtime_ns}:{size}"``, so a
    file is re-read only after its content changes.

    A single unreadable file or missing location is logged and skipped (by
    id/path, never by content) so one bad path does not block the scan — the
    project's "degrade, don't crash" ingestion rule.
    """

    def __init__(self, locations: Optional[List[Dict[str, str]]] = None):
        self._locations = locations if locations is not None else _load_locations()

    def list_documents(self, org_id: str) -> List[DocumentRef]:
        refs: List[DocumentRef] = []
        for loc in self._locations:
            name = str(loc.get("location", "")).strip()
            root = loc.get("path")
            if not root:
                continue
            base = Path(root)
            if not base.is_dir():
                logger.warning(
                    "documents: configured location %r path is not a directory — skipping",
                    name or root,
                )
                continue
            for path in sorted(p for p in base.rglob("*") if p.is_file()):
                try:
                    stat = path.stat()
                    rel = path.relative_to(base).as_posix()
                    ts = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
                    refs.append(
                        DocumentRef(
                            artifact_id=f"{name}/{rel}" if name else rel,
                            filename=path.name,
                            location=name,
                            signature=f"{stat.st_mtime_ns}:{stat.st_size}",
                            source_timestamp=ts,
                            size_bytes=stat.st_size,
                            provenance={"path": str(path)},
                        )
                    )
                except OSError as exc:
                    logger.warning(
                        "documents: could not stat file in location %r (skipping): %s",
                        name or root,
                        exc,
                    )
        return refs

    def read(self, org_id: str, ref: DocumentRef) -> bytes:
        path = (ref.provenance or {}).get("path")
        if not path:
            raise DocumentSourceError(f"no path recorded for {ref.artifact_id!r}")
        with open(path, "rb") as fh:
            return fh.read()


def _load_locations() -> List[Dict[str, str]]:
    """Parse the ``DOCUMENT_LOCATIONS`` env var into a list of location configs."""
    raw = os.getenv(_LOCATIONS_ENV, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DocumentSourceError(
            f"{_LOCATIONS_ENV} is not valid JSON: {type(exc).__name__}"
        ) from exc
    if not isinstance(parsed, list):
        raise DocumentSourceError(f"{_LOCATIONS_ENV} must be a JSON array of locations")
    return [e for e in parsed if isinstance(e, dict)]


def default_source(org_id: str) -> DocumentSource:
    """Return the document source for the current mode.

    Offline (default): the deterministic fixture. Live: the configured-location
    scan COMPOSED with the SharePoint document-library and Confluence attachment
    sources (R18-A1 / T5), so files surfaced by the 1.7 connectors flow through the
    same DocumentIngestor. A connector that is not connected (no vault token)
    simply contributes nothing — the composite isolates each source (degrade, don't
    crash), so live document ingestion still runs against whatever IS reachable.
    """
    if not is_live():
        return FixtureDocumentSource()

    # Lazy import avoids a module cycle (documents_attachments imports this module)
    # and keeps the SharePoint/Confluence connectors out of the offline path.
    from .documents_attachments import (
        CompositeDocumentSource,
        ConfluenceDocumentSource,
        SharePointDocumentSource,
    )

    return CompositeDocumentSource(
        [
            ConfiguredLocationSource(),
            SharePointDocumentSource(),
            ConfluenceDocumentSource(),
        ]
    )
