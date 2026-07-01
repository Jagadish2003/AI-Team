# R17-A4 — .NET Application Ingestion (Operational)

AgentIQ 2.0 · Release 1.7 · Track A — Connectors & Enterprise Technology

.NET application ingestion is the **.NET counterpart to R17-A3 (Java)**, completing
the operational phase of the Java/.NET enterprise-application scope. It reads the
*operational surface* of a running .NET enterprise application — its
health/diagnostics surface (ASP.NET Core health checks + EventCounters) and its
application logs — and produces operational SIGNAL where the application shows
runtime friction an agent could help address.

This document is the design-review reference for the story (T8). It states the
phase-one scope explicitly, points to the implementation, and — most importantly —
records how the .NET ingestor **reuses the Java ingestor's operational-signal
extraction rather than duplicating it** (AC3).

---

## Shared extraction, platform-specific collection (AC3 — READ THIS FIRST)

The whole point of building Java and .NET as a matched pair is that the
*interpretation* of runtime friction is identical across platforms — only the
*collection* differs. AgentIQ therefore explains Java and .NET runtime friction in
**exactly the same signal language**, so downstream scoring, corroboration, and
reporting need no per-technology logic.

| Layer | Shared or platform-specific? | Where |
| --- | --- | --- |
| Signal **interpretation** — error clustering, latency-degradation detection, throughput decline, resource pressure, exception clustering, the friction rollup | **SHARED** (one implementation, reused verbatim) | `backend/discovery/ingest/operational_signals.py` |
| Change-based **foundation** — opaque per-app checkpoint cursor, delta windowing, resumable batch streaming, provenance stamping, tolerant log-body parsing | **SHARED** | `backend/discovery/ingest/operational_ingest.py` |
| Credential handling — secret-in-config rejection, vault-first/env-fallback resolution | **SHARED** | `backend/discovery/ingest/operational_config.py` |
| **Collection edge** — which endpoints (Actuator vs health-checks + EventCounters), which log formats, which native metric/level names | **platform-specific** | `java_app*.py` / `dotnet_app*.py` |

`DotNetAppIngestor` subclasses the shared `OperationalChangeIngestor` and supplies
only the collection edge; `dotnet_app_signals.py` is a thin adapter that binds the
`dotnet_app` identity onto the **same** shared extraction functions Java uses. A
future fix to the extraction therefore cannot drift between the two platforms — a
`discovery/tests/test_operational_signals_shared.py` test pins that both platforms
produce byte-identical signal for identical records, and that the extraction
functions are the *same objects*, not copies.

---

## Scope — phase one of two

R17-A4 is the **OPERATIONAL** phase. It reads what a running .NET application
reports *about itself*.

### In scope (phase one)

| Surface | What it yields |
| --- | --- |
| Health/diagnostics endpoints (ASP.NET Core health checks + EventCounters/diagnostics) | Service health state, runtime metrics (throughput, latency, error rates, GC/resource pressure), application metadata — the live operational behaviour. |
| Application logs | Error patterns, exception clustering, retry/failure signals, process-level friction visible over time. |

### Explicitly OUT of scope (phase one) — AC8

* **Application SOURCE CODE.** Reading the application's source is the **separate
  1.8 code-and-structure phase** (which pairs Java and .NET again). This story does
  **not** clone repositories, parse source/IL, or read configuration files. The
  ingestor has no code path that reads source.
* **External APM / observability-platform data** (Datadog, New Relic, Dynatrace,
  Application Insights, OpenTelemetry collectors, etc.). Out of scope for phase one.

The code enforces this boundary: every record the ingestor emits is either a
`metrics` sample or a `log` entry, and contract/discovery tests assert that no
record carries source-code/repository fields and that the modules reference no
external-APM or source/IL-parser dependency.

---

## Configured, not auto-discovered (security & deployment control)

AgentIQ does **not** scan the network to find .NET apps. Each customer deployment
**explicitly configures** which applications are in scope.

Each target declares (non-secret) configuration only:

| Field | Meaning |
| --- | --- |
| `app_id` | Stable identity; the artifact-id prefix and per-app checkpoint key. |
| `name` | Human-readable application name. |
| `diagnostics_url` | Base URL of the .NET health/diagnostics surface (health checks + EventCounters). |
| `log_source` | Where the application's logs are read from (path or endpoint). |
| `metadata` | Non-secret service metadata (`service`, `environment`, owning team) used to link the signal back to the right service for corroboration. |
| `credential_ref` | A **reference** (vault key) naming where the secret lives — **never the secret itself**. |

* **Offline (default):** targets are read from the deterministic fixture
  `backend/discovery/ingest/fixtures/dotnet_app_sample.json`, so the whole pipeline
  runs without any credentials.
* **Live (`INGEST_MODE=live`):** targets are read from the `DOTNET_APP_TARGETS`
  environment variable — a JSON array of target configs, configured per deployment.

### Credentials — vault only, never in config or logs (AC4)

