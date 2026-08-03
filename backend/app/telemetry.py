"""Telemetry write and read API — shared foundation for AgentIQ 2.0.

Public surface
--------------
  record_event(event_type, payload)  — fire-and-forget write for registered events.
  get_telemetry_range(...)           — time-range read scoped to org_id.
  register_event_type(name, schema)  — register a TypedDict schema for an event type.

Signature contract (locked by T3-S10-A):
    record_event(event_type: str, payload: Optional[dict] = None) -> None

All telemetry writes go through record_event(). No story writes directly
to telemetry_events.
"""

from __future__ import annotations

import json
import logging
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, MutableMapping, Optional, Type

from typing_extensions import NotRequired, TypedDict

from database.connection import get_db_connection, get_db_session
from database.models.telemetry import ALL_TELEMETRY_DDL

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event type registry — maps event_type string to its payload TypedDict class.
# Mutable so new event types can be registered at module load time.
# ---------------------------------------------------------------------------

EVENT_REGISTRY: MutableMapping[str, Type[Any]] = {}
TELEMETRY_EVENT_REGISTRY = EVENT_REGISTRY   # alias for Track 3 imports
EVENT_TYPE_REGISTRY = EVENT_REGISTRY        # alias for T1-S10-C unit tests

# ---------------------------------------------------------------------------
# Lazy table initialisation
# ---------------------------------------------------------------------------

_table_ready = False


def _ensure_telemetry_table() -> None:
    """No-op. The telemetry_events table is provisioned by database/provision/provision.sh."""
    return None


# ---------------------------------------------------------------------------
# Domain object — one instance per row written/returned
# ---------------------------------------------------------------------------

@dataclass
class TelemetryEvent:
    """In-memory representation of a telemetry_events row."""

    id: str
    org_id: str
    event_type: str
    source: str
    run_id: Optional[str]
    connector_id: Optional[str]
    pack_id: Optional[str]
    duration_ms: Optional[int]
    success: Optional[bool]
    count: Optional[int]
    error_code: Optional[str]
    payload: str        # JSON-serialised dict
    timestamp: datetime


# ---------------------------------------------------------------------------
# Application exception for read failures
# ---------------------------------------------------------------------------

class TelemetryReadError(Exception):
    """Raised by get_telemetry_range() when the database operation fails."""


# ---------------------------------------------------------------------------
# Payload TypedDicts — documentation only; record_event() accepts any dict.
# ---------------------------------------------------------------------------

class RunStartedPayload(TypedDict, total=False):
    pack_id: NotRequired[Optional[str]]
    system_count: NotRequired[Optional[int]]


class RunCompletedPayload(TypedDict, total=False):
    pack_id: NotRequired[Optional[str]]
    system_count: NotRequired[Optional[int]]
    # R-1.9.1-L1 / T6 (AC5): the license deployment_type (saas | customer_hosted)
    # the run executed under, stamped by the discovery runner so L2 billing can
    # record the AI-deployment topology. None when the org has no verifiable license.
    deployment_type: NotRequired[Optional[str]]


class ConnectorHealthPayload(TypedDict):
    status: str                          # 'connected' | 'needs_refresh' | 'needs_auth'
    connector_id: str
    token_expiry_seconds: Optional[int]
    check_duration_ms: int


class RunSignalSnapshotPayload(TypedDict, total=False):
    """T3-S10-A — aggregate signal snapshot write summary per run."""
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    pack_id: NotRequired[str]
    signal_count: int
    detector_count: int
    fired_count: int
    below_threshold: int


RunSignalSnapshotEvent = RunSignalSnapshotPayload   # alias


class PackExecutedPayload(TypedDict, total=False):
    """Exact, org-scoped execution snapshot for a discovery pack."""
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    pack_id: NotRequired[str]
    pack_name: NotRequired[str]
    pack_version: NotRequired[str]
    detector_ids: NotRequired[list[str]]
    detector_count: NotRequired[int]
    evaluated_count: NotRequired[int]
    not_evaluated_count: NotRequired[int]
    executed_at: NotRequired[str]
    duration_ms: NotRequired[int]


class DetectorFiredPayload(TypedDict, total=False):
    """T1-S14-C — one record per detector that fires in a run."""
    detector_id: NotRequired[str]
    pack_id: NotRequired[str]


class LlmEnrichmentAttemptedPayload(TypedDict, total=False):
    """T1-S14-C — written on each LLM enrichment call."""
    model: NotRequired[str]
    prompt_tokens: NotRequired[int]
    completion_tokens: NotRequired[int]


class EntityExtractionCompletedPayload(TypedDict, total=False):
    """T3-S12-A T7 — written after entity extraction completes successfully.

    ambiguous_count is load-bearing for monitoring: a spike in ambiguous
    entities per org_id signals naming-convention changes or data-quality
    degradation in the source system. Not emitted on exception — runner
    warning log covers that failure path.
    """
    entity_count: NotRequired[int]
    ambiguous_count: NotRequired[int]
    failure_count: NotRequired[int]
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    source: NotRequired[str]
    pack_id: NotRequired[str]
    # ENT-1 / AC5: count of service-account identities filtered out by the
    # active entity-extraction overlay. 0 (or absent) when no overlay is active.
    filtered_service_account_count: NotRequired[int]


class TemporalEnrichmentCompletedPayload(TypedDict, total=False):
    """T3-S11-A — emitted once per run after temporal enrichment completes.

    org_id is threaded by materialize_t2 so the event is attributed to the run's
    org (R17-D3 / AT-450 T5). record_event validates the event TYPE only — not
    payload keys — so this declaration is documentation of the emitted shape rather
    than a runtime gate, but it keeps the registered schema honest (review L2).
    """
    run_id: NotRequired[str]
    opp_count: NotRequired[int]
    org_id: NotRequired[str]


class RelationshipMappingCompletedPayload(TypedDict, total=False):
    """T3-S13-A — emitted once per run after relationship mapping completes.

    Emitted from inside relationship_mapper.map_relationships() on success only.
    The runner's non-blocking wrapper swallows any failure, so the ABSENCE of
    this event alongside a warning log is the diagnostic signal for a failed
    mapping run.

    observed_count   — directly observed edges written (confidence=0.9, inferred=False).
    inferred_count   — co-firing inferred edges written (confidence=0.6, inferred=True).
                       Always stored regardless of INFERRED_RELATIONSHIPS_ENABLED.
    skipped_ambiguous_count — edges skipped because one endpoint had
                       resolution_status='ambiguous'. A spike in this field
                       signals entity-resolution quality degradation upstream.
    mapping_duration_ms — wall-clock time for both mapping passes combined.
    """
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    observed_count: NotRequired[int]
    inferred_count: NotRequired[int]
    skipped_ambiguous_count: NotRequired[int]
    mapping_duration_ms: NotRequired[float]


