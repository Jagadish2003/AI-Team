"""MSP-B5 T1 — deterministic explicit-citation runbook matching.

The MSP pack's strongest signal is *documented + repeated + manual*. MSP-B4
delivered the "repeated" leg (``RecurrenceRecord``) and, as a by-product,
captured the runbook identifiers an engineer explicitly cited in the incident
resolution notes (``RecurrenceRecord.runbook_citations`` / ``cited_runbook_refs``).
This module restores the "documented" leg by resolving those already-captured
citations against the provenance of the ingested runbook library.

Two disciplines govern this whole file:

* **Deterministic, never semantic.** An explicit citation is resolved by
  matching STABLE IDENTIFIERS and NORMALISED-EXACT references — a runbook id
  (``KB0010234``), a URL, a document/page id, or an explicitly named runbook.
  There is no embedding lookup, cosine similarity, or fuzzy text matching here;
  that is MSP-B5 T2/T3's ``proposed`` path, deliberately kept out of this module.
* **Observed, because the engineer said so.** A citation match is
  ``origin='observed'`` (:data:`MATCH_OBSERVED`): the engineer directly named or
  linked the runbook, so the match is directly observed fact, not a proposal.

Every lookup is scoped by ``org_id`` (AC7): a citation from one organisation can
NEVER resolve against an identically named or linked runbook belonging to
another. The library implementations are hard-partitioned by org, and a cross-org
call raises :class:`OrgScopeError` rather than silently missing.

If a citation is missing, invalid, ambiguous, or cannot be resolved,
:func:`match_runbooks` returns ``None`` — no observed match, no guessing. The
no-match case is what MSP-B5 T5's documentation-gap finding consumes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from app.provenance import OBSERVED, EvidencePointer
from discovery.signals.evidence_store import OrgScopeError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from discovery.detectors.ops_recurrence import RecurrenceRecord

# ---- match-state lifecycle (Section 1 of the MSP-B5 story) ----
# T1 produces only OBSERVED. PROPOSED (T2/T3, carries match_confidence) and
# CONFIRMED (T4, analyst-hardened) are named here so the vocabulary lives in one
# place, but this module never emits them.
MATCH_OBSERVED = "observed"
MATCH_PROPOSED = "proposed"
MATCH_CONFIRMED = "confirmed"

# The source systems that carry runbook content in the 1.8 retrieval substrate.
# The runbook library is scoped to these when reading library provenance; reused
# by MSP-B5 T2's runbook-scoped retrieval so the "library" is defined in one place.
RUNBOOK_SOURCE_SYSTEMS: Tuple[str, ...] = ("document", "confluence", "sharepoint")

# Structured runbook identifier tokens (mirrors MSP-B4's capture regex) — matched
# case-insensitively, normalised to upper case. Anchored: a whole reference either
# IS such a token or it is treated as a URL / free identifier instead.
_IDENTIFIER_RE = re.compile(
    r"^(?:KB\d{4,}|RB\d{3,}|RUNBOOK[-_][A-Z0-9]+(?:[-_][A-Z0-9]+)*)$",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")

# Provenance keys a runbook page may declare a stable identifier under. Reading
# these off the ingested library's provenance is how a cited id joins to the page
# that documents it — deterministic, exact, no guessing.
_PROVENANCE_ID_KEYS: Tuple[str, ...] = (
    "runbook_id",
    "document_id",
    "doc_id",
    "page_id",
    "kb_number",
    "kb",
    "url",
    "source_url",
    "title",
    "name",
    "filename",
)


def _require_org(org_id: Any) -> str:
    """Return a non-empty org id or raise — every lookup is org-scoped (AC7)."""
    text = str(org_id).strip() if org_id is not None else ""
    if not text:
        raise OrgScopeError("org_id is required for runbook matching")
    return text


def _normalize_url(url: str) -> str:
    """Canonicalise a URL for exact comparison (scheme/host lower-cased, no
    fragment, no trailing slash). Deterministic string normalisation only — this
    is not fuzzy matching, just two spellings of the same link resolving equal."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def normalize_reference(ref: Any) -> Optional[str]:
    """Canonicalise a runbook reference to a stable, type-tagged match key.

    Returns ``None`` for an empty/unusable reference. The key is prefixed by kind
    (``id:`` / ``url:`` / ``name:``) so a token and an identically spelled free
    name can never collide across kinds. This is NORMALISED-EXACT matching — case
    and surrounding whitespace are folded, nothing else. No stemming, no
    tokenisation, no similarity: those belong to the semantic path (T2/T3).
    """
    if ref is None:
        return None
    text = str(ref).strip()
    if not text:
        return None
    if _URL_RE.match(text):
        return "url:" + _normalize_url(text)
    if _IDENTIFIER_RE.match(text):
        return "id:" + text.upper()
    collapsed = _WHITESPACE_RE.sub(" ", text).strip().casefold()
    return ("name:" + collapsed) if collapsed else None