Secrets **must not** appear in target configuration, in logs, or in code. The
configuration carries only a `credential_ref`; the secret is resolved at ingest
time from the **credential vault** via the per-run credential context (the same
shared resolver Java uses — `operational_config.resolve_target_secret`), with an
env var (`DOTNET_APP_TOKEN`) as a CLI/standalone fallback. `load_targets()`
**rejects** any target entry carrying an inline secret-looking field, so a pasted
credential surfaces as a rejected target rather than a silently-persisted plaintext
secret.

---

## Built on the change-based foundation (R16-A1)

`DotNetAppIngestor` implements the `ChangeBasedIngestor` contract via the shared
`OperationalChangeIngestor`. .NET operational data is inherently incremental — logs
are read forward from a position, EventCounters are sampled over time — so the
connector encodes its read position as the **opaque checkpoint**: a per-app
`{log_offset, metrics_ts, metrics_seq}` map (identical to Java). Each run processes
only new operational data; an idle application yields an empty (or minimal) delta.
The shared change runner owns the checkpoint lifecycle and emits one
`ingestion.artifact_changed` event per changed operational artifact.

The one genuinely .NET-specific bit of log handling is **LogLevel normalisation**:
.NET's `Microsoft.Extensions.Logging`/Serilog levels (`Critical`, `Warning`,
`Information` …) are normalised onto the shared canonical vocabulary so the shared
error/exception extraction reads a .NET log identically to a Java one.

---

## Signal, provenance, and corroboration

* **Provenance (R16-B1):** every operational signal carries an `EvidencePointer`
  with `source_system='dotnet_app'`, the application/endpoint artifact id, a
  timestamp, and `origin='observed'`. Operational signals are directly measured, so
  they are **first-class observed evidence — not inferred**.
* **Corroboration (COR-10):** a .NET-app operational signal can corroborate a
  finding in another connected system — e.g. a rising error rate in a .NET service
  corroborating a spike in ServiceNow incidents for the same service — and
  **elevates confidence**, exactly as Java's COR-09 does. Because the signal is
  observed (not inferred), COR-10 is an *elevating* rule (unlike the Slack-only
  ceiling, COR-05). A run with only `dotnet_app` connected has no finding to
  corroborate and stays at MEDIUM (single-source, COR-08).

---

## Implementation map

| Concern | File |
| --- | --- |
| Ingestor (health/EventCounters + logs, incremental) | `backend/discovery/ingest/dotnet_app.py` |
| Per-deployment config + vault credential resolution | `backend/discovery/ingest/dotnet_app_config.py` |
| .NET signal adapter (evidence pointer + corroboration payload) | `backend/discovery/ingest/dotnet_app_signals.py` |
| **Shared** operational-signal extraction | `backend/discovery/ingest/operational_signals.py` |
| **Shared** change-ingestion base (cursor + orchestration + log parsing) | `backend/discovery/ingest/operational_ingest.py` |
| **Shared** credential/secret primitives | `backend/discovery/ingest/operational_config.py` |
| Offline fixture | `backend/discovery/ingest/fixtures/dotnet_app_sample.json` |
| Corroboration rule COR-10 | `backend/discovery/packs/corroboration_rules.py`, `backend/app/corroboration_engine.py` |
| Runner wiring (`_ingest_dotnet_app_corroboration`) | `backend/discovery/runner.py` |
| Live-credential wiring (`_resolve_dotnet_app`) | `backend/app/live_ingest_credentials.py` |
| Tests | `backend/discovery/tests/test_dotnet_app_*.py`, `backend/tests/contract/test_dotnet_app_ingestion.py` |

### Acceptance criteria → tests

| AC | Where it is verified |
| --- | --- |
| AC1 reads endpoints + logs, produces signal | `test_dotnet_app_ingestor.py`, `test_dotnet_app_ingestion.py` |
| AC2 incremental; idle yields empty delta | `test_dotnet_app_ingestor.py` |
| AC3 shared extraction reused, not duplicated | `test_operational_signals_shared.py`, `test_dotnet_app_ingestion.py::test_ac3_*` + this document |
| AC4 configured per deployment; vault credentials, never logged | `test_dotnet_app_config.py` |
| AC5 valid observed EvidencePointer on every signal | `test_dotnet_app_evidence_pointer.py` |
| AC6 .NET-app signal corroborates another system, contributes to confidence | `test_dotnet_app_corroboration.py` |
| AC7 changed artifacts emit `ingestion.artifact_changed` | `test_dotnet_app_artifact_changed_events.py` |
| AC8 operational surfaces only — no source code, no external APM | `test_dotnet_app_ingestor.py::test_records_describe_operational_surface_not_source_code`, `test_dotnet_app_ingestion.py::test_ac8_*` + this document |

---

## Running a .NET app source live

1. Configure the in-scope applications in `DOTNET_APP_TARGETS` (JSON array, no
   secrets).
2. Store the credential in the vault under the `dotnet_app` connector key (or the
   per-target `credential_ref`).
3. Run discovery with `dotnet_app` among the connected systems and
   `INGEST_MODE=live`.

Offline mode needs none of the above — it reads the fixture deterministically.
