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

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from app.provenance import OBSERVED, EvidencePointer
from discovery.signals.evidence_store import OrgScopeError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from discovery.detectors.ops_recurrence import RecurrenceRecord

logger = logging.getLogger(__name__)

# ---- match-state lifecycle (Section 1 of the MSP-B5 story) ----
# T1 produces only OBSERVED. PROPOSED (T2/T3, carries match_confidence) and
# CONFIRMED (T4, analyst-hardened) are named here so the vocabulary lives in one
# place, but this module never emits them.
MATCH_OBSERVED = "observed"
MATCH_PROPOSED = "proposed"
MATCH_CONFIRMED = "confirmed"

# Explicit-citation resolution has the same availability distinction as semantic
# retrieval. A successful lookup with no page is meaningful; a failed library
# read is not evidence that documentation is absent.
CITATION_RESOLUTION_OK = "ok"
CITATION_RESOLUTION_UNAVAILABLE = "unavailable"

# The source systems that carry runbook content in the 1.8 retrieval substrate.
# The runbook library is scoped to these when reading library provenance; reused
# by MSP-B5 T2's runbook-scoped retrieval so the "library" is defined in one place.
RUNBOOK_SOURCE_SYSTEMS: Tuple[str, ...] = ("document", "confluence", "sharepoint")

#: SharePoint indexes TWO different things under ``source_system='sharepoint'``:
#: page-native content (``"{site}:page:{id}"`` / ``"{site}:list:{id}"``), which is
#: real prose and legitimate runbook material; and the reach connector's driveItem
#: METADATA (``"{site}/{drive}:{item}"``), whose refresh resolver renders only
#: ``Name/Type/Size/Parent/URL`` — a filename card, not documentation.
#:
#: Both are chunked and embedded, so without this a changed spreadsheet competes
#: as a runbook candidate: "Name: Q3-budget.xlsx / Type: file / URL: ..." can out-
#: score a real runbook on a filename term, and a documentation-gap detector can be
#: talked out of firing by a file that documents nothing. The metadata stays
#: indexed (filenames remain searchable elsewhere); it is excluded HERE, where the
#: question is specifically "is this a documented procedure?".
#:
#: Distinguishing them needs no new field: the two id namespaces are structurally
#: disjoint by construction (``sharepoint_content._page_artifact_id``).
_SHAREPOINT_CONTENT_INFIXES: Tuple[str, ...] = (":page:", ":list:")