@dataclass(frozen=True)
class RunbookPage:
    """One runbook page/document in the ingested library, with its stable ids.

    ``source_system`` + ``source_artifact`` are the substrate's stable-id key (the
    page/document id). ``identifiers`` are every stable reference the page declares
    (its runbook id, URL, page id, title) — the citation side is matched against
    the normalised form of these plus the ``source_artifact`` itself.
    """

    org_id: str
    source_system: str
    source_artifact: str
    identifiers: Tuple[str, ...] = ()
    title: Optional[str] = None
    url: Optional[str] = None
    source_timestamp: Optional[str] = None
    chunk_id: Optional[str] = None
    retrieval_result_id: Optional[str] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    def match_keys(self) -> frozenset:
        """The normalised match keys this page resolves for (its stable ids)."""
        keys = set()
        candidates = list(self.identifiers) + [self.source_artifact, self.url, self.title]
        for candidate in candidates:
            key = normalize_reference(candidate)
            if key:
                keys.add(key)
        return frozenset(keys)

    def evidence_pointer(self) -> Dict[str, Any]:
        """An OBSERVED evidence pointer to this runbook page (the resolved side)."""
        if self.chunk_id and self.retrieval_result_id:
            pointer = EvidencePointer.retrieved(
                source_system=self.source_system,
                source_artifact=self.source_artifact,
                chunk_id=self.chunk_id,
                retrieval_result_id=self.retrieval_result_id,
                source_timestamp=self.source_timestamp,
                source_artifact_type="record_id",
            )
        else:
            pointer = EvidencePointer.observed(
                source_system=self.source_system,
                source_artifact=self.source_artifact,
                source_timestamp=self.source_timestamp,
                source_artifact_type="record_id",
            )
        return pointer.to_dict()

    def summary(self) -> Dict[str, Any]:
        """The matched-runbook descriptor carried on a :class:`RunbookMatch`."""
        return {
            "source_system": self.source_system,
            "source_artifact": self.source_artifact,
            "title": self.title,
            "url": self.url,
            "identifiers": list(self.identifiers),
        }


class RunbookLibrary:
    """Resolution seam: an org-scoped, deterministic runbook-id → page lookup.

    Implementations MUST be hard-partitioned by org — ``resolve`` may only ever
    return pages belonging to ``org_id`` (AC7). ``resolve`` returns every distinct
    page whose stable identifiers match ``normalized_ref``; more than one means the
    reference is ambiguous and the caller must not guess.
    """

    def resolve(self, org_id: str, normalized_ref: str) -> Tuple[RunbookPage, ...]:
        raise NotImplementedError


class InMemoryRunbookLibrary(RunbookLibrary):
    """Deterministic, org-partitioned in-memory library (offline + tests).

    Pages are bucketed by ``org_id`` at insert time, so a lookup with one org's id
    physically cannot see another org's pages — the org boundary is structural, not
    a filter a caller can forget.
    """

    def __init__(self, pages: Optional[Sequence[RunbookPage]] = None) -> None:
        self._by_org: Dict[str, list] = {}
        for page in pages or ():
            self.add(page)

    def add(self, page: RunbookPage) -> None:
        org = _require_org(page.org_id)
        self._by_org.setdefault(org, []).append(page)

    def resolve(self, org_id: str, normalized_ref: str) -> Tuple[RunbookPage, ...]:
        org = _require_org(org_id)
        if not normalized_ref:
            return ()
        # Distinct by stable-id key so the same page listed twice is not counted
        # as ambiguity; different pages sharing a key IS ambiguity.
        matched: Dict[Tuple[str, str], RunbookPage] = {}
        for page in self._by_org.get(org, ()):  # only this org's partition
            if normalized_ref in page.match_keys():
                matched[(page.source_system, page.source_artifact)] = page
        return tuple(matched.values())


