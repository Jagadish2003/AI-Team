# LLM Enrichment

## Overview

LLM enrichment is an optional, advisory post-processing step that runs after T2 materialization. It uses the Anthropic API to add narrative context to opportunities and the executive report.

**Key constraint:** LLM enrichment must never mutate scoring fields — `impact`, `effort`, `tier`, `decision`, or `evidenceIds`. It may only add or update advisory fields such as `rationale`, `aiSummary`, `llmLabel`.

---

## Configuration

| Env var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Required for live LLM enrichment. If unset, deterministic fallbacks run instead. |
| `INGEST_MODE` | Set to `offline` to skip all live calls including LLM. |

---

## Where it runs

Enrichment is triggered inside `_run_trackb_and_persist()` in `routes_sprint4_t1.py` after opportunities are scored and persisted. It runs in a background thread, non-blocking.

The enrichment results are stored in run-scoped KV storage:
- `llm_enrichment:{run_id}` — aggregate enrichment summary
- Per-opportunity enrichment is embedded in the opportunity payload

---

## API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/runs/{runId}/llm-enrichment` | Aggregate enrichment summary for a run |
| `GET /api/runs/{runId}/opportunities/{oppId}/enrichment` | Per-opportunity enrichment detail |

Both endpoints return 404 if enrichment has not completed for the run.

---

## Deterministic fallback

When `ANTHROPIC_API_KEY` is not set, `llm_enrichment.py` returns a structured fallback response with empty narrative fields. All scoring and opportunity data remains intact. The run completes normally without LLM enrichment.

---

## Replay behaviour

Replay re-serves stored enrichment artifacts only. It does not re-call the LLM. If a run was completed without `ANTHROPIC_API_KEY`, replay will return the fallback enrichment response.
