"""HP-2.4 — embedding-dimension consistency check at startup.

The residual from the pack's item #1. The vector column accepts any dimension by
design and **that is not changed here**: what was missing is any check that the
CONFIGURED embedding model emits the dimension already stored under the active
model stamp. Until now a mismatch surfaced as a runtime storage error on the first
write — long after startup, on whichever run happened to embed something.

What counts as a mismatch
-------------------------
Only the ACTIVE ``(embedding_model, embedding_model_version)`` stamp is examined.
Vectors under a different stamp are never compared against the active ones
(``store.search`` filters by the pair), so they are legitimately allowed to have a
different dimension — those are the R18-B2 T5 managed backfill's work, not a
fault.

Which makes one thing worth stating plainly, because it changes the remedy: for
the active stamp the backfill is **not** the fix. It re-embeds vectors stamped by
a NON-active model, so rows already carrying the active stamp are invisible to it.
The actionable remedies are to repin the model back, or to move the model VERSION
so the existing rows become non-active and the backfill can reach them. The
refusal message says exactly that rather than pointing at a tool that would run
and change nothing.

Fails closed on a real conflict, silent on absence of evidence
-------------------------------------------------------------
* No declared dimension for the configured model → **skip**. An unknown model is
  not a mismatch (see ``embedding_dimensions``).
* No embedded vectors under the active stamp → **skip**. That is the normal
  first-run state, and a fresh deployment must boot silently.
* Store unreadable, table absent, or ``vector_dims`` unavailable → **skip**, with
  a warning. A version record must never be the reason a deployment cannot start,
  and a startup check that cannot read its evidence has not found a fault.
* A stored dimension that differs from the declared one → **refuse**, in every
  profile. Unlike HP-2.3's reachability probe this is not environmental: the
  configured model provably cannot write into this index, so the deployment is
  misconfigured whoever operates it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

#: Reasons a check was skipped rather than passed. Skipped is NOT passed: the two
#: are reported distinctly so "we could not look" never reads as "it is fine".
SKIP_NO_ACTIVE_MODEL = "no_active_embedding_model"
SKIP_NO_DECLARED_DIMENSION = "no_declared_dimension"
SKIP_NO_STORED_VECTORS = "no_stored_vectors"
SKIP_STORE_UNREADABLE = "store_unreadable"

OUTCOME_OK = "ok"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_SKIPPED = "skipped"


class EmbeddingDimensionMismatch(RuntimeError):
    """The configured embedding model cannot write into the existing index.

    Raised at startup, in every deployment profile. A ``RuntimeError`` alongside
    HP-2.3's ``ProviderUnreachable`` rather than a ``ValueError``: each individual
    setting is well-formed; it is the combination with what is already stored that
    cannot work.
    """


@dataclass(frozen=True)
class DimensionCheck:
    """The outcome of one consistency check. Carries no vector content."""

    outcome: str
    model_identity: str = ""
    model_version: str = ""
    declared_dimensions: Optional[int] = None
    stored: List[Dict[str, Any]] = field(default_factory=list)
    skip_reason: str = ""
    detail: str = ""

    @property
    def is_mismatch(self) -> bool:
        return self.outcome == OUTCOME_MISMATCH

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "modelIdentity": self.model_identity,
            "modelVersion": self.model_version,
            "declaredDimensions": self.declared_dimensions,
            "stored": list(self.stored),
            "skipReason": self.skip_reason,
            "detail": self.detail,
        }


def _skip(reason: str, detail: str, **kw: Any) -> DimensionCheck:
    return DimensionCheck(
        outcome=OUTCOME_SKIPPED, skip_reason=reason, detail=detail, **kw
    )


def check_embedding_dimensions(
    *,
    active_model: Optional[Callable[[], tuple]] = None,
    declared: Optional[Callable[[str], Optional[int]]] = None,
    stored_dimensions: Optional[Callable[[str, str], List[Dict[str, Any]]]] = None,
) -> DimensionCheck:
    """Resolve the check. **Never raises** — the outcome is the return value.

    The three collaborators are injectable so the rules are testable without a
    provider, a database, or a pgvector extension.
    """
    if active_model is None:
        from app.retrieval.embedder import active_embedding_model as active_model
    if declared is None:
        from app.retrieval.embedding_dimensions import declared_dimension as declared
    if stored_dimensions is None:
        from app.retrieval.store import stored_embedding_dimensions as stored_dimensions

    try:
        identity, version = active_model()
    except Exception:  # noqa: BLE001 — identity lookup must not break startup
        logger.warning(
            "dimension guard: could not resolve the active embedding model",
            exc_info=True,
        )
        return _skip(
            SKIP_NO_ACTIVE_MODEL,
            "The active embedding model could not be resolved, so dimension "
            "consistency was not checked.",
        )

    identity = (identity or "").strip()
    if not identity:
        # The hosted provider stamps no model (it has no embeddings endpoint).
        return _skip(
            SKIP_NO_ACTIVE_MODEL,
            "No embedding model is active, so there is no dimension to check.",
        )

    declared_dims = declared(identity)
    if declared_dims is None:
        return _skip(
            SKIP_NO_DECLARED_DIMENSION,
            f"Embedding model '{identity}' has no declared output dimension in "
            "app/retrieval/embedding_dimensions.py, so dimension consistency "
            "cannot be checked for it. Add it there to enable this check.",
            model_identity=identity,
            model_version=version or "",
        )

    try:
        stored = stored_dimensions(identity, version or "")
    except Exception:  # noqa: BLE001 — an unreadable store is not a fault found
        logger.warning(
            "dimension guard: could not read stored embedding dimensions for "
            "model %s; dimension consistency not checked",
            identity,
            exc_info=True,
        )
        return _skip(
            SKIP_STORE_UNREADABLE,
            "The retrieval store could not be read, so dimension consistency was "
            "not checked. This is not evidence that the dimensions agree.",
            model_identity=identity,
            model_version=version or "",
            declared_dimensions=declared_dims,
        )

    if not stored:
        # The normal first-run state: nothing embedded under this stamp yet.
        return _skip(
            SKIP_NO_STORED_VECTORS,
            f"No vectors are stored under embedding model '{identity}' yet, so "
            "there is nothing to be inconsistent with.",
            model_identity=identity,
            model_version=version or "",
            declared_dimensions=declared_dims,
        )

    conflicting = [s for s in stored if int(s.get("dimensions", 0)) != declared_dims]
    if not conflicting:
        return DimensionCheck(
            outcome=OUTCOME_OK,
            model_identity=identity,
            model_version=version or "",
            declared_dimensions=declared_dims,
            stored=list(stored),
            detail=(
                f"Embedding model '{identity}' emits {declared_dims} dimensions, "
                f"matching every vector stored under it."
            ),
        )

    found = ", ".join(
        f"{int(s['dimensions'])} dimensions ({int(s['chunks'])} chunks across "
        f"{int(s['orgs'])} org(s))"
        for s in conflicting
    )
    return DimensionCheck(
        outcome=OUTCOME_MISMATCH,
        model_identity=identity,
        model_version=version or "",
        declared_dimensions=declared_dims,
        stored=list(stored),
        detail=(
            f"Configured embedding model '{identity}' (version "
            f"'{version or 'unset'}') emits {declared_dims} dimensions, but the "
            f"retrieval index already holds {found} stamped with that same model "
            f"identity and version. Vectors of different dimensions cannot be "
            f"compared, so the next embedding write into this index will fail."
        ),
    )


def validate_embedding_dimensions(**kw: Any) -> DimensionCheck:
    """Startup entry point. Logs the outcome; raises on a real mismatch.

    Returns the check so a caller can log or surface the resolved state.
    """
    check = check_embedding_dimensions(**kw)

    if check.outcome == OUTCOME_OK:
        logger.info("embedding dimensions: %s", check.detail)
        return check

    if check.outcome == OUTCOME_SKIPPED:
        # A first run and an unknown model are ordinary; an unreadable store is
        # worth a warning because it means the check silently did not happen.
        if check.skip_reason == SKIP_STORE_UNREADABLE:
            logger.warning("embedding dimensions: %s", check.detail)
        else:
            logger.info("embedding dimensions: %s", check.detail)
        return check

    raise EmbeddingDimensionMismatch(
        f"{check.detail} "
        "Remedies, in order of preference: (1) repin the embedding model to the "
        "one that produced the stored vectors; or (2) change the embedding model "
        "VERSION so the existing rows are no longer stamped with the active pair, "
        "which lets the managed backfill re-embed them onto the new model. Note "
        "the backfill alone will NOT fix this — it only re-embeds vectors stamped "
        "by a NON-active model, and these already carry the active stamp. "
        "Dropping the affected chunks and re-ingesting is the last resort."
    )


__all__ = [
    "OUTCOME_MISMATCH",
    "OUTCOME_OK",
    "OUTCOME_SKIPPED",
    "SKIP_NO_ACTIVE_MODEL",
    "SKIP_NO_DECLARED_DIMENSION",
    "SKIP_NO_STORED_VECTORS",
    "SKIP_STORE_UNREADABLE",
    "DimensionCheck",
    "EmbeddingDimensionMismatch",
    "check_embedding_dimensions",
    "validate_embedding_dimensions",
]