class HallucinationGuardRemovedPayload(TypedDict, total=False):
    """ENT-3 / T3-S15-A — emitted when the hallucination guard DROPS a bullet.

    Two drop paths produce this event, distinguished by ``reason``:
      reason='dropped_generic' — generic bullet with no graph content, dropped
                                 without attempting a second-pass LLM rewrite.
      reason='dropped_timeout' — second-pass LLM rewrite exceeded
                                 REWRITE_TIMEOUT_MS and the bullet was dropped.

    PII GUARD: this payload carries COUNTS, reason codes, and run/org
    identifiers ONLY. The hallucinated proper nouns and the bullet text itself
    are NEVER added here — they can contain fabricated or real person/team
    names and telemetry must not log sensitive values.
    """
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    reason: NotRequired[str]          # 'dropped_generic' | 'dropped_timeout'
    hallucinated_count: NotRequired[int]
    source: NotRequired[str]


class HallucinationGuardRewrittenPayload(TypedDict, total=False):
    """ENT-3 / T3-S15-A — emitted when the hallucination guard REPAIRS a bullet.

    Two repair paths produce this event, distinguished by ``method``:
      method='rule_rewrite' — deterministic rule-based rewrite produced a
                              coherent bullet; no LLM call was made.
      method='llm_rewrite'  — second-pass LLM rewrite produced a clean bullet.

    PII GUARD: counts, method, and run/org identifiers only — never the
    hallucinated names or bullet text.
    """
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    method: NotRequired[str]          # 'rule_rewrite' | 'llm_rewrite'
    hallucinated_count: NotRequired[int]
    source: NotRequired[str]


class LlmEnrichmentGroundedPayload(TypedDict, total=False):
    """ENT-3 / T3-S15-A — emitted once per opportunity when first-pass
    enrichment ran against a real (non-sparse) ENT-4 graph context.

    Distinguishes graph-grounded runs from the sparse-graph fallback path
    (entity_count < 3) which sets llm_grounded=False and emits nothing.

    Carries graph shape counts only — no entity names.
    """
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    opp_id: NotRequired[str]
    graph_entity_count: NotRequired[int]
    graph_entity_count_shown: NotRequired[int]
    graph_truncated: NotRequired[bool]
    source: NotRequired[str]


class CausalHypothesisRejectedPayload(TypedDict, total=False):
    """ENT-6 / T3-S16-A — emitted when a causal hypothesis is rejected.

    Closed reason set (T4 / T10 contract):
      no_falsifiability        — cause_chain or falsifiability_condition absent/empty.
      generic_falsifiability   — falsifiability condition names no measurable disproof.
      empty_cause_chain        — all steps were empty or filtered out.
      hallucination_in_cause_chain — < 2 steps survived the entity-name guard.
      insufficient_graph_context   — neighbourhood < 3 entities (T2).

    PII GUARD: reason codes and run/org identifiers only — no hypothesis text.
    """
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    opportunity_id: NotRequired[str]
    reason: NotRequired[str]


class CausalHypothesisGeneratedPayload(TypedDict, total=False):
    """ENT-6 / T3-S16-A — emitted when a hypothesis is stored (T6), after commit.

    PII GUARD: identifiers, the preliminary flag, and gate metrics only — never
    hypothesis text (cause_chain / falsifiability_condition).
    """
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    opportunity_id: NotRequired[str]
    preliminary: NotRequired[bool]
    confidence: NotRequired[float]
    gate_run_count: NotRequired[int]
    inferred: NotRequired[bool]


class GraphContextBuiltPayload(TypedDict, total=False):
    """ENT-4 / T3-S14-A — emitted once per build_graph_context() call.

    Records the shape of the graph context assembled for an opportunity:
    the full entity count, the capped count actually placed in the prompt,
    whether the graph was truncated past the 15-entity / 20-relationship caps,
    and the build duration. Carries shape counts only — no entity names.
    """
    org_id: NotRequired[str]
    opportunity_id: NotRequired[str]
    entity_count: NotRequired[int]
    entity_count_shown: NotRequired[int]
    relationship_count: NotRequired[int]
    relationship_count_shown: NotRequired[int]
    truncated: NotRequired[bool]
    sparse_graph: NotRequired[bool]
    duration_ms: NotRequired[int]
    source: NotRequired[str]


class RunStartedEvent(TypedDict):
    run_id: str
    org_id: str
    # R-1.9.1-L1 / T6 (AC5): license deployment_type stamped by the discovery
    # runner into the run's telemetry context (None when unlicensed).
    deployment_type: NotRequired[Optional[str]]


class RunCompletedEvent(TypedDict):
    run_id: str
    org_id: str
    duration_ms: int
    connectors_processed: int


class ConnectorRegisteredEvent(TypedDict):
    connector_id: str
    org_id: str


class DbQueryExecutedEvent(TypedDict, total=False):
    """T1-S10-C T2 events — written after every connector DB/API query.

    org_id is threaded by the (background) DB connector query path so the event
    is attributed to the org that owns the run (R17-D3 / AT-450 T5-AC1).
    """
    org_id: NotRequired[str]
    connector_id: str
    query_hash: str
    row_count: int
    duration_ms: int
    driver: str
    truncated: bool


class DbIngestorCompletedEvent(TypedDict):
    """T1-S10-C T2 events — written after a connector ingestor finishes."""
    connector_id: str
    tables_processed: int
    rows_ingested: int
    duration_ms: int


class DBIngestorCompletedPayload(TypedDict):
    """T2-S11-A Sprint 11 payload for db.ingestor_completed.

    Replaces DbIngestorCompletedEvent in the registry.  Written by every
    Track 2 DB ingestor (SQL Server, Oracle DB, PostgreSQL) at the end of
    each discovery run.  Emitted via record_event() — fire-and-forget.

    Fields
    ------
    connector_id:
        Identifies the source database connector, e.g. ``'sqlserver'``.
    pack_id:
        The detector pack that consumed the ingested signals,
        e.g. ``'sqlserver_opsignal'``.
    query_count:
        Number of execute_query() calls made during this ingestion run
        (one per signal query, e.g. 3 for the SQL Server ingestor).
    signal_count:
        Number of signal metrics successfully extracted across all queries.
    degraded_count:
        Number of metrics with degraded_signal=True (query timeout,
        missing column, or other partial-failure conditions).
    duration_ms:
        Total wall-clock time for the entire ingestor execution in
        milliseconds, from first query to return.
    org_id:
        The org that owns the run. Threaded by every DB ingestor so the
        completed event is attributed to the right tenant (R17-D3 / AT-450
        T5-AC1), since ingestion runs in a background context with no request.
    """
    org_id: NotRequired[str]
    connector_id: str
    pack_id: str
    query_count: int
    signal_count: int
    degraded_count: int
    duration_ms: int