class RetrievalRunbookLibrary(RunbookLibrary):
    """Runbook library backed by the 1.8 retrieval substrate's provenance.

    Builds a per-org index of ``normalized reference -> page(s)`` from the DISTINCT
    indexed artifacts of the runbook-scoped source systems, reading each artifact's
    provenance for the stable identifiers it declares. This is a deterministic
    provenance join (no vectors, no similarity), which is exactly what an explicit
    citation needs.

    Defensive by design: it never raises into a discovery run — if the substrate is
    unavailable the index is empty and every citation simply fails to resolve
    (documentation-gap path), never a crash. The provenance reader is injectable so
    the join logic is unit-testable without a database.
    """

    def __init__(
        self,
        *,
        source_systems: Sequence[str] = RUNBOOK_SOURCE_SYSTEMS,
        provenance_reader: Optional[Any] = None,
    ) -> None:
        self._source_systems = tuple(source_systems)
        self._reader = provenance_reader
        self._cache: Dict[str, Dict[str, Dict[Tuple[str, str], RunbookPage]]] = {}

    def _read(self, org_id: str) -> list:
        # Never raise into a discovery run: any failure reading library provenance
        # degrades to an empty index, so the citation simply fails to resolve
        # (documentation-gap path) instead of crashing the run.
        try:
            reader = self._reader
            if reader is None:
                from app.retrieval import store  # lazy: keep DB import off hot path
                reader = store.iter_artifact_provenance
            return list(reader(org_id, self._source_systems) or ())
        except Exception:  # pragma: no cover - substrate unavailable / read failed
            return []

    def _index(self, org_id: str) -> Dict[str, Dict[Tuple[str, str], RunbookPage]]:
        if org_id in self._cache:
            return self._cache[org_id]
        index: Dict[str, Dict[Tuple[str, str], RunbookPage]] = {}
        for row in self._read(org_id):
            page = _page_from_provenance_row(org_id, row)
            if page is None:
                continue
            page_key = (page.source_system, page.source_artifact)
            for key in page.match_keys():
                index.setdefault(key, {})[page_key] = page
        self._cache[org_id] = index
        return index

    def resolve(self, org_id: str, normalized_ref: str) -> Tuple[RunbookPage, ...]:
        org = _require_org(org_id)
        if not normalized_ref:
            return ()
        return tuple(self._index(org).get(normalized_ref, {}).values())


def _page_from_provenance_row(org_id: str, row: Mapping[str, Any]) -> Optional[RunbookPage]:
    """Build a :class:`RunbookPage` from one substrate provenance row (or None)."""
    if not isinstance(row, Mapping):
        return None
    source_system = str(row.get("source_system") or "").strip()
    source_artifact = str(row.get("source_artifact") or "").strip()
    if not source_system or not source_artifact:
        return None
    provenance = row.get("provenance")
    provenance = dict(provenance) if isinstance(provenance, Mapping) else {}
    identifiers = []
    for key in _PROVENANCE_ID_KEYS:
        value = provenance.get(key)
        if isinstance(value, str) and value.strip():
            identifiers.append(value.strip())
        elif isinstance(value, (list, tuple)):
            identifiers.extend(str(v).strip() for v in value if str(v).strip())
    url = provenance.get("url") or provenance.get("source_url")
    title = provenance.get("title") or provenance.get("name")
    return RunbookPage(
        org_id=org_id,
        source_system=source_system,
        source_artifact=source_artifact,
        identifiers=tuple(dict.fromkeys(identifiers)),  # dedupe, keep order
        title=str(title).strip() if isinstance(title, str) and title.strip() else None,
        url=str(url).strip() if isinstance(url, str) and url.strip() else None,
        source_timestamp=row.get("source_timestamp") or provenance.get("source_timestamp"),
        chunk_id=row.get("chunk_id"),
        provenance=provenance,
    )


@dataclass(frozen=True)
class RunbookMatch:
    """A match between a recurrence's runbook citation(s) and a library page.

    Reusable across the MSP-B5 lifecycle: T1 emits only ``match_state='observed'``
    / ``origin='observed'`` (an explicit citation). T2/T3 reuse the same structure
    for ``proposed`` matches (carrying ``match_confidence``); T4 hardens accepted
    proposals to ``confirmed``. Both sides of the match are evidenced:
    ``citing_incident_evidence`` points at the incidents that cited the runbook and
    ``runbook_evidence`` at the exact runbook page/document resolved from the
    library.
    """

    org_id: str
    recurrence_id: str
    match_state: str
    origin: str
    runbook: Dict[str, Any]
    runbook_evidence: Dict[str, Any]
    citing_incident_evidence: Tuple[Dict[str, Any], ...]
    cited_references: Tuple[str, ...]
    match_confidence: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "org_id": self.org_id,
            "recurrence_id": self.recurrence_id,
            "match_state": self.match_state,
            "origin": self.origin,
            "runbook": dict(self.runbook),
            "runbook_evidence": dict(self.runbook_evidence),
            "citing_incident_evidence": [
                dict(pointer) for pointer in self.citing_incident_evidence
            ],
            "cited_references": list(self.cited_references),
            "match_confidence": self.match_confidence,
        }


