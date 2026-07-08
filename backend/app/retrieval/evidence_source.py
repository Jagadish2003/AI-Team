"""Context-assembly evidence source backed by ``retrieve()`` (R18-B1 T6).

The bridge between the 1.8 retrieval substrate and the R16-B2 context assembly
foundation — the moment the ``evidence_source`` hook that 1.6 deliberately left
``None``-capable pays off (Section 2). :func:`retrieval_evidence_source` builds
the real evidence source: a callable that, given an opportunity, calls the
platform ``retrieve()`` API and returns retrieved chunks in the shape
``assemble_context`` already understands. No caller changes anywhere else.

The architecture's standing pattern applies in full — **retrieval PROPOSES
candidates; the assembler DECIDES what enters context**:

* This source never feeds enrichment directly. Its output is only ever consumed
  through ``assemble_context(evidence_source=...)``, where the assembler's
  deterministic rules — confidence floor, observed-first budget filling, hard
  caps (``max_evidence_chunks``), and the selection log — apply to retrieved
  chunks exactly as they apply to graph context (AC6). Semantic retrieval is
  probabilistic; context selection stays predictable.
* The source deliberately does NOT pre-filter by the policy's
  ``confidence_floor``: weak matches are proposed and excluded BY THE ASSEMBLER,
  so every below-floor exclusion is recorded in the selection log rather than
  silently disappearing at the retrieval layer. Explainability beats a marginally
  smaller candidate list. (A caller-supplied ``min_score`` still passes through
  to ``retrieve()`` for callers that want a hard retrieval-side floor.)
* It proposes MORE candidates than the evidence budget
  (:data:`PROPOSAL_FACTOR` × ``max_evidence_chunks``) so the assembler's floor
  and ranking have real work to do — proposing exactly the budget would make the
  cap a no-op and the floor a silent hole in the package.

Each proposed chunk maps the retrieval similarity onto the assembler's
``confidence`` axis and carries origin ``observed`` (retrieved content was seen
directly in a source — the same stance as ``EvidencePointer.retrieved``, T4).
The payload keeps the full provenance including the populated EvidencePointer
fields — ``chunk_id`` and ``retrieval_result_id`` — so a selected chunk enters
the ``ContextPackage`` pointer-complete (AC5).

Advisory by contract: a failing source yields no evidence, never an error —
same posture as the hook itself (``_resolve_evidence``) and the rest of the
substrate. Query text derivation, the ``retrieve()`` call, and the mapping are
all guarded; the worst outcome of any failure is an evidence-free package.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, List, Optional

try:  # Repo-root import style (tests add both roots to sys.path).
    from backend.app.context_assembly import DEFAULT_MAX_EVIDENCE_CHUNKS
    from backend.app.provenance import OBSERVED
    from backend.app.retrieval.api import RetrievedChunk, retrieve
except ModuleNotFoundError:  # Runtime inside backend/ where app is top-level.
    from app.context_assembly import DEFAULT_MAX_EVIDENCE_CHUNKS
    from app.provenance import OBSERVED
    from app.retrieval.api import RetrievedChunk, retrieve

logger = logging.getLogger(__name__)

# How many candidates to propose per unit of evidence budget. Oversampling gives
# the assembler slack: when some candidates fall below its confidence floor, the
# budget can still fill from the remainder — and the floor/cap decisions all land
# in the selection log instead of being pre-empted here.
PROPOSAL_FACTOR = 2

# Upper bound on the derived query text. Retrieval embeds the query through the
# gateway; an unbounded opportunity description must not turn into an unbounded
# embedding call.
MAX_QUERY_CHARS = 2000

# Opportunity fields consulted (in order) to derive the retrieval query. An
# explicit ``query_text`` always wins; otherwise the title-like and body-like
# fields are combined. Read tolerantly from dicts or objects.
QUERY_FIELDS = (
    "query_text",
    "title",
    "name",
    "summary",
    "description",
    "friction_summary",
)

__all__ = [
    "retrieval_evidence_source",
    "opportunity_query_text",
    "retrieved_chunk_to_evidence",
    "PROPOSAL_FACTOR",
    "MAX_QUERY_CHARS",
    "QUERY_FIELDS",
]


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from an object attribute or a mapping key, else default."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def opportunity_query_text(opportunity: Any) -> Optional[str]:
    """Derive the retrieval query for an opportunity, or ``None`` when it has none.

    An explicit ``query_text`` field is used verbatim. Otherwise the known
    text-bearing fields (:data:`QUERY_FIELDS`) are combined in a fixed order —
    deterministic input, deterministic query. De-duplicated so a same-valued
    ``title``/``name`` pair is not embedded twice, and bounded at
    :data:`MAX_QUERY_CHARS`. An opportunity with no usable text (e.g. the bare
    ``{"run_id": ...}`` the run-level graph bridge passes today) yields ``None``
    — the source then proposes nothing rather than querying on noise.
    """
    explicit = _get(opportunity, "query_text")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()[:MAX_QUERY_CHARS]

    parts: List[str] = []
    for field_name in QUERY_FIELDS[1:]:
        value = _get(opportunity, field_name)
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in parts:
            parts.append(text)
    if not parts:
        return None
    return "\n".join(parts)[:MAX_QUERY_CHARS]


def retrieved_chunk_to_evidence(chunk: RetrievedChunk) -> dict:
    """Map one :class:`RetrievedChunk` into the assembler's evidence-chunk shape.

    The keys the assembler's evidence adapter reads directly:

    * ``chunk_id``          -> the candidate's stable id (and tiebreaker)
    * ``origin``            -> ``observed`` — retrieved content was seen directly
                               in a source (same stance as
                               ``EvidencePointer.retrieved``); it earns observed
                               precedence under observed-first filling
    * ``confidence``        -> the retrieval similarity, so the assembler's
                               confidence floor and ranking act on retrieval
                               quality
    * ``source_timestamp``  -> feeds the assembler's freshness scoring

    The rest is provenance payload carried verbatim into the
    ``ContextPackage`` when the chunk is selected: content, source system and
    artifact, similarity under its own name, and the fully populated
    ``evidence_pointer`` — ``chunk_id`` + ``retrieval_result_id`` filled per AC5.
    """
    return {
        "chunk_id": chunk.chunk_id,
        "origin": OBSERVED,
        "confidence": chunk.similarity,
        "source_timestamp": chunk.source_timestamp or None,
        "content": chunk.content,
        "similarity": chunk.similarity,
        "source_system": chunk.source_system,
        "source_artifact": chunk.source_artifact,
        "retrieval_result_id": chunk.retrieval_result_id,
        "evidence_pointer": chunk.to_evidence_pointer().to_dict(),
    }


def retrieval_evidence_source(
    org_id: str,
    *,
    source_filter: Optional[List[str]] = None,
    min_score: Optional[float] = None,
    k: Optional[int] = None,
) -> Callable[[Any, Any], List[dict]]:
    """Build the real ``evidence_source`` for ``assemble_context`` (T6).

    Returns a callable with the hook's ``(opportunity, policy)`` signature. Per
    call it derives the opportunity's query text, invokes the platform
    ``retrieve()`` API scoped to ``org_id``, and returns the ranked chunks in the
    assembler's evidence shape — candidates only; selection belongs to the
    assembler (AC6).

    ``source_filter`` / ``min_score`` pass through to ``retrieve()`` for callers
    that want source-scoped or retrieval-side-floored proposals. ``k`` overrides
    the proposal count; by default the source proposes
    ``PROPOSAL_FACTOR × policy.max_evidence_chunks`` (falling back to the default
    evidence budget when no policy arrives) so the assembler's floor, ranking,
    and cap all have real candidates to decide over.

    The returned source never raises: no org, no query text, a degraded
    gateway, or any unexpected error all yield ``[]`` — an evidence-free package
    is a normal outcome, never a broken assembly.
    """

    def _source(opportunity: Any, policy: Any = None) -> List[dict]:
        try:
            if not org_id:
                return []
            query_text = opportunity_query_text(opportunity)
            if not query_text:
                return []

            if k is not None:
                propose_k = k
            else:
                budget = _get(policy, "max_evidence_chunks", None)
                try:
                    budget = int(budget) if budget is not None else DEFAULT_MAX_EVIDENCE_CHUNKS
                except (TypeError, ValueError):
                    budget = DEFAULT_MAX_EVIDENCE_CHUNKS
                propose_k = max(1, budget) * PROPOSAL_FACTOR

            chunks = retrieve(
                org_id,
                query_text,
                k=propose_k,
                source_filter=source_filter,
                min_score=min_score,
            )
            return [retrieved_chunk_to_evidence(c) for c in chunks]
        except Exception:  # noqa: BLE001 — evidence is advisory; never break assembly
            logger.warning(
                "retrieval evidence source failed (advisory, org=%s); "
                "proposing no evidence",
                org_id,
                exc_info=True,
            )
            return []

    return _source