class AuditWriteFailedPayload(TypedDict):
    """AT-292 / FixPack v2 Fix 5 — emitted when an audit_log write fails.

    Audit writes are fail-silent by design (an audit failure must never break
    the request that triggered it), which previously made persistence failures
    invisible — no telemetry, no alert. Regulated enterprise customers (TCU,
    City National) require audit-trail integrity, so every swallowed audit write
    now surfaces here so it is observable and alertable. Emitted by
    app.middleware.audit.log_event() from its failure handler — fire-and-forget.

    PII GUARD: org/event identifiers and the stringified error only — never the
    audit payload field values themselves.

    org_id:     The org whose audit event failed to persist.
    event_type: The audit event_type that failed (e.g. 'connector_connected').
    error:      str(exception) from the failed write — exception text only.
    """
    org_id: str
    event_type: str
    error: str


# LIC-1 / AT-348 (T7) — license lifecycle event payloads.
# The offline license validator surfaces its state transitions here so
# CloudFulcrum can spot an approaching/lapsed term in the customer's own
# telemetry during support and start a proactive renewal conversation.
# PII GUARD: status, dates, and the license customer id only — NEVER the raw
# key string, signature, or any secret.

class LicenseValidatedPayload(TypedDict):
    """license.validated — emitted at each startup/periodic check (T4)."""
    customer: str
    status: str            # 'valid' | 'grace' | 'readonly'
    expires_at: str
    days_remaining: int


class LicenseEnteredGracePayload(TypedDict):
    """license.entered_grace — first crossing from valid into grace (T4)."""
    customer: str
    expires_at: str


class LicenseEnteredReadonlyPayload(TypedDict):
    """license.entered_readonly — first crossing from grace into read-only (T4)."""
    customer: str
    expires_at: str


class LicenseUpdatedPayload(TypedDict):
    """license.updated — a new key was installed via the admin route (T6)."""
    customer: str
    status: str
    expires_at: str


class LicenseClockAnomalyPayload(TypedDict):
    """license.clock_anomaly — clock-rollback guard tripped (T4, §6). Dates only."""
    last_seen: str
    now: str


class IngestionCheckpointResetPayload(TypedDict):
    """ingestion.checkpoint_reset — R16-A1 / AT-383 (§3, AC7).

    Emitted when an admin explicitly clears a source's ingestion checkpoint,
    forcing a full re-read on the next run. Identifiers + outcome only — no
    source data.

    org_id:          The org whose checkpoint was reset.
    connector_id:    The source connector whose checkpoint was cleared.
    had_checkpoint:  True if a checkpoint existed and was removed; False if there
                     was nothing to clear (already at "first run").
    """
    org_id: str
    connector_id: str
    had_checkpoint: bool


class IngestionArtifactChangedPayload(TypedDict):
    """ingestion.artifact_changed — R16-A1 / AT-381 (§4, AC4).

    Emitted once per changed source artifact when a connector reports a delta, so
    the Release 1.8 retrieval-freshness layer can later invalidate/refresh exactly
    the artifacts that changed. 1.6 only EMITS — there is no consumer yet
    (forward-design rule, §4). Identifiers + change kind only — no artifact content.

    org_id:        The org the artifact belongs to.
    connector_id:  The source connector that reported the change.
    artifact_id:   Stable id of the changed item (connector-defined).
    change_kind:   'created' | 'updated' | 'deleted'.
    observed_at:   When the change was observed during the run (UTC ISO).
    """
    org_id: str
    connector_id: str
    artifact_id: str
    change_kind: str
    observed_at: str


class IngestionSecretRedactedPayload(TypedDict, total=False):
    """ingestion.secret_redacted — R18-A2 / AT-531 (§1, AC5).

    Emitted once per content artifact from which a committed secret was redacted
    BEFORE the content reached the retrieval substrate, so the redaction is
    observable in run health. Identifiers + pattern types + counts ONLY — the
    matched secret value is NEVER carried on the event (recording the secret would
    re-leak exactly what redaction removed).

    org_id:          The org the content belongs to.
    connector_id:    The source connector that redacted (e.g. 'git_content').
    source_system:   The producing system (e.g. 'git').
    source_artifact: The artifact the secret was redacted from (file path / commit).
    content_type:    Which content stream ('code' | 'conversation' | 'prose').
    redaction_count: How many secrets were redacted from this artifact.
    pattern_types:   Distinct signature names that fired (e.g. ['aws_access_key_id']).
    repo:            The repository id, when the producer supplies it.
    observed_at:     When the redaction happened during the run (UTC ISO).
    """
    org_id: str
    connector_id: str
    source_system: NotRequired[str]
    source_artifact: NotRequired[str]
    content_type: NotRequired[str]
    redaction_count: int
    pattern_types: NotRequired[List[str]]
    repo: NotRequired[Optional[str]]
    observed_at: NotRequired[str]


class IngestionStructureCapturedPayload(TypedDict, total=False):
    """ingestion.structure_captured — R18-A2 / AT-534 (§1, "Structure").

    Emitted once per repository whose directory-tree + file-inventory structural
    metadata was captured (graph-facing, NOT embedded) so the capture is visible
    in run health. Counts + identifiers ONLY — never file paths' contents.

    org_id:             The org the repository belongs to.
    connector_id:       The source connector (e.g. 'git_content').
    source_system:      The producing system (e.g. 'git').
    repo:               The repository id the snapshot describes.
    commit_sha:         The HEAD commit the captured shape reflects.
    mode:               'full' (from a tree walk) or 'incremental' (diff-applied).
    file_count:         In-scope files in the inventory.
    directory_count:    Directory nodes in the derived tree (incl. the root).
    binary_file_count:  Inventoried files whose body was skipped as binary.
    observed_at:        When the capture happened during the run (UTC ISO).
    """
    org_id: str
    connector_id: str
    source_system: NotRequired[str]
    repo: NotRequired[str]
    commit_sha: NotRequired[str]
    mode: NotRequired[str]
    file_count: NotRequired[int]
    directory_count: NotRequired[int]
    binary_file_count: NotRequired[int]
    observed_at: NotRequired[str]


class IngestionArtifactSkippedPayload(TypedDict, total=False):
    """ingestion.artifact_skipped — R18-C2 / T2 (emission gap-fill).

    Emitted at the ORIGIN — the document ingestor (``discovery/ingest/documents.py``)
    — once per artifact it skips-with-reason, so the Run-Health Dashboard's content
    panel (R18-C2 T1, AC3) can report skipped-with-reason volume as an EXPLICIT,
    org-scoped fact rather than inferring it. Before this, a skip was recorded only
    on the in-flight hand-off record and a WARNING log — never a queryable event —
    so the state existed but the dashboard could not read it. Fire-and-forget by
    contract: a telemetry write failure must never break ingestion.

    org_id:          The org the content belongs to (required for aggregation).
    connector_id:    The ingestor that skipped (e.g. 'documents').
    source_system:   The producing system for the artifact.
    artifact_id:     The skipped artifact (file id / path).
    reason:          Why it was skipped — one of the extraction reasons
                     (size_capped, budget_exceeded, unsupported_format, no_handler,
                     encrypted, scanned_image).
    count:           Artifacts represented by this event (always 1 per artifact).
    run_id:          The run during which the skip occurred, when available.
    observed_at:     When the skip happened (UTC ISO), when available.
    """
    org_id: str
    connector_id: str
    source_system: NotRequired[str]
    artifact_id: NotRequired[str]
    reason: str
    count: NotRequired[int]
    run_id: NotRequired[str]
    observed_at: NotRequired[str]