def match_runbooks(
    org_id: str,
    rec: "RecurrenceRecord",
    library: Optional[RunbookLibrary] = None,
) -> Optional[RunbookMatch]:
    """Resolve a recurrence's explicit runbook citations to an OBSERVED match.

    Deterministic and org-scoped. Returns a :class:`RunbookMatch` with
    ``origin='observed'`` when the recurrence's citations resolve, unambiguously,
    to exactly one runbook page in the org's library; otherwise ``None`` — a
    missing, invalid, ambiguous, or unresolvable citation is NOT a match and is
    never guessed at (the semantic ``proposed`` path is MSP-B5 T2/T3).

    ``library`` defaults to the retrieval-substrate-backed library; inject an
    :class:`InMemoryRunbookLibrary` for offline/deterministic use.
    """
    org = _require_org(org_id)
    rec_org = getattr(rec, "org_id", None)
    if rec_org and str(rec_org).strip() != org:
        # A recurrence from one org may never be matched under another (AC7).
        raise OrgScopeError(
            f"recurrence belongs to org {rec_org!r}, cannot match under {org!r}"
        )

    citations = tuple(getattr(rec, "runbook_citations", ()) or ())
    if not citations:
        return None  # no citation -> no observed match (documentation-gap path)

    if library is None:
        library = default_runbook_library()

    # Resolve each cited reference against the org's library, grouping by the
    # distinct page each resolves to and remembering which incidents cited it.
    resolved: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for citation in citations:
        if not isinstance(citation, Mapping):
            continue
        incident_evidence = citation.get("evidence")
        incident_sys_id = citation.get("incident_sys_id")
        for raw_ref in citation.get("runbook_references", ()) or ():
            key = normalize_reference(raw_ref)
            if not key:
                continue  # invalid / unusable reference
            pages = library.resolve(org, key)
            if len(pages) > 1:
                return None  # a single citation matching many runbooks is ambiguous
            if not pages:
                continue  # cannot be resolved
            page = pages[0]
            page_key = (page.source_system, page.source_artifact)
            entry = resolved.setdefault(
                page_key, {"page": page, "refs": set(), "incidents": {}}
            )
            entry["refs"].add(str(raw_ref))
            pointer = _safe_pointer(incident_evidence)
            if pointer is not None:
                dedup_key = str(incident_sys_id).strip() if incident_sys_id else ""
                entry["incidents"][dedup_key or pointer["source_artifact"]] = pointer

    if len(resolved) != 1:
        # Zero -> unresolvable; more than one distinct runbook -> ambiguous.
        return None

    (entry,) = resolved.values()
    page: RunbookPage = entry["page"]
    citing_evidence = tuple(
        entry["incidents"][key] for key in sorted(entry["incidents"])
    )
    return RunbookMatch(
        org_id=org,
        recurrence_id=str(getattr(rec, "record_id", "") or ""),
        match_state=MATCH_OBSERVED,
        origin=OBSERVED,
        runbook=page.summary(),
        runbook_evidence=page.evidence_pointer(),
        citing_incident_evidence=citing_evidence,
        cited_references=tuple(sorted(entry["refs"])),
    )


def _safe_pointer(value: Any) -> Optional[Dict[str, Any]]:
    """Return a valid OBSERVED evidence-pointer dict, or None. Allow-lists fields
    so a caller-supplied blob cannot smuggle unknown keys into the match."""
    if not isinstance(value, Mapping):
        return None
    allowed = set(EvidencePointer.__dataclass_fields__)
    pointer = EvidencePointer.from_dict({k: value.get(k) for k in allowed if k in value})
    return pointer.to_dict() if pointer.is_valid() else None


def default_runbook_library() -> RunbookLibrary:
    """The production runbook library — backed by the retrieval substrate."""
    return RetrievalRunbookLibrary()
