"""Ask the retrieval substrate whether a topic is DOCUMENTED (COR-04's producer).

COR-04 has been in the corroboration registry since ENT-2 and has never fired in a
real run. Its check reads ``run_data["confluence"]["covenant_documentation_present"]``
(falling back to ``documentation_gap``), and nothing anywhere produced either key —
so the rule evaluated to False on every run while reading, to anyone browsing the
registry, as a shipped capability.

This module produces that fact, and the whole design turns on one distinction:

    "we searched the documentation corpus and found nothing"      -> a real gap
    "we could not search the documentation corpus"                -> NOT a gap

Those are the same empty result set and opposite conclusions. Conflating them is
how a platform tells a customer "your covenant review process is undocumented"
because an embedding provider was misconfigured — and COR-04 ELEVATES on absence,
so the mistake would be promoted to HIGH confidence. MSP-B5 already made this
distinction for runbooks (``RETRIEVAL_OK`` vs ``RETRIEVAL_UNAVAILABLE``); this
module reuses that vocabulary rather than inventing a second one.

Three-valued by construction — there is deliberately no boolean return:

    DOCUMENTED  the corpus contains documentation about this topic
    ABSENT      the corpus was searched and contains none
    UNKNOWN     the corpus could not be searched, or is not indexed at all

UNKNOWN is what the caller must translate into "COR-04 does not fire", never into
"documentation is missing".

Scope note: this asks about a TOPIC, not about a specific page. It queries the same
source systems MSP-B5 treats as the documentation corpus, and — like the runbook
library — excludes SharePoint driveItem METADATA, which is a filename card rather
than a documented procedure (``runbook_match.is_runbook_content_artifact``).

Pure apart from the injected reader: ``retrieve_fn`` is a seam so this is testable
with no substrate, and the default import is lazy so importing this module never
pulls the app package into an offline connector process.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: The three answers. A caller that collapses these to a boolean has reintroduced
#: the bug this module exists to prevent.
DOCUMENTED = "documented"
ABSENT = "absent"
UNKNOWN = "unknown"

#: The documentation corpus — the same source systems MSP-B5's runbook library
#: reads. Imported there rather than restated so the two cannot drift.
def _corpus_source_systems() -> Tuple[str, ...]:
    from discovery.detectors.runbook_match import RUNBOOK_SOURCE_SYSTEMS

    return tuple(RUNBOOK_SOURCE_SYSTEMS)


#: Topic -> the query put to the substrate. A topic is declared here rather than
#: composed at the call site so the same question is asked identically on every
#: run: a query that varies run to run makes "documented last week, absent today"
#: unexplainable.
COVENANT_REVIEW = "covenant_review_process"

TOPIC_QUERIES: Dict[str, str] = {
    COVENANT_REVIEW: "covenant review process monitoring compliance breach",
}

#: Retrieval must clear this to count as documentation. Deliberately the same
#: default as MSP-B5's runbook matching: a loose threshold turns any page that
#: mentions a word into "documented", which would suppress real gaps.
DEFAULT_MIN_SCORE = 0.7

#: How many chunks to ask for. Only presence matters, so this is small.
DEFAULT_K = 5


@dataclass(frozen=True)
class DocumentationProbe:
    """The answer, with enough context to explain itself later."""

    topic: str
    status: str                      # DOCUMENTED | ABSENT | UNKNOWN
    query: str = ""
    match_count: int = 0
    top_source_system: Optional[str] = None
    top_source_artifact: Optional[str] = None
    detail: Optional[str] = None     # why UNKNOWN, when it is

    @property
    def is_gap(self) -> bool:
        """True ONLY for a searched-and-absent result. UNKNOWN is never a gap."""
        return self.status == ABSENT

    @property
    def is_documented(self) -> bool:
        return self.status == DOCUMENTED

    @property
    def conclusive(self) -> bool:
        """True when the corpus was actually searched, either way."""
        return self.status in (DOCUMENTED, ABSENT)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "status": self.status,
            "query": self.query,
            "matchCount": self.match_count,
            "topSourceSystem": self.top_source_system,
            "topSourceArtifact": self.top_source_artifact,
            "detail": self.detail,
        }


def _default_retrieve(org_id: str, query: str, **kwargs: Any):
    from app.retrieval.api import retrieve

    return retrieve(org_id, query, **kwargs)


def probe_documentation(
    org_id: str,
    topic: str,
    *,
    retrieve_fn: Optional[Callable[..., Any]] = None,
    min_score: float = DEFAULT_MIN_SCORE,
    k: int = DEFAULT_K,
) -> DocumentationProbe:
    """Return whether ``topic`` is documented for this org.

    Never raises: every failure resolves to :data:`UNKNOWN` with a reason, because
    the one thing this must not do is report a retrieval problem as missing
    documentation.
    """
    org = str(org_id or "").strip()
    if not org:
        return DocumentationProbe(topic=topic, status=UNKNOWN, detail="no org id")

    query = TOPIC_QUERIES.get(topic, "")
    if not query:
        # An undeclared topic is a programming error, but reporting it as ABSENT
        # would elevate a finding on the strength of a typo.
        return DocumentationProbe(
            topic=topic, status=UNKNOWN, detail=f"no query declared for topic {topic!r}"
        )

    retrieve = retrieve_fn or _default_retrieve
    try:
        chunks = retrieve(
            org,
            query,
            k=k,
            source_filter=list(_corpus_source_systems()),
            min_score=min_score,
            include_stale=False,
        )
    except Exception as exc:  # noqa: BLE001 — a probe must never break a run
        logger.warning(
            "documentation probe unavailable for org=%s topic=%s; reporting UNKNOWN "
            "(NOT a documentation gap): [%s]",
            org, topic, type(exc).__name__,
        )
        return DocumentationProbe(
            topic=topic, status=UNKNOWN, query=query, detail=type(exc).__name__
        )

    # Exclude SharePoint file metadata: "Name: covenants.xlsx" is a filename, not a
    # documented process, and treating it as documentation would silently suppress a
    # real gap.
    try:
        from discovery.detectors.runbook_match import is_runbook_content_artifact

        chunks = [
            c for c in (chunks or ())
            if is_runbook_content_artifact(
                getattr(c, "source_system", ""), getattr(c, "source_artifact", "")
            )
        ]
    except Exception:  # pragma: no cover - filter is best-effort, never fatal
        chunks = list(chunks or ())

    if not chunks:
        return DocumentationProbe(topic=topic, status=ABSENT, query=query)

    top = chunks[0]
    return DocumentationProbe(
        topic=topic,
        status=DOCUMENTED,
        query=query,
        match_count=len(chunks),
        top_source_system=str(getattr(top, "source_system", "") or "") or None,
        top_source_artifact=str(getattr(top, "source_artifact", "") or "") or None,
    )


def apply_to_confluence_block(
    block: Dict[str, Any], probe: DocumentationProbe
) -> Dict[str, Any]:
    """Stamp a probe onto the Confluence corroboration block for COR-04.

    Writes ``covenant_documentation_present`` ONLY when the probe is conclusive.
    An UNKNOWN probe leaves the key absent, so ``check_cor04_confluence_doc_gap``
    reads ``None`` and the rule does not fire — the correct outcome for "we could
    not check", and the reason this helper exists instead of a boolean at the call
    site, where ``False`` is the easy thing to write and the wrong thing to mean.

    The full probe always travels as ``documentation_probe`` so a reviewer can see
    what was asked and what came back, including for an UNKNOWN.
    """
    out = dict(block or {})
    out["documentation_probe"] = probe.to_dict()
    if probe.conclusive and probe.topic == COVENANT_REVIEW:
        out["covenant_documentation_present"] = probe.is_documented
    return out