class IngestionCompletedPayload(TypedDict, total=False):
    """ingestion.completed — the CONNECTOR-AGNOSTIC ingestion-completion fact.

    Emitted once per successful pass of the SHARED change-based ingestion runner
    (``discovery/ingest/change_runner.py::ingest_with_checkpoint``) — the single
    completion path every ``ChangeBasedIngestor`` already travels.

    Why a new event type was needed. The Run-Health connectors panel
    (``app/health_aggregation.py``) reads "last successful ingestion" from exactly
    two places, and NEITHER covers a change-based connector:

      * ``db.ingestor_completed`` — emitted only by the native DB ingestors. Its
        payload is DB-shaped (query_count / signal_count / degraded_count) and the
        panel derives ``last_error`` from its ``degraded_count``, so a non-DB
        connector borrowing it would inject false meaning into the error column.
      * the ``lastSynced`` display string — written by ``app/connector_metrics.py``
        for the three hardcoded ids salesforce / servicenow / jira_confluence.

    The other candidates were checked and rejected: ``ingestion.artifact_changed``
    is per-RECORD and suppressed entirely for a transport-only connector
    (``produces_retrieval_content = False``) and for any empty delta, so it cannot
    witness a completed pass; and the ingestion checkpoint's ``captured_at`` is
    already surfaced separately as ``checkpoint_captured_at`` /
    ``checkpoint_age_seconds`` (rendered as its own two facts) and only advances
    when the position MOVES, so an idle-but-healthy connector would report a stale
    ingestion time and trip the stalled-checkpoint rule.

    Success is the runner's OWN definition — no captured error on the pass
    (``IngestionResult.error is None``). A pass that polled cleanly and found
    nothing new IS a successful ingestion and is reported as one: gating on
    ``count > 0`` would leave an idle connector indistinguishable from one that
    never ran.

    Fire-and-forget by contract: a telemetry failure must never break ingestion.
    Secret-free — identifiers and counts only, never a credential, a checkpoint
    value, or record content.

    org_id:              The org the ingestion belongs to (required for attribution).
    connector_id:        The ingestor's declared id (e.g. 'azure_events') — the same
                         key the runner checkpoints by and the panel joins on.
    count:               Records reported across the pass (0 is meaningful).
    batches:             Delta batches processed.
    complete:            Whether the source reported a terminal batch.
    checkpoint_advanced: Whether a checkpoint was written this pass.
    first_run:           Whether this was a first (full-load) pass.
    observed_at:         When the pass completed (UTC ISO), when available.
    """
    org_id: str
    connector_id: str
    count: NotRequired[int]
    batches: NotRequired[int]
    complete: NotRequired[bool]
    checkpoint_advanced: NotRequired[bool]
    first_run: NotRequired[bool]
    observed_at: NotRequired[str]


# R16-D1 / AT-366 (T5) — model provider gateway telemetry.
# Emitted once per gateway generate()/embed() call so model usage is observable
# across hosted, in-boundary, and future customer-tenant modes. The provider
# name records WHICH backend served the call.
# PII GUARD: provider name, ok flag, and counts only — NEVER the prompt text,
# generated output, input texts, or embedding vectors.

class ModelGenerationCompletedPayload(TypedDict, total=False):
    """model.generation_completed — emitted once per gateway generate() call (T5).

    provider is the name from GenerationResult.provider — the backend that
    actually served the request. Emitted on success and failure alike so a
    provider error (ok=False) is observable.
    """
    provider: NotRequired[str]
    ok: NotRequired[bool]
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    source: NotRequired[str]


class ModelEmbeddingCompletedPayload(TypedDict, total=False):
    """model.embedding_completed — emitted once per gateway embed() call (T5).

    provider is the active embedding provider's name. text_count/vector_count
    carry only sizes — never the texts or vectors themselves.
    """
    provider: NotRequired[str]
    ok: NotRequired[bool]
    text_count: NotRequired[int]
    vector_count: NotRequired[int]
    org_id: NotRequired[str]
    run_id: NotRequired[str]
    source: NotRequired[str]


class RetrievalQueryCompletedPayload(TypedDict, total=False):
    """retrieval.query_completed — R18-B1 T4, emitted once per retrieve() call.

    Makes source-aware semantic retrieval observable: how many chunks a query
    returned, whether it was source-scoped, and whether a min-score floor applied.

    PII GUARD: identifiers, counts, and filter SHAPE only — NEVER the query text,
    the chunk content, or the vectors. ``source_filter`` records the NAMES of the
    scoped source systems ('confluence', 'slack', …), which are non-sensitive
    system identifiers, so a caller can see how a scope affected recall.

    org_id:            The org whose partition was searched (hard-scoped).
    k:                 The requested result cap.
    result_count:      How many ranked chunks were returned (<= k).
    source_filter:     The normalised source systems the query was scoped to, or
                       absent when unscoped.
    min_score:         The similarity floor applied, or absent when none.
    query_embedded:    False when the query could not be embedded (gateway
                       degraded) — a retrieval miss, never a crash.
    include_stale:     Whether stale chunks were included this query (R18-B2 T4);
                       default retrieval excludes them.
    stale_count:       How many returned chunks were stale — present only when
                       stale chunks were being included.
    """
    org_id: NotRequired[str]
    k: NotRequired[int]
    result_count: NotRequired[int]
    source_filter: NotRequired[list]
    min_score: NotRequired[float]
    query_embedded: NotRequired[bool]
    include_stale: NotRequired[bool]
    stale_count: NotRequired[int]


class RetrievalArtifactInvalidatedPayload(TypedDict, total=False):
    """retrieval.artifact_invalidated — R18-B2 T1, emitted once per handled change.

    Makes the freshness contract observable: when a source artifact changes, the
    freshness subscriber records what it did — marked chunks stale + queued a
    refresh (created/updated), or removed chunks immediately (deleted). This is the
    'staleness is allowed to exist; it is never allowed to be invisible' rule
    (Section 1) at the invalidation moment; T6 aggregates the standing metrics.

    PII GUARD: identifiers, the change kind, and counts only — NEVER artifact
    content. ``source_system`` / ``source_artifact`` are non-sensitive system
    identifiers, same as the emitted ingestion.artifact_changed event.

    org_id:          The org the artifact belongs to (hard-scoped).
    source_system:   The connector/source system that reported the change.
    source_artifact: Stable id of the changed artifact (connector-defined).
    change_kind:     'created' | 'updated' | 'deleted'.
    action:          What the subscriber did — 'marked_stale' | 'removed'.
    chunks_affected: How many chunks were marked stale or removed.
    queued:          True when the artifact was queued for async refresh
                     (created/updated); absent/False for a deletion.
    """
    org_id: NotRequired[str]
    source_system: NotRequired[str]
    source_artifact: NotRequired[str]
    change_kind: NotRequired[str]
    action: NotRequired[str]
    chunks_affected: NotRequired[int]
    queued: NotRequired[bool]


