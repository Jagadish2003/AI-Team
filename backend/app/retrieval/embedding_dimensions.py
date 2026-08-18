"""HP-2.4 — declared output dimensions for the supported embedding models.

The pgvector column is an unqualified ``vector`` with NO fixed dimension, and that
is deliberate (see ``database/models/retrieval.py``): the embedding model is a
per-deployment decision and vectors from different models are never compared
(R18-B1 AC8), so the store holds whatever dimension the active model emits.
**Nothing here changes that**, and this module must never be read as a constraint
on which models the store can hold.

What it exists for is the one thing the flexible column cannot do: tell an
operator at STARTUP that the model they have configured does not emit the
dimension already stored under the active model stamp. Without a declared
dimension there is nothing to compare against until the first write fails.

Why a table rather than measuring
---------------------------------
The only way to observe a model's real output dimension is to embed something,
which costs money, needs a valid credential and a reachable endpoint, and would
turn startup into a billable network call. A declared table is the cheap answer.

Its honest limitation, stated rather than hidden: a model absent from this table
has NO declared dimension, so the HP-2.4 check cannot run for it and is skipped.
An unknown model is not a mismatch. Adding a model here is a one-line change and
does not require a migration or a re-embed.

Each entry records a ``basis`` for the number, following the convention used by
``discovery/signals/ops_calibration.py`` and ``app/scale_envelope.py``:

``published``
    The model publisher documents this dimension.
``measured``
    Observed from vectors this platform actually produced.

A note on truncation: the ``text-embedding-3-*`` family accepts a ``dimensions``
request parameter that shortens the output. Neither the in-boundary nor the
customer-tenant adapter sends one (verified — no adapter references
``dimensions``), so the DEFAULT dimension recorded here is what the platform
receives. If an adapter ever starts sending that parameter, these numbers stop
being authoritative for that family and this note is the reason to revisit them.

This module names MODELS, never a provider, an endpoint, or an SDK — the R16-D1
no-bypass rule keeps those inside ``app/model_gateway/``, and the scanners that
enforce it sweep this file too. A model name is a property of the vector space,
which is what this table is about; where it is served from is the gateway's
business.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)

BASIS_PUBLISHED = "published"
BASIS_MEASURED = "measured"


@dataclass(frozen=True)
class EmbeddingModelDimension:
    """A model's declared output dimension, and where the number came from."""

    model: str
    dimensions: int
    basis: str
    note: str = ""


def _entry(model: str, dimensions: int, basis: str, note: str = "") -> EmbeddingModelDimension:
    return EmbeddingModelDimension(
        model=model, dimensions=dimensions, basis=basis, note=note
    )


#: Supported self-hosted and managed embedding models, keyed by NORMALISED name.
#: Extend by adding a row — no migration, no re-embed, no schema change.
MODEL_DIMENSIONS: Dict[str, EmbeddingModelDimension] = {
    # Self-hosted, served in-boundary by Ollama / vLLM / any compatible server.
    # These four are the set HP-2.7's on-prem templates document.
    "nomic-embed-text": _entry("nomic-embed-text", 768, BASIS_PUBLISHED),
    "mxbai-embed-large": _entry("mxbai-embed-large", 1024, BASIS_PUBLISHED),
    "all-minilm-l6-v2": _entry("all-MiniLM-L6-v2", 384, BASIS_PUBLISHED),
    "bge-large-en-v1.5": _entry("bge-large-en-v1.5", 1024, BASIS_PUBLISHED),
    "bge-large-en": _entry("bge-large-en", 1024, BASIS_PUBLISHED),
    "bge-base-en-v1.5": _entry("bge-base-en-v1.5", 768, BASIS_PUBLISHED),
    "bge-small-en-v1.5": _entry("bge-small-en-v1.5", 384, BASIS_PUBLISHED),
    # Managed, served through the customer's own model service or an in-boundary
    # proxy. text-embedding-3-small is the shipped default configuration —
    # backend/.env.template pins it and records 1536-dim vectors.
    "text-embedding-3-small": _entry(
        "text-embedding-3-small",
        1536,
        BASIS_MEASURED,
        note="the shipped configuration; 1536 is recorded in .env.template",
    ),
    "text-embedding-3-large": _entry("text-embedding-3-large", 3072, BASIS_PUBLISHED),
    "text-embedding-ada-002": _entry("text-embedding-ada-002", 1536, BASIS_PUBLISHED),
}


def normalise_model_name(name: str) -> str:
    """Fold a model name to its table key.

    Case is folded and surrounding whitespace trimmed because a model name is
    typed into a ``.env`` by hand. A trailing Ollama tag (``:latest``, ``:v1.5``)
    is stripped: ``nomic-embed-text`` and ``nomic-embed-text:latest`` are the same
    model and the same vector space, so treating them as different would make the
    check silently skip a model it knows.
    """
    cleaned = (name or "").strip().lower()
    if "/" in cleaned:
        # An Ollama/HF namespace prefix ("library/nomic-embed-text").
        cleaned = cleaned.rsplit("/", 1)[-1]
    if ":" in cleaned:
        cleaned = cleaned.split(":", 1)[0]
    return cleaned


def model_from_identity(identity: str) -> str:
    """Extract the model name from a stamped ``embedding_model`` identity.

    The substrate stamps ``"{provider}:{model}"`` (``in_boundary:nomic-embed-text``,
    ``customer_tenant:text-embedding-3-small``). A bare identity with no colon —
    the ``hosted`` provider's ``"hosted"``, or a provider that declares no model —
    has no model component and yields ``""``.

    The customer-tenant provider has a documented FALLBACK form that stamps the
    deployment ALIAS instead of the model when ``CUSTOMER_TENANT_EMBEDDING_MODEL``
    is undeclared. An alias is operator-chosen and will not be in the table, so the
    check skips — which is the honest outcome: we genuinely do not know which model
    is behind an alias.
    """
    raw = (identity or "").strip()
    if ":" not in raw:
        return ""
    return raw.split(":", 1)[1]


def declared_dimension(model_or_identity: str) -> Optional[int]:
    """The declared output dimension, or ``None`` when it is not known.

    Accepts either a bare model name (``nomic-embed-text``) or a stamped identity
    (``in_boundary:nomic-embed-text``). ``None`` means "no declaration", never
    "zero" — a caller must treat it as "cannot check", not as a mismatch.
    """
    candidate = (model_or_identity or "").strip()
    if not candidate:
        return None

    entry = MODEL_DIMENSIONS.get(normalise_model_name(candidate))
    if entry is not None:
        return entry.dimensions

    # Try again as a stamped identity ("provider:model").
    model = model_from_identity(candidate)
    if model:
        entry = MODEL_DIMENSIONS.get(normalise_model_name(model))
        if entry is not None:
            return entry.dimensions
    return None


def supported_models() -> Dict[str, EmbeddingModelDimension]:
    """The table, for documentation and for the HP-2.7 on-prem templates."""
    return dict(MODEL_DIMENSIONS)


__all__ = [
    "BASIS_MEASURED",
    "BASIS_PUBLISHED",
    "EmbeddingModelDimension",
    "MODEL_DIMENSIONS",
    "declared_dimension",
    "model_from_identity",
    "normalise_model_name",
    "supported_models",
]
