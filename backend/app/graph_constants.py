"""ENT-4 / ENT-3 — single source of truth for graph-context hard caps.

These caps are enterprise-safety constants, NOT per-run configurable. They are
co-located here (rather than duplicated across modules) so that if the caps must
change together — e.g. when upgrading to a Claude model with a larger context
window — they are updated in exactly one place. See the review finding "hard
caps split across two files" (ENT-4 #9).

Consumers (must import, never redefine):
  * ``app.graph_context_builder`` — ENT-4 neighbourhood ranking + prompt summary.
  * ``app.graph_context``         — ENT-3 run-KV enrichment bridge.
"""
from __future__ import annotations

# Cap on entities placed in the LLM prompt context. A larger neighbourhood is
# truncated (most-significant-first) and the model is told it sees a partial view.
GRAPH_CONTEXT_MAX_ENTITIES = 15

# Cap on relationship edges placed in the LLM prompt context.
GRAPH_CONTEXT_MAX_RELATIONSHIPS = 20

# Below this many entities the graph is too thin to ground against; the caller
# falls back to its pre-graph prompt and runs no hallucination guard.
SPARSE_GRAPH_THRESHOLD = 3