class IngestionSubscriptionHealthPayload(TypedDict, total=False):
    """ingestion.subscription_health — MSP-B2 T6 / AT-653.

    Emitted per PINNED Azure subscription (per event stream) whose poll FAILED, so
    a subscription-specific failure is LOUD in discovery run health while unaffected
    subscriptions keep processing (failure isolation). Identifiers + failure
    classification + retry counts ONLY — never a secret, token, or raw payload.

    org_id:          The org the subscription belongs to.
    connector_id:    Always 'azure_events'.
    source_system:   The event stream that failed ('azure_monitor' | 'azure_activity'
                     | 'azure_service_health'), or the connector when auth failed.
    account_scope:   The Azure subscription id (B0 account scope).
    stream:          The connector stream key ('alerts'|'activity_log'|'service_health').
    status:          'error' (a health event is emitted only on failure).
    category:        Failure class — 'authentication' | 'authorization' | 'not_found'
                     | 'throttled' | 'server_error' | 'timeout' | 'network' |
                     'malformed_response' | 'unexpected'.
    retryable:       Whether the failure is transient (was eligible for backoff retry).
    attempts:        How many attempts were made (1 + retries).
    recoverable:     Whether a later run may recover (transient) vs needs operator
                     action (auth/authorization/not_found).
    error_summary:   Short, non-sensitive error string (never a secret/token/body).
    observed_at:     When the failure was observed during the run (UTC ISO).
    """
    org_id: NotRequired[str]
    connector_id: str
    source_system: NotRequired[str]
    account_scope: NotRequired[str]
    stream: NotRequired[str]
    status: str
    category: str
    retryable: NotRequired[bool]
    attempts: NotRequired[int]
    recoverable: NotRequired[bool]
    error_summary: NotRequired[str]
    observed_at: NotRequired[str]


class RetrievalModelBackfillPayload(TypedDict, total=False):
    """retrieval.model_backfill — R18-B2 T5, emitted per org pass that re-embedded.

    Makes an embedding-model-version migration observable: when the provider/version
    repins, old-model vectors are invalidated (excluded from retrieval immediately)
    and re-embedded onto the active model by a managed background backfill. This
    event records each pass that actually re-embedded, so a migration's progress is
    never invisible.

    PII GUARD: identifiers, the ACTIVE model stamp, and counts only — NEVER chunk
    content or vectors. ``embedding_model`` / ``embedding_model_version`` are the
    non-sensitive active-model identifiers everything is being converged onto.

    org_id:                  The org whose old-model vectors were backfilled.
    embedding_model:         Active model identity now stamped on the re-embedded
                             vectors.
    embedding_model_version: Active model version now stamped.
    reembedded:              How many old-model vectors were re-embedded this pass.
    incompatible_seen:       How many old-model vectors this pass attempted.
    batches:                 Gateway embedding calls made this pass.
    """
    org_id: NotRequired[str]
    embedding_model: NotRequired[str]
    embedding_model_version: NotRequired[str]
    reembedded: NotRequired[int]
    incompatible_seen: NotRequired[int]
    batches: NotRequired[int]


class SecOpsEvidencePointerResolvedPayload(TypedDict, total=False):
    """secops.evidence_pointer_resolved — MSP-B12 T3.

    Emitted on EVERY attempt to resolve a Security-Operations evidence pointer to
    its individual source record — the access audit the aggregation floor requires.
    Individual records are reachable only through org-scoped, access-controlled
    pointers, and each resolution (or denied attempt) leaves this trail.

    PII GUARD: identifiers, outcome, and access time ONLY — never the resolved
    record's content. ``pointer_id`` is the source-artifact record id (a workflow
    record identifier, not a host or CVE); ``user_id`` attributes the access.

    org_id:        Requesting organization (the org the resolution was scoped to).
    user_id:       The requesting user (access attribution).
    source_system: The source system the pointer resolves into (e.g. 'servicenow').
    pointer_id:    The source-artifact identifier the pointer names.
    outcome:       'resolved' | 'denied'.
    reason:        Why a resolution was denied ('insufficient_role' |
                   'not_found_or_cross_org' | 'invalid_pointer'); absent on success.
    access_time:   When the resolution was attempted (UTC ISO-8601).
    """
    org_id: str
    user_id: NotRequired[str]
    source_system: NotRequired[str]
    pointer_id: NotRequired[str]
    outcome: str
    reason: NotRequired[str]
    access_time: NotRequired[str]


class BillingRunCompletedPayload(TypedDict, total=False):
    """billing.run_completed — R-1.9.1-L2 / T1 (AC1).

    Emitted once into the immutable telemetry store for EVERY discovery run,
    regardless of AI mode. Billability is DERIVED BY THE L2 USAGE REPORT, never
    decided at emission (hosted = billable; in_boundary / customer_tenant are
    recorded for audit) — so this event is a neutral, complete record of what ran.

    ai_mode is the active generation provider mode (hosted | in_boundary |
    customer_tenant); provider is that provider's concrete name (coincides with
    ai_mode today, kept distinct so the report can evolve). connected_system_count
    is the org's connected Integration-Hub entities (the pricing "one connected
    entity = one system" definition), NOT the number of source systems ingested by
    this run. pack_ids is a list (forward-compatible with multi-pack runs, P1).
    deployment_type is the license topology stamped from L1.

    PII/secret guard: identifiers, mode, counts, pack ids, and timestamps only —
    never prompt/output text, tokens, or credentials.
    """
    run_id: NotRequired[str]
    org_id: NotRequired[str]
    ai_mode: NotRequired[str]
    provider: NotRequired[Optional[str]]
    connected_system_count: NotRequired[Optional[int]]
    pack_ids: NotRequired[list]
    deployment_type: NotRequired[Optional[str]]
    started_at: NotRequired[Optional[str]]
    completed_at: NotRequired[Optional[str]]


class OpportunityLifecycleTransitionPayload(TypedDict, total=False):
    """opportunity.lifecycle_transitioned — 2.0-A2 / T1.

    One event per lifecycle transition of an opportunity IDENTITY (not a run):
    ``open -> actioned -> monitoring -> measured``, plus ``dismissed`` and
    ``stalled``. Registered here BEFORE any emission call site exists, because
    ``record_event()`` raises ``ValueError`` for an unregistered ``event_type`` —
    so registering after the first emitter would fail at the first transition.

    ``actor`` distinguishes ``human`` from ``system``: the platform never infers
    that a change was deployed, so a ``to_state`` of ``actioned`` always carries
    ``actor='human'`` and a non-null ``action_date``.

    Carries no free-form narrative and no evidence values — an identity, a state
    pair, and the actor kind. No PII beyond the actor id the audit log already
    records.
    """

    org_id: str
    opportunity_identity: str
    from_state: str
    to_state: str
    actor: NotRequired[str]
    #: ISO date. Present only on a transition that records an action.
    action_date: NotRequired[Optional[str]]
    revision: NotRequired[int]
    run_id: NotRequired[Optional[str]]