def is_runbook_content_artifact(source_system: str, source_artifact: str) -> bool:
    """True when an indexed artifact is page-native content, not file metadata.

    Only SharePoint carries both kinds under one source system; every other
    runbook source indexes prose exclusively, so they always qualify.
    """
    if str(source_system or "").strip().lower() != "sharepoint":
        return True
    artifact = str(source_artifact or "")
    return any(infix in artifact for infix in _SHAREPOINT_CONTENT_INFIXES)

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
        # Never let the shared pointer builder substitute the wall clock. A library
        # row without a source time stays honestly unavailable and repeated matching
        # remains byte-for-byte deterministic.
        source_timestamp = self.source_timestamp or "not_available"
        if self.chunk_id and self.retrieval_result_id:
            pointer = EvidencePointer.retrieved(
                source_system=self.source_system,
                source_artifact=self.source_artifact,
                chunk_id=self.chunk_id,
                retrieval_result_id=self.retrieval_result_id,
                source_timestamp=source_timestamp,
                source_artifact_type="record_id",
            )
        else:
            pointer = EvidencePointer.observed(
                source_system=self.source_system,
                source_artifact=self.source_artifact,
                source_timestamp=source_timestamp,
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

    def resolve_checked(
        self, org_id: str, normalized_ref: str
    ) -> Tuple[str, Tuple[RunbookPage, ...]]:
        """Resolve and report whether the library lookup was available."""
        return CITATION_RESOLUTION_OK, self.resolve(org_id, normalized_ref)


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

    Defensive by design: it never raises into a discovery run. A failed substrate
    read returns an unavailable checked result, keeping it distinct from a
    successful empty lookup so it cannot become a false documentation gap. The
    provenance reader is injectable so the join logic is unit-testable without a
    database.
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
        self._availability: Dict[str, str] = {}

    def _read(self, org_id: str) -> Tuple[str, list]:
        # Never raise into a discovery run; preserve an explicit unavailable state.
        try:
            reader = self._reader
            if reader is None:
                from app.retrieval import store  # lazy: keep DB import off hot path
                reader = store.iter_artifact_provenance
            return CITATION_RESOLUTION_OK, list(reader(org_id, self._source_systems) or ())
        except Exception:  # pragma: no cover - substrate unavailable / read failed
            logger.warning(
                "explicit runbook citation resolution unavailable for org %s",
                org_id,
                exc_info=True,
            )
            return CITATION_RESOLUTION_UNAVAILABLE, []

    def _index(self, org_id: str) -> Dict[str, Dict[Tuple[str, str], RunbookPage]]:
        if org_id in self._cache:
            return self._cache[org_id]
        index: Dict[str, Dict[Tuple[str, str], RunbookPage]] = {}
        status, rows = self._read(org_id)
        self._availability[org_id] = status
        for row in rows:
            page = _page_from_provenance_row(org_id, row)
            if page is None:
                continue
            page_key = (page.source_system, page.source_artifact)
            for key in page.match_keys():
                index.setdefault(key, {})[page_key] = page
        self._cache[org_id] = index
        return index

    def resolve(self, org_id: str, normalized_ref: str) -> Tuple[RunbookPage, ...]:
        return self.resolve_checked(org_id, normalized_ref)[1]

    def resolve_checked(
        self, org_id: str, normalized_ref: str
    ) -> Tuple[str, Tuple[RunbookPage, ...]]:
        org = _require_org(org_id)
        if not normalized_ref:
            return CITATION_RESOLUTION_OK, ()
        pages = tuple(self._index(org).get(normalized_ref, {}).values())
        return self._availability.get(org, CITATION_RESOLUTION_OK), pages


def _page_from_provenance_row(org_id: str, row: Mapping[str, Any]) -> Optional[RunbookPage]:
    """Build a :class:`RunbookPage` from one substrate provenance row (or None)."""
    if not isinstance(row, Mapping):
        return None
    source_system = str(row.get("source_system") or "").strip()
    source_artifact = str(row.get("source_artifact") or "").strip()
    if not source_system or not source_artifact:
        return None
    # A SharePoint driveItem's indexed content is filename metadata, not a
    # documented procedure — it must not enter the runbook library.
    if not is_runbook_content_artifact(source_system, source_artifact):
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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunbookMatch":
        """Rebuild a stored match without accepting arbitrary extra fields.

        MSP-B5 T4 persists proposed matches before an analyst decides on them.
        Keeping deserialisation here means the persisted lifecycle and the
        detector use one contract instead of maintaining two subtly different
        match shapes.
        """
        if not isinstance(value, Mapping):
            raise ValueError("runbook match must be a mapping")
        recurrence_id = str(value.get("recurrence_id") or "").strip()
        if not recurrence_id:
            raise ValueError("recurrence_id is required on a runbook match")
        state = str(value.get("match_state") or "").strip().lower()
        origin = str(value.get("origin") or "").strip().lower()
        if state not in (MATCH_OBSERVED, MATCH_PROPOSED, MATCH_CONFIRMED):
            raise ValueError(f"invalid runbook match state: {state!r}")
        if origin != state:
            raise ValueError("runbook match origin must equal its lifecycle state")
        return cls(
            org_id=_require_org(value.get("org_id")),
            recurrence_id=recurrence_id,
            match_state=state,
            origin=origin,
            runbook=dict(value.get("runbook") or {}),
            runbook_evidence=dict(value.get("runbook_evidence") or {}),
            citing_incident_evidence=tuple(
                dict(pointer)
                for pointer in (value.get("citing_incident_evidence") or ())
                if isinstance(pointer, Mapping)
            ),
            cited_references=tuple(
                str(reference)
                for reference in (value.get("cited_references") or ())
                if str(reference).strip()
            ),
            match_confidence=(
                float(value["match_confidence"])
                if value.get("match_confidence") is not None
                else None
            ),
        )

    def with_state(self, match_state: str) -> "RunbookMatch":
        """Return the same match in a valid lifecycle state.

        Only analyst acceptance may call this with ``confirmed``.  Evidence is
        intentionally unchanged: confirmation changes the status of the match,
        not the source page or retrieval result that led to it.
        """
        state = str(match_state or "").strip().lower()
        if state not in (MATCH_OBSERVED, MATCH_PROPOSED, MATCH_CONFIRMED):
            raise ValueError(f"invalid runbook match state: {match_state!r}")
        return RunbookMatch(
            org_id=self.org_id,
            recurrence_id=self.recurrence_id,
            match_state=state,
            origin=state,
            runbook=dict(self.runbook),
            runbook_evidence=dict(self.runbook_evidence),
            citing_incident_evidence=tuple(
                dict(pointer) for pointer in self.citing_incident_evidence
            ),
            cited_references=tuple(self.cited_references),
            match_confidence=self.match_confidence,
        )


@dataclass(frozen=True)
class CitationResolutionResult:
    """Availability-aware outcome of deterministic citation resolution.

    ``match is None`` proves no explicit match only when ``status`` is ``ok``.
    Checked references and the reason keep the conclusion auditable.
    """

    status: str
    match: Optional[RunbookMatch]
    checked_references: Tuple[str, ...] = ()
    reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.status == CITATION_RESOLUTION_OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "match": self.match.as_dict() if self.match else None,
            "checked_references": list(self.checked_references),
            "reason": self.reason,
        }


