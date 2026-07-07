"""Embedding pipeline — batch embedding via the model gateway ONLY (R18-B1).

Embeddings are model calls. The boundary rule applies in full: **every** embedding
is produced through the model gateway's embedding provider
(``get_embedding_provider()`` / the gateway's instrumented ``embed()``), so
in-boundary and customer-tenant deployments embed inside the customer's control
automatically (R16-D1). This module makes NO direct embedding-API call — any such
call anywhere outside ``app/model_gateway/`` is a build-breaking violation of the
R16-D1 no-bypass test (T7 / AC2), and for a sovereignty customer a data breach.

Scope (T1): this module establishes the gateway-only embedding entry point so the
skeleton is complete and the no-bypass test covers the package. The asynchronous
batch pipeline that stamps each vector with its model identity + version and never
blocks a discovery run (AC7 / AC8) is implemented in T3 on top of ``embed_texts``
and ``store.set_embedding``.
"""
from __future__ import annotations

import logging
from typing import List

from app.model_gateway import embed as _gateway_embed
from app.model_gateway import get_embedding_provider

logger = logging.getLogger(__name__)


def embedding_model_identity() -> str:
    """Return the active embedding provider's name (stamped per vector, AC8).

    The concrete model id + version stamping is finalised in T3; T1 exposes the
    provider identity so the store's model columns have a defined source. Resolved
    live so a provider/config change is picked up without restart.
    """
    return get_embedding_provider().name


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a batch of strings through the gateway. One vector per input.

    Routes through the gateway's instrumented ``embed()`` so the call is observable
    and honours the active provider (hosted / in-boundary / customer-tenant). The
    gateway degrades gracefully — it returns an empty list on failure — so callers
    (the T3 pipeline) treat a short/empty result as "not embedded yet", never a
    crash: embedding lag must never block a run (AC7).
    """
    if not texts:
        return []
    return _gateway_embed(texts)