class RankingAdjustmentChangedPayload(TypedDict, total=False):
    """ranking_adjustment.changed — 2.0-A3 / T4.

    Emitted after the per-org ranking-adjustment state changes through explicit
    recomputation or Owner reset. Registered before the emitter exists because
    ``record_event()`` raises ``ValueError`` for an unregistered event type.

    Carries governance metadata only: actor, org, change kind, before/after state
    summaries, and counts. The audit log remains the primary governance record.
    """

    org_id: str
    actor_id: str
    change_kind: str
    target: NotRequired[str]
    previous_state: NotRequired[list]
    current_state: NotRequired[list]
    groups_changed: NotRequired[int]
    opportunities_affected: NotRequired[int]
    config_version: NotRequired[Optional[str]]
    changed_at: NotRequired[str]
    reason: NotRequired[Optional[str]]


class BillingSystemLedgerPayload(TypedDict, total=False):
    """billing.system_connected / billing.system_disconnected — R-1.9.1-L2 / T2 (AC2).

    The immutable connect/disconnect LEDGER: one event is emitted for each genuine
    system addition (``billing.system_connected``) and each genuine removal
    (``billing.system_disconnected``). Together with the per-run billing record
    (T1) this gives the L2 usage report the pro-ration record CloudFulcrum needs
    for mid-term system additions/removals — a system connected partway through a
    period, or disconnected before its end, is billed for the portion it was live.

    A "system" is one connected Integration-Hub entity (the pricing "one connected
    entity = one system" definition), so the ledger records only true state
    transitions: re-authorising an already-connected connector is not a new system
    and emits nothing, and disconnecting a connector that was never connected emits
    nothing. This keeps the ledger free of phantom additions/removals so the report
    aggregates cleanly (usage_report.py reads connector / system_identity /
    occurred_at / seq for each ledger entry).

    connector is the Integration-Hub connector id (e.g. ``salesforce``, ``jira``);
    system_identity is the concrete instance being added/removed (the captured
    instance/base URL where one is known, else the connector id) so pro-ration can
    tell two instances of the same connector type apart. occurred_at is the ISO-8601
    UTC transition time. seq is the per-org monotonic tamper-evidence sequence
    number stamped at emission (T4); None when the counter could not be advanced
    (the event is still emitted, just unsequenced).

    PII/secret guard: identifiers, a non-secret instance URL, a timestamp, and the
    sequence number only — never tokens, credentials, or the org's private data.
    """
    connector: NotRequired[str]
    system_identity: NotRequired[Optional[str]]
    occurred_at: NotRequired[str]
    org_id: NotRequired[str]
    seq: NotRequired[Optional[int]]


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def register_event_type(event_type: str, schema: Type[Any]) -> None:
    """Register an event type and its payload TypedDict schema.

    Idempotent when called with the same schema.  Raises ValueError if the
    same event_type is registered with a different schema (catches mistakes).
    """
    if event_type in EVENT_REGISTRY:
        if EVENT_REGISTRY[event_type] is not schema:
            raise ValueError(
                f"Telemetry event type '{event_type}' is already registered "
                f"with {EVENT_REGISTRY[event_type]!r}; cannot re-register "
                f"with {schema!r}."
            )
        return
    EVENT_REGISTRY[event_type] = schema


# ---------------------------------------------------------------------------
# Named aliases — used by AT-211 contract tests and ValueError message copy.
# REGISTERED_EVENT_TYPES: set-like view of every registered event_type name.
# EVENT_PAYLOAD_TYPES:    mapping of event_type → TypedDict schema class.
# Both are live views of EVENT_REGISTRY; no separate sync required.
# ---------------------------------------------------------------------------

REGISTERED_EVENT_TYPES = EVENT_REGISTRY   # alias: keys are the registered names
EVENT_PAYLOAD_TYPES = EVENT_REGISTRY      # alias: values are the TypedDict schemas