def resolve_runbook_citations(
    org_id: str,
    rec: "RecurrenceRecord",
    library: Optional[RunbookLibrary] = None,
) -> CitationResolutionResult:
    """Resolve a recurrence's explicit runbook citations to an OBSERVED match.

    Deterministic and org-scoped. The outcome carries an observed match when the
    recurrence resolves unambiguously to one page. A successful miss and a failed
    library read remain distinct; neither is guessed into a match.

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
        return CitationResolutionResult(
            status=CITATION_RESOLUTION_OK,
            match=None,
            reason="no_explicit_citation",
        )

    if library is None:
        library = default_runbook_library()

    # Resolve each cited reference against the org's library, grouping by the
    # distinct page each resolves to and remembering which incidents cited it.
    resolved: Dict[Tuple[str, str], Dict[str, Any]] = {}
    checked_references = set()
    for citation in citations:
        if not isinstance(citation, Mapping):
            continue
        incident_evidence = citation.get("evidence")
        incident_sys_id = citation.get("incident_sys_id")
        for raw_ref in citation.get("runbook_references", ()) or ():
            if str(raw_ref).strip():
                checked_references.add(str(raw_ref).strip())
            key = normalize_reference(raw_ref)
            if not key:
                continue  # invalid / unusable reference
            status, pages = library.resolve_checked(org, key)
            if status == CITATION_RESOLUTION_UNAVAILABLE:
                return CitationResolutionResult(
                    status=CITATION_RESOLUTION_UNAVAILABLE,
                    match=None,
                    checked_references=tuple(sorted(checked_references)),
                    reason="runbook_library_unavailable",
                )
            if len(pages) > 1:
                return CitationResolutionResult(
                    status=CITATION_RESOLUTION_OK,
                    match=None,
                    checked_references=tuple(sorted(checked_references)),
                    reason="ambiguous_citation",
                )
            if not pages:
                continue  # cannot be resolved
            page = pages[0]
            if _require_org(page.org_id) != org:
                raise OrgScopeError(
                    "runbook library returned a page outside the requested org"
                )
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
        return CitationResolutionResult(
            status=CITATION_RESOLUTION_OK,
            match=None,
            checked_references=tuple(sorted(checked_references)),
            reason=(
                "unresolved_citation" if not resolved else "ambiguous_runbook_match"
            ),
        )

    (entry,) = resolved.values()
    page: RunbookPage = entry["page"]
    citing_evidence = tuple(
        entry["incidents"][key] for key in sorted(entry["incidents"])
    )
    match = RunbookMatch(
        org_id=org,
        recurrence_id=str(getattr(rec, "record_id", "") or ""),
        match_state=MATCH_OBSERVED,
        origin=OBSERVED,
        runbook=page.summary(),
        runbook_evidence=page.evidence_pointer(),
        citing_incident_evidence=citing_evidence,
        cited_references=tuple(sorted(entry["refs"])),
    )
    return CitationResolutionResult(
        status=CITATION_RESOLUTION_OK,
        match=match,
        checked_references=tuple(sorted(checked_references)),
        reason="explicit_citation_resolved",
    )


def match_runbooks(
    org_id: str,
    rec: "RecurrenceRecord",
    library: Optional[RunbookLibrary] = None,
) -> Optional[RunbookMatch]:
    """Return the observed match while preserving the original T1 API.

    Use :func:`resolve_runbook_citations` when an unavailable library must remain
    distinct from a successful no-match.
    """
    return resolve_runbook_citations(org_id, rec, library).match


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


# ═════════════════════════════════════════════════════════════════════════════
# MSP-B5 T2 — the semantic retrieval PATH (query construction + runbook-scoped
# retrieval). This is the input to T3's scoring/threshold, invoked for recurrence
# records that have NO resolved explicit citation (T1). It PROPOSES candidates;
# it never decides a match — T3 scores them and, above threshold, emits a
# RunbookMatch(origin='proposed'). Reuses the Release 1.8 retrieval API
# (``app.retrieval.api.retrieve``); it never opens a second vector store or search.
# ═════════════════════════════════════════════════════════════════════════════

# The runbook library scope: the source systems that carry runbook content
# (R18-A1 documents + R18-A5 Confluence/SharePoint page deep content). Restricting
# retrieval to these keeps Slack messages, source code, incidents, and other
# unrelated content out of the runbook candidate set. Alias of the T1 scope so the
# "what counts as the runbook library" answer lives in exactly one place.
RUNBOOK_SCOPE: Tuple[str, ...] = RUNBOOK_SOURCE_SYSTEMS

# Retrieval-path result status — the load-bearing distinction the task requires:
# a genuine "search ran, found no runbook" (OK + no candidates) must never be
# confused with "we could not search" (UNAVAILABLE: embedding provider down or a
# retrieval failure). T3 must not read a documentation gap from UNAVAILABLE.
RETRIEVAL_OK = "ok"
RETRIEVAL_UNAVAILABLE = "unavailable"

# Thresholds are CONFIG, not code (AC3): overridable per deployment via env, or
# per call via a RunbookRetrievalConfig. Defaults are conservative — a tight
# candidate set and a similarity floor so weak matches never enter T3's scoring.
DEFAULT_CANDIDATE_LIMIT = 5
DEFAULT_MIN_SCORE = 0.7
CANDIDATE_LIMIT_ENV = "MSP_B5_RUNBOOK_CANDIDATE_LIMIT"
MIN_SCORE_ENV = "MSP_B5_RUNBOOK_MIN_SCORE"
INCLUDE_STALE_ENV = "MSP_B5_RUNBOOK_INCLUDE_STALE"
SOURCE_SYSTEMS_ENV = "MSP_B5_RUNBOOK_SOURCE_SYSTEMS"

# Fixed, ordered structured fields that form the resolution-pattern query. Order
# is fixed so the SAME recurrence always yields the SAME query text (determinism).
_QUERY_STRUCTURED_FIELDS = ("category", "close_code", "ci_class", "resolved_by_group")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logger.warning("runbook retrieval: %s=%r is not an int; using %d", name, raw, default)
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        logger.warning("runbook retrieval: %s=%r is not a float; using %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class RunbookRetrievalConfig:
    """Tunable retrieval-path knobs — thresholds are config, not code (AC3).

    ``candidate_limit`` is the ``k`` passed to the retrieval API; ``min_score`` is
    the cosine-similarity floor below which a chunk is not even a candidate;
    ``include_stale`` keeps stale chunks EXCLUDED by default (a changed-but-not-yet-
    refreshed runbook is not served as current evidence) unless a caller explicitly
    opts to surface staleness; ``source_systems`` is the runbook library scope.
    """

    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT
    min_score: float = DEFAULT_MIN_SCORE
    include_stale: bool = False
    source_systems: Tuple[str, ...] = RUNBOOK_SCOPE

    @classmethod
    def from_env(cls) -> "RunbookRetrievalConfig":
        raw_sources = os.getenv(SOURCE_SYSTEMS_ENV)
        if raw_sources and raw_sources.strip():
            sources = tuple(
                s.strip() for s in raw_sources.split(",") if s.strip()
            ) or RUNBOOK_SCOPE
        else:
            sources = RUNBOOK_SCOPE
        return cls(
            candidate_limit=_env_int(CANDIDATE_LIMIT_ENV, DEFAULT_CANDIDATE_LIMIT),
            min_score=_env_float(MIN_SCORE_ENV, DEFAULT_MIN_SCORE),
            include_stale=_env_bool(INCLUDE_STALE_ENV, False),
            source_systems=sources,
        )


def _ci_class_from_component(component: Any) -> str:
    """Extract the CI CLASS from a signature ``ci_component`` marker, else ''.

    ``ci_component`` is ``'class:<ci_class>'`` (broad, descriptive), ``'ci:<sys_id>'``
    (a specific, opaque record id), or ``''``. Only the class is useful query text;
    an opaque sys_id would be search noise, so it is deliberately dropped.
    """
    text = str(component or "").strip()
    if text.lower().startswith("class:"):
        return text[len("class:"):].strip()
    return ""


def _query_fields(rec: "RecurrenceRecord") -> Dict[str, str]:
    """Pull the structured resolution-pattern fields off the recurrence record.

    Reads ONLY the deterministic, privacy-safe signature components (never free
    text, never short-description tokens — those are stripped upstream). A missing
    component yields an empty string, handled predictably by the query builder.
    """
    components = getattr(rec, "signature_components", None) or {}
    resolution = components.get("resolution") or {}
    identity = components.get("incident_identity") or {}
    category = resolution.get("category") or identity.get("category") or ""
    ci_class = _ci_class_from_component(
        resolution.get("ci_component") or identity.get("ci_component")
    )
    return {
        "category": str(category).strip(),
        "close_code": str(resolution.get("close_code") or "").strip(),
        "ci_class": ci_class,
        "resolved_by_group": str(resolution.get("resolved_by_group") or "").strip(),
    }


def build_resolution_query(
    rec: "RecurrenceRecord",
    *,
    redacted_texts: Sequence[str] = (),
) -> str:
    """Deterministically construct the runbook retrieval query for a recurrence.

    The query is the recurrence's normalised resolution pattern — its category,
    close code, CI class, and resolved-by group — followed by any REDACTED
    resolution text. Only redacted text supplied by MSP-B4 (whose T6 redacts notes
    before they leave the incident) may be passed in; this builder holds no path to
    raw notes, so a seeded secret cannot reach the query (AC7). The recurrence
    record carries no free text itself, so the structured pattern is always safe.

    Determinism (task requirement): a fixed field order and stable de-duplication
    mean the SAME recurrence record (and same redacted texts) always yields the
    SAME query string. Empty or incomplete fields are simply omitted — never filled
    with a placeholder that would add noise to the embedding.
    """
    parts: list = []
    fields = _query_fields(rec)
    for key in _QUERY_STRUCTURED_FIELDS:
        value = fields.get(key, "")
        if value:
            parts.append(value)
    for text in redacted_texts or ():
        cleaned = _WHITESPACE_RE.sub(" ", str(text or "")).strip()
        if cleaned and cleaned not in parts:
            parts.append(cleaned)
    return " ".join(parts)


@dataclass(frozen=True)
class RunbookCandidate:
    """One runbook-library chunk proposed by retrieval (input to T3 scoring)."""

    source_system: str
    source_artifact: str
    content: str
    similarity: float
    chunk_id: str
    retrieval_result_id: str
    source_timestamp: str
    is_stale: bool = False

    @classmethod
    def from_chunk(cls, chunk: Any) -> "RunbookCandidate":
        return cls(
            source_system=getattr(chunk, "source_system", ""),
            source_artifact=getattr(chunk, "source_artifact", ""),
            content=getattr(chunk, "content", ""),
            similarity=float(getattr(chunk, "similarity", 0.0) or 0.0),
            chunk_id=getattr(chunk, "chunk_id", ""),
            retrieval_result_id=getattr(chunk, "retrieval_result_id", ""),
            source_timestamp=getattr(chunk, "source_timestamp", "") or "",
            is_stale=bool(getattr(chunk, "is_stale", False)),
        )


@dataclass(frozen=True)
class RunbookRetrievalResult:
    """The outcome of the runbook retrieval path — status + proposed candidates.

    ``status`` is the load-bearing field: :data:`RETRIEVAL_OK` means the search ran
    (``candidates`` may still be empty — a genuine "no runbook matched"), while
    :data:`RETRIEVAL_UNAVAILABLE` means retrieval could not run (embedding provider
    down or a retrieval failure) and the empty candidate set carries NO meaning. A
    discovery run never breaks on either outcome.
    """

    status: str
    query: str
    candidates: Tuple[RunbookCandidate, ...] = ()

    @property
    def available(self) -> bool:
        return self.status == RETRIEVAL_OK

    @property
    def found_any(self) -> bool:
        return bool(self.candidates)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "query": self.query,
            "candidates": [
                {
                    "source_system": c.source_system,
                    "source_artifact": c.source_artifact,
                    "similarity": c.similarity,
                    "chunk_id": c.chunk_id,
                    "retrieval_result_id": c.retrieval_result_id,
                    "is_stale": c.is_stale,
                }
                for c in self.candidates
            ],
        }


def _default_retrieve(
    org_id: str,
    query_text: str,
    *,
    k: int,
    source_filter: Sequence[str],
    min_score: float,
    include_stale: bool,
) -> list:
    """Call the Release 1.8 retrieval API (no separate search implementation)."""
    from app.retrieval.api import retrieve  # lazy: keep DB/gateway off the import path

    return retrieve(
        org_id,
        query_text,
        k=k,
        source_filter=list(source_filter),
        min_score=min_score,
        include_stale=include_stale,
    )


def _default_embedding_available(query_text: str) -> bool:
    """True iff the embedding provider can currently embed — the signal that tells
    a genuine empty result apart from a degraded provider. Probes through the SAME
    gateway the retrieval API uses; a miss (empty vectors) means unavailable."""
    from app.retrieval import embedder  # lazy

    vectors, _ = embedder.embed_texts_with_model([query_text])
    return bool(vectors)


def retrieve_runbook_candidates(
    org_id: str,
    rec: "RecurrenceRecord",
    *,
    config: Optional[RunbookRetrievalConfig] = None,
    redacted_texts: Sequence[str] = (),
    retrieve_fn: Optional[Callable[..., list]] = None,
    embedding_available_fn: Optional[Callable[[str], bool]] = None,
) -> RunbookRetrievalResult:
    """Retrieve runbook-library candidates for a non-cited recurrence (MSP-B5 T2).

    Builds the deterministic resolution-pattern query and searches ONLY the runbook
    library via the org-scoped Release 1.8 retrieval API, with a configured
    candidate limit and minimum score, stale chunks excluded by default. Returns a
    :class:`RunbookRetrievalResult` whose ``status`` keeps an unavailable provider /
    retrieval failure distinguishable from a genuine no-match — and never raises
    into the discovery run.

    ``retrieve_fn`` / ``embedding_available_fn`` are injectable for tests; in
    production they default to the 1.8 retrieval API and its embedding gateway.
    """
    org = _require_org(org_id)
    rec_org = getattr(rec, "org_id", None)
    if rec_org and str(rec_org).strip() != org:
        raise OrgScopeError(
            f"recurrence belongs to org {rec_org!r}, cannot retrieve under {org!r}"
        )

    cfg = config or RunbookRetrievalConfig.from_env()
    query = build_resolution_query(rec, redacted_texts=redacted_texts)
    if not query:
        # Nothing to search on — a predictable, genuine empty result (no noise,
        # no phantom query embedded). Distinct from "unavailable".
        return RunbookRetrievalResult(status=RETRIEVAL_OK, query="", candidates=())

    retrieve = retrieve_fn or _default_retrieve
    try:
        chunks = retrieve(
            org,
            query,
            k=cfg.candidate_limit,
            source_filter=cfg.source_systems,
            min_score=cfg.min_score,
            include_stale=cfg.include_stale,
        )
    except Exception:  # retrieval failure (e.g. store/DB down) — never break the run
        logger.warning("runbook retrieval failed for org %s; reporting unavailable", org, exc_info=True)
        return RunbookRetrievalResult(status=RETRIEVAL_UNAVAILABLE, query=query, candidates=())

    # Same exclusion as the citation library: the substrate's source_filter cannot
    # separate SharePoint page content from SharePoint file metadata (they share a
    # source system), so the split happens here, on the artifact id namespace.
    chunks = [
        c
        for c in (chunks or ())
        if is_runbook_content_artifact(
            getattr(c, "source_system", ""), getattr(c, "source_artifact", "")
        )
    ]

    candidates = tuple(RunbookCandidate.from_chunk(c) for c in (chunks or ()))
    if candidates:
        return RunbookRetrievalResult(status=RETRIEVAL_OK, query=query, candidates=candidates)

    # Empty result: the 1.8 API returns [] for BOTH a genuine miss AND a degraded
    # embedding provider. Probe the provider ONCE to tell them apart so T3 never
    # reads a documentation gap from an outage.
    probe = embedding_available_fn or _default_embedding_available
    try:
        available = probe(query)
    except Exception:  # a failing probe is itself an unavailable signal
        logger.warning("runbook retrieval: embedding probe failed for org %s", org, exc_info=True)
        available = False
    if not available:
        return RunbookRetrievalResult(status=RETRIEVAL_UNAVAILABLE, query=query, candidates=())
    return RunbookRetrievalResult(status=RETRIEVAL_OK, query=query, candidates=())


# ═════════════════════════════════════════════════════════════════════════════
# MSP-B5 T3 — deterministic scoring + threshold => PROPOSED match.
#
# Scores the T2 candidates for how strongly each corresponds to the recurrence's
# resolution pattern (retrieval similarity + structured agreement), selects the
# strongest with stable tie-breaking, and — only when it meets the CONFIGURED
# threshold — emits a RunbookMatch(origin='proposed', match_confidence=…). Below
# threshold: no match, never a stretch. A proposed match is visibly distinct from
# an observed (T1) or analyst-confirmed (T4) one: match_state/origin='proposed'
# and a numeric match_confidence. Retrieval PROPOSES; it never becomes fact here.
# ═════════════════════════════════════════════════════════════════════════════

# Scoring knobs are CONFIG, not code (task requirement / AC3): the match threshold
# lives here so it can be calibrated per pack/org without touching the matching
# function. Defaults are conservative; overridable via env or an injected config
# (the org/pack-configuration seam). The threshold sits at/above the T2 retrieval
# floor so a candidate that barely cleared retrieval is not auto-proposed.
DEFAULT_MATCH_THRESHOLD = 0.75
DEFAULT_STRUCTURED_WEIGHT = 0.15
MATCH_THRESHOLD_ENV = "MSP_B5_RUNBOOK_MATCH_THRESHOLD"
STRUCTURED_WEIGHT_ENV = "MSP_B5_RUNBOOK_STRUCTURED_WEIGHT"

# Score is rounded to this many places so float noise can never flip a stable
# ordering or a threshold comparison between otherwise-identical runs.
_SCORE_PRECISION = 6
_WORD_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class RunbookScoringConfig:
    """Tunable proposed-match scoring — thresholds are config, not code (AC3).

    ``match_threshold`` is the minimum combined score for a candidate to be
    PROPOSED; below it, no match is emitted. ``structured_agreement_weight`` is how
    much structured agreement (resolution-pattern terms present in the candidate)
    can lift a candidate above its bare retrieval similarity. Resolve per pack/org
    by constructing this with the calibrated values; ``from_env`` is the dev/default
    path.
    """

    match_threshold: float = DEFAULT_MATCH_THRESHOLD
    structured_agreement_weight: float = DEFAULT_STRUCTURED_WEIGHT

    @classmethod
    def from_env(cls) -> "RunbookScoringConfig":
        return cls(
            match_threshold=_env_float(MATCH_THRESHOLD_ENV, DEFAULT_MATCH_THRESHOLD),
            structured_agreement_weight=_env_float(
                STRUCTURED_WEIGHT_ENV, DEFAULT_STRUCTURED_WEIGHT
            ),
        )


def _word_set(text: Any) -> frozenset:
    """Lower-cased word tokens of ``text`` (alphanumeric/underscore runs)."""
    return frozenset(
        tok for tok in _WORD_RE.findall(str(text or "").casefold()) if len(tok) > 1
    )


def _structured_agreement(rec: "RecurrenceRecord", candidate: "RunbookCandidate") -> float:
    """Fraction of the recurrence's resolution-pattern terms present in the
    candidate content — a deterministic bag-of-words agreement in ``[0, 1]``.

    Zero when the recurrence carries no usable structured pattern (no bonus, never
    a penalty). Uses only the privacy-safe structured fields (never free text).
    """
    pattern_words = set()
    for value in _query_fields(rec).values():
        pattern_words |= _word_set(value)
    if not pattern_words:
        return 0.0
    content_words = _word_set(getattr(candidate, "content", ""))
    matched = len(pattern_words & content_words)
    return matched / len(pattern_words)


def score_candidate(
    rec: "RecurrenceRecord",
    candidate: "RunbookCandidate",
    config: Optional[RunbookScoringConfig] = None,
) -> float:
    """Deterministically score how strongly ``candidate`` matches the recurrence.

    The score PRESERVES the retrieval similarity as its anchor and lifts it by the
    structured agreement (bounded by ``structured_agreement_weight``), so a
    candidate never scores below its own similarity and structured agreement can
    only strengthen — never fabricate — a match. Clamped to ``[0, 1]`` and rounded
    for stable comparisons. Pure function of ``(rec, candidate, config)``.
    """
    cfg = config or RunbookScoringConfig.from_env()
    similarity = float(getattr(candidate, "similarity", 0.0) or 0.0)
    similarity = max(0.0, min(1.0, similarity))
    agreement = _structured_agreement(rec, candidate)
    score = similarity + cfg.structured_agreement_weight * agreement
    return round(max(0.0, min(1.0, score)), _SCORE_PRECISION)


def _candidate_summary(candidate: "RunbookCandidate") -> Dict[str, Any]:
    """The matched-runbook descriptor for a PROPOSED match (provenance only)."""
    return {
        "source_system": candidate.source_system,
        "source_artifact": candidate.source_artifact,
        "title": None,
        "url": None,
        "identifiers": [],
    }


def _candidate_evidence(candidate: "RunbookCandidate") -> Dict[str, Any]:
    """An OBSERVED (retrieved) evidence pointer preserving the retrieval-result ids
    that explain WHY the candidate was proposed (chunk id, retrieval-result id, and
    the retrieval similarity as confidence)."""
    return EvidencePointer.retrieved(
        source_system=candidate.source_system,
        source_artifact=candidate.source_artifact,
        chunk_id=candidate.chunk_id,
        retrieval_result_id=candidate.retrieval_result_id,
        source_timestamp=candidate.source_timestamp or None,
        confidence=candidate.similarity,
        source_artifact_type="record_id",
    ).to_dict()


def propose_runbook_match(
    org_id: str,
    rec: "RecurrenceRecord",
    candidates: Sequence["RunbookCandidate"],
    *,
    config: Optional[RunbookScoringConfig] = None,
) -> Optional[RunbookMatch]:
    """Score T2 candidates and PROPOSE the strongest, iff it meets the threshold.

    Deterministic and org-scoped. Scores every candidate, selects the strongest
    with a STABLE ordering (highest score, then ``source_artifact`` then
    ``chunk_id`` so equal scores resolve identically across runs), and returns a
    :class:`RunbookMatch` with ``origin='proposed'`` / ``match_state='proposed'``
    and a numeric ``match_confidence`` when it meets the configured threshold.
    Below threshold: ``None`` — the weaker candidate is never selected, the
    threshold is never auto-lowered, and a low-quality result is never dressed up
    as a possible match. A proposed match is visibly distinct from an observed or
    confirmed one and never becomes established fact here (AC2).
    """
    org = _require_org(org_id)
    rec_org = getattr(rec, "org_id", None)
    if rec_org and str(rec_org).strip() != org:
        raise OrgScopeError(
            f"recurrence belongs to org {rec_org!r}, cannot propose under {org!r}"
        )
    if not candidates:
        return None

    cfg = config or RunbookScoringConfig.from_env()
    scored = [(score_candidate(rec, c, cfg), c) for c in candidates]
    # Stable ordering: strongest score first, then stable provenance tie-breakers
    # so repeated runs over an equal-scored set always pick the same candidate.
    scored.sort(key=lambda sc: (-sc[0], sc[1].source_artifact, sc[1].chunk_id))
    best_score, best = scored[0]

    if best_score < cfg.match_threshold:
        return None  # below threshold => no match, never a stretch

    return RunbookMatch(
        org_id=org,
        recurrence_id=str(getattr(rec, "record_id", "") or ""),
        match_state=MATCH_PROPOSED,
        origin=MATCH_PROPOSED,
        runbook=_candidate_summary(best),
        runbook_evidence=_candidate_evidence(best),
        citing_incident_evidence=(),  # a proposal has no citing incident (uncited)
        cited_references=(),
        match_confidence=best_score,
    )