# Register Sprint 10 initial set
register_event_type("run.started", RunStartedEvent)
# AT-209 audit: run.completed call sites send pack_id/system_count (see
# discovery/runner.py), which match RunCompletedPayload. The legacy
# RunCompletedEvent required a connectors_processed field that no call site
# emits, so the payload never matched its registered schema. Bind to the
# documented RunCompletedPayload (Task 5A §1b). RunCompletedEvent is retained
# in __all__ for backward-compatible imports.
register_event_type("run.completed", RunCompletedPayload)
register_event_type("connector.registered", ConnectorRegisteredEvent)
register_event_type("connector.health_check", ConnectorHealthPayload)
register_event_type("db.query_executed", DbQueryExecutedEvent)
register_event_type("db.ingestor_completed", DBIngestorCompletedPayload)
register_event_type("run.signal_snapshot", RunSignalSnapshotPayload)
# R18-C2 T2: emitted at the detector execution origin. The run-health Packs
# panel consumes this historical snapshot instead of reconstructing an old run
# from the mutable pack registry.
register_event_type("run.pack_executed", PackExecutedPayload)
# T3-S11-A Sprint 11
register_event_type("temporal.enrichment_completed", TemporalEnrichmentCompletedPayload)
# T3-S12-A T7 Sprint 12
register_event_type("entity.extraction_completed", EntityExtractionCompletedPayload)
# T3-S13-A Sprint 13 — relationship mapping (emitted by map_relationships())
register_event_type("relationship.mapping_completed", RelationshipMappingCompletedPayload)
# ENT-3 / T3-S15-A Sprint 15 — LLM enrichment enterprise hardening.
# hallucination_guard.* are emitted by app.hallucination_guard.log_hallucination();
# llm.enrichment_grounded is emitted by app.llm_enrichment on the grounded path.
register_event_type("hallucination_guard.removed", HallucinationGuardRemovedPayload)
register_event_type("hallucination_guard.rewritten", HallucinationGuardRewrittenPayload)
register_event_type("llm.enrichment_grounded", LlmEnrichmentGroundedPayload)
# ENT-6 / T3-S16-A — causal hypothesis lifecycle events
register_event_type("causal.hypothesis_rejected", CausalHypothesisRejectedPayload)
register_event_type("causal.hypothesis_generated", CausalHypothesisGeneratedPayload)
# ENT-4 / T3-S14-A Sprint 14 — graph context builder.
# graph.context_built is emitted by app.graph_context_builder.build_graph_context().
register_event_type("graph.context_built", GraphContextBuiltPayload)
# AT-292 / FixPack v2 Fix 5 — audit write-failure telemetry.
# audit.write_failed is emitted by app.middleware.audit.log_event() when an
# audit_log write is swallowed, so silent audit-persistence failures become
# observable and alertable.
register_event_type("audit.write_failed", AuditWriteFailedPayload)
# LIC-1 / AT-348 (T7) — license lifecycle events. Registered here so T4
# (validated / entered_grace / entered_readonly / clock_anomaly) and T6
# (updated) can emit them. record_event() raises ValueError for an unregistered
# type, so registration must land before any emission call-site.
register_event_type("license.validated", LicenseValidatedPayload)
register_event_type("license.entered_grace", LicenseEnteredGracePayload)
register_event_type("license.entered_readonly", LicenseEnteredReadonlyPayload)
register_event_type("license.updated", LicenseUpdatedPayload)
register_event_type("license.clock_anomaly", LicenseClockAnomalyPayload)
# R16-A1 / AT-383 (T7): admin checkpoint-reset action.
register_event_type("ingestion.checkpoint_reset", IngestionCheckpointResetPayload)
# R16-A1 / AT-381 (T5): per-changed-artifact event (emitted by the change runner).
register_event_type("ingestion.artifact_changed", IngestionArtifactChangedPayload)
# R18-A2 / AT-531 (T3): per-artifact secret-redaction event (emitted by the git
# content ingestor's secret scan before hand-off to the substrate). Registered
# here so the ingestor can emit it; record_event() raises ValueError for an
# unregistered type, so registration must precede the first emission.
register_event_type("ingestion.secret_redacted", IngestionSecretRedactedPayload)
# R18-A2 / AT-534 (T6): per-repo structural-metadata capture event (emitted by the
# git content ingestor when it persists a repo's tree/inventory as graph-facing
# metadata). Registered here so the ingestor can emit it; record_event() raises
# ValueError for an unregistered type, so registration must precede first emission.
register_event_type("ingestion.structure_captured", IngestionStructureCapturedPayload)
# MSP-B2 / AT-653 (T6): per-subscription Azure connector failure health, emitted by
# the Azure Event Connector when a subscription's poll fails so the failure is loud
# in run health. Registered here so the connector can emit it; record_event() raises
# ValueError for an unregistered type, so registration must precede first emission.
register_event_type("ingestion.subscription_health", IngestionSubscriptionHealthPayload)
# R18-C2 / T2 (emission gap-fill): per-skipped-artifact event, emitted at origin
# by the document ingestor (discovery/ingest/documents.py) so the run-health
# dashboard's content panel can report skipped-with-reason volume as an explicit
# org-scoped fact instead of inferring it (the only genuine gap the T1 dashboard
# could not read). Registered here so the ingestor can emit it; record_event()
# raises ValueError for an unregistered type, so registration must precede the
# first emission.
register_event_type("ingestion.artifact_skipped", IngestionArtifactSkippedPayload)
# The connector-agnostic ingestion-completion fact, emitted by the SHARED
# change-based runner so every ChangeBasedIngestor (and every future one) reports
# "Last ingestion" to Run Health without per-connector code. Deliberately NOT
# db.ingestor_completed — see the payload docstring for why that event and the
# other existing candidates are unsuitable. Registered here because record_event()
# raises ValueError for an unregistered type, so registration must precede the
# first emission.
register_event_type("ingestion.completed", IngestionCompletedPayload)
# R16-D1 / AT-366 (T5) — model provider gateway telemetry. Registered here so
# the gateway's generate()/embed() paths can emit them; record_event() raises
# ValueError for an unregistered type, so registration must land before any
# emission call-site (T5-AC5).
register_event_type("model.generation_completed", ModelGenerationCompletedPayload)
register_event_type("model.embedding_completed", ModelEmbeddingCompletedPayload)
# R18-B1 T4 — source-aware retrieval API. retrieval.query_completed is emitted
# once per retrieve() call so semantic retrieval is observable. Registered here so
# app.retrieval.api can emit it; record_event() raises ValueError for an
# unregistered type, so registration must precede the first emission.
register_event_type("retrieval.query_completed", RetrievalQueryCompletedPayload)
# R18-B2 T1 — retrieval freshness. retrieval.artifact_invalidated is emitted once
# per handled ingestion.artifact_changed event so invalidation is observable.
# Registered here so app.retrieval.freshness can emit it; record_event() raises
# ValueError for an unregistered type, so registration must precede emission.
register_event_type("retrieval.artifact_invalidated", RetrievalArtifactInvalidatedPayload)
# R18-B2 T5 — embedding-model-version invalidation + managed backfill.
# retrieval.model_backfill is emitted per org pass that re-embedded old-model
# vectors onto the active model, so a model migration is observable. Registered
# here so app.retrieval.embedder can emit it; record_event() raises ValueError for
# an unregistered type, so registration must precede emission.
register_event_type("retrieval.model_backfill", RetrievalModelBackfillPayload)
# MSP-B12 T3 — Security-Operations evidence-pointer resolution audit. Emitted by
# app/discovery security_ops_evidence_resolver.resolve_evidence_pointer() on every
# resolution attempt (resolved or denied). Registered here so the resolver can
# emit it; record_event() raises ValueError for an unregistered type, so
# registration must precede the first emission.
register_event_type("secops.evidence_pointer_resolved", SecOpsEvidencePointerResolvedPayload)
# R-1.9.1-L2 / T1 (AC1) — usage metering. billing.run_completed is emitted once
# per discovery run (every run, every AI mode) by discovery/runner.py so the L2
# usage report has a complete, immutable record; record_event() raises ValueError
# for an unregistered type, so registration must precede the first emission.
register_event_type("billing.run_completed", BillingRunCompletedPayload)
# R-1.9.1-L2 / T2 (AC2) — the system connect/disconnect billing ledger. Emitted by
# app.billing_ledger from the Integration-Hub connect/disconnect routes on each
# genuine state transition so the L2 usage report has the pro-ration record for
# mid-term system additions/removals; record_event() raises ValueError for an
# unregistered type, so registration must precede the first emission.
register_event_type("billing.system_connected", BillingSystemLedgerPayload)
register_event_type("billing.system_disconnected", BillingSystemLedgerPayload)
# 2.0-A2 T1 — opportunity lifecycle transitions. Registered before the first
# emission site exists: record_event() raises for an unregistered event_type.
register_event_type(
    "opportunity.lifecycle_transitioned", OpportunityLifecycleTransitionPayload
)
# 2.0-A3 T4 — ranking-adjustment recompute/reset governance. Registered before
# the first emission site exists: record_event() raises for an unregistered type.
register_event_type("ranking_adjustment.changed", RankingAdjustmentChangedPayload)


# ---------------------------------------------------------------------------
# Public write API — locked signature (T3-S10-A contract)
# ---------------------------------------------------------------------------

def record_event(event_type: str, payload: Optional[dict] = None) -> None:
    """Fire-and-forget telemetry write.

    Signature is locked: record_event(event_type, payload).
    Track 3 (T3-S10-A) calls this with 2 positional args.

    Raises:
        ValueError: if event_type is not in EVENT_REGISTRY.

    The function:
    1. Logs the event via logger.info so tests can observe it via caplog.
    2. Persists to telemetry_events DB table (best-effort; never raises for DB errors).
    """
    if event_type not in EVENT_REGISTRY:
        raise ValueError(
            f"unregistered event type: '{event_type}'. "
            f"Add it to REGISTERED_EVENT_TYPES before calling record_event()."
        )
    try:
        if payload is None:
            payload = {}

        # Log for observability — Track 3 contract tests read from caplog.
        event_log = {
            "event_type": event_type,
            "ts": time.time(),
            **payload,
        }
        logger.info("[telemetry] %s", event_log)

        # Persist to telemetry_events table.
        _ensure_telemetry_table()

        # Org attribution via the shared resolver (R17-D3 / AT-450 T5-AC1/AC3):
        # the authenticated request org wins; otherwise the payload's org_id
        # (threaded by background emitters — discovery runner, DB ingestors); and
        # an event that can resolve neither is marked UNATTRIBUTED rather than
        # mis-filed under a real tenant.
        try:
            from app.middleware.tenancy import resolve_event_org_id
            org_id = resolve_event_org_id(payload.get("org_id"))
        except Exception:
            # Use the shared sentinel (M3), not a bare "unknown" literal. Guard the
            # import: this branch runs when importing from tenancy may have failed.
            try:
                from app.middleware.tenancy import UNATTRIBUTED_ORG as _unattr
            except Exception:
                _unattr = "_unattributed"
            org_id = payload.get("org_id") or _unattr

        payload_str = json.dumps(payload)

        tel_event = TelemetryEvent(
            id=str(uuid.uuid4()),
            org_id=org_id,
            event_type=event_type,
            source=payload.get("source", "telemetry"),
            run_id=payload.get("run_id"),
            connector_id=payload.get("connector_id"),
            pack_id=payload.get("pack_id"),
            duration_ms=payload.get("duration_ms"),
            success=payload.get("success"),
            count=payload.get("count"),
            error_code=payload.get("error_code"),
            payload=payload_str,
            timestamp=datetime.now(timezone.utc),
        )

        with get_db_session() as session:
            session.add(tel_event)
            session.commit()

    except Exception:
        logger.error(
            "telemetry.record_event failed — event_type=%s\n%s",
            event_type,
            traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Public read API — only approved read path until T1-S15-C.
# ---------------------------------------------------------------------------

_MAX_LIMIT = 10_000


def get_telemetry_range(
    org_id: str,
    event_type: str,
    from_dt: datetime,
    to_dt: datetime,
    limit: int = 1000,
) -> list[Any]:
    """Return telemetry events for a given org, event type, and UTC time range.

    Always scoped to org_id — events from other orgs are never returned.
    Results are ordered oldest-first (timestamp ASC).

    Raises:
        ValueError:         org_id empty/None, from_dt >= to_dt, or limit < 1.
        TelemetryReadError: DB operation failed.
    """
    if not org_id:
        raise ValueError("org_id must not be empty or None")
    if from_dt >= to_dt:
        raise ValueError("from_dt must be strictly before to_dt")
    if limit < 1:
        raise ValueError("limit must be >= 1")

    if limit > _MAX_LIMIT:
        logger.warning(
            "get_telemetry_range: requested limit %d exceeds maximum %d — clamped",
            limit,
            _MAX_LIMIT,
        )
        limit = _MAX_LIMIT

    _ensure_telemetry_table()

    try:
        with get_db_session() as session:
            return (
                session.query(TelemetryEvent)
                .filter(
                    org_id=org_id,
                    event_type=event_type,
                    from_dt=from_dt.isoformat(),
                    to_dt=to_dt.isoformat(),
                )
                .order_by("timestamp")
                .limit(limit)
                .all()
            )
    except TelemetryReadError:
        raise
    except Exception as exc:
        logger.error(
            "get_telemetry_range failed — org_id=%s event_type=%s: %s",
            org_id,
            event_type,
            exc,
        )
        raise TelemetryReadError(str(exc)) from exc


__all__ = [
    "AuditWriteFailedPayload",               # AT-292 / FixPack v2 Fix 5
    "ConnectorHealthPayload",
    "ConnectorRegisteredEvent",
    "DBIngestorCompletedPayload",           # Sprint 11 — SQL Server ingestor payload
    "DbIngestorCompletedEvent",             # T1-S10-C legacy — kept for backward compat
    "DbQueryExecutedEvent",
    "EntityExtractionCompletedPayload",     # T3-S12-A T7
    "RelationshipMappingCompletedPayload",  # T3-S13-A
    "HallucinationGuardRemovedPayload",     # ENT-3 / T3-S15-A
    "HallucinationGuardRewrittenPayload",   # ENT-3 / T3-S15-A
    "LlmEnrichmentGroundedPayload",         # ENT-3 / T3-S15-A
    "CausalHypothesisRejectedPayload",      # ENT-6 / T3-S16-A
    "CausalHypothesisGeneratedPayload",     # ENT-6 / T3-S16-A
    "GraphContextBuiltPayload",             # ENT-4 / T3-S14-A
    "LicenseValidatedPayload",              # LIC-1 / AT-348 (T7)
    "LicenseEnteredGracePayload",           # LIC-1 / AT-348 (T7)
    "LicenseEnteredReadonlyPayload",        # LIC-1 / AT-348 (T7)
    "LicenseUpdatedPayload",                # LIC-1 / AT-348 (T7)
    "LicenseClockAnomalyPayload",           # LIC-1 / AT-348 (T7)
    "IngestionCheckpointResetPayload",      # R16-A1 / AT-383 (T7)
    "IngestionArtifactChangedPayload",      # R16-A1 / AT-381 (T5)
    "IngestionSecretRedactedPayload",       # R18-A2 / AT-531 (T3)
    "ModelGenerationCompletedPayload",      # R16-D1 / AT-366 (T5)
    "ModelEmbeddingCompletedPayload",       # R16-D1 / AT-366 (T5)
    "RetrievalQueryCompletedPayload",       # R18-B1 T4
    "EVENT_PAYLOAD_TYPES",          # AT-211 alias: event_type → TypedDict schema
    "EVENT_REGISTRY",
    "EVENT_TYPE_REGISTRY",          # alias for T1-S10-C unit tests
    "REGISTERED_EVENT_TYPES",       # AT-211 alias: set-like view of registered names
    "RunCompletedEvent",
    "RunSignalSnapshotEvent",
    "OpportunityLifecycleTransitionPayload",  # 2.0-A2 / T1
    "RankingAdjustmentChangedPayload",        # 2.0-A3 / T4
    "RunSignalSnapshotPayload",
    "RunStartedEvent",
    "TELEMETRY_EVENT_REGISTRY",
    "TemporalEnrichmentCompletedPayload",
    "TelemetryReadError",
    "get_telemetry_range",
    "record_event",
    "register_event_type",
]
