# R17-A3 — Java Application Ingestion (Operational)

AgentIQ 2.0 · Release 1.7 · Track A — Connectors & Enterprise Technology

Java application ingestion is **AgentIQ's first non-SaaS enterprise source**. It
reads the *operational surface* of a running Java enterprise application — its
framework health/diagnostics endpoints (Spring Boot Actuator) and its application
logs — and produces operational SIGNAL where the application shows runtime
friction an agent could help address.

This document is the design-review reference for the story (T8). It states the
phase-one scope explicitly and points to the implementation.

---

## Scope — phase one of two (READ THIS FIRST)

R17-A3 is the **OPERATIONAL** phase. It reads what a running Java application
reports *about itself*.

### In scope (phase one)

| Surface | What it yields |
| --- | --- |
| Framework health/diagnostics endpoints (Spring Boot Actuator: `health`, `metrics`, `info`) | Service health state, runtime metrics (throughput, latency, error rates, resource pressure), application metadata — the live operational behaviour. |
| Application logs | Error patterns, exception clustering, retry/failure signals, process-level friction visible over time. |

### Explicitly OUT of scope (phase one) — AC8

* **Application SOURCE CODE.** Reading the application's source to find where the
  application itself could be improved is the **separate 1.8 code-and-structure
  phase**. This story does **not** clone repositories, parse source/ASTs, or read
  configuration files. The ingestor has no code path that reads source.
* **External APM / observability-platform data** (Datadog, New Relic, Dynatrace,
  etc.). A possible later extension — not phase one.

The operational/code split is deliberate: reading what a running application
reports about itself is lower-risk and faster to value than reading its code.
Keeping the two phases separate lets the operational value land first.

The code enforces this boundary: every record the ingestor emits is either a
`metrics` sample or a `log` entry, and a contract test
(`test_java_app_ingestor.py::test_records_describe_operational_surface_not_source_code`)
asserts that no record carries source-code/repository fields.

---

## Configured, not auto-discovered (security & deployment control)

AgentIQ does **not** scan the network to find Java apps. Each customer deployment
**explicitly configures** which applications are in scope. This keeps phase one
bounded, secure, and predictable — the customer decides which applications
AgentIQ is allowed to read, and AgentIQ only reads those configured sources.

Each target declares (non-secret) configuration only:

| Field | Meaning |
| --- | --- |
| `app_id` | Stable identity; the artifact-id prefix and per-app checkpoint key. |
| `name` | Human-readable application name. |
| `actuator_url` | Base URL of the Spring Boot Actuator endpoint. |
| `log_source` | Where the application's logs are read from (path or endpoint). |
| `metadata` | Non-secret service metadata (`service`, `environment`, owning team) used to link the signal back to the right service for corroboration. |
| `credential_ref` | A **reference** (vault key) naming where the secret lives — **never the secret itself**. |

* **Offline (default):** targets are read from the deterministic fixture
  `backend/discovery/ingest/fixtures/java_app_sample.json`, so the whole pipeline
  runs without any credentials.
* **Live (`INGEST_MODE=live`):** targets are read from the `JAVA_APP_TARGETS`
  environment variable — a JSON array of target configs, configured per
  deployment.

### Credentials — vault only, never in config or logs (AC3)

Passwords, tokens, API keys, and basic-auth values **must not** appear in target
configuration, in logs, or in code. The configuration carries only a
`credential_ref`; the secret is resolved at ingest time from the **credential
vault** via the per-run credential context (the same mechanism the SaaS
connectors use — `discovery.ingest.get_live_connector`), with an env var
(`JAVA_APP_TOKEN`) as a CLI/standalone fallback. The resolved secret is handed
straight to the HTTP/log client and is never attached to the target, logged, or
echoed.

To make this enforceable rather than merely documented, `load_targets()`
**rejects** any target entry that carries an inline secret-looking field
(`password`/`token`/`secret`/`api_key`/`basic_auth`/…) — a misconfigured
deployment that pastes a credential into config surfaces as a rejected target
rather than silently persisting a plaintext secret.

---

## Built on the change-based foundation (R16-A1)

`JavaAppIngestor` implements the `ChangeBasedIngestor` contract. Java operational
data is inherently incremental — logs are read forward from a position, metrics
endpoints are sampled over time — so the connector encodes its read position as
the **opaque checkpoint**: a per-app `{log_offset, metrics_ts}` map. Each run
processes only new operational data; an idle application yields an empty (or
minimal) delta. The shared change runner owns the checkpoint lifecycle and emits
one `ingestion.artifact_changed` event per changed operational artifact.

---

## Signal, provenance, and corroboration

* **Provenance (R16-B1):** every operational signal carries an `EvidencePointer`
  with `source_system='java_app'`, the application/endpoint artifact id, a
  timestamp, and `origin='observed'`. Operational signals are directly measured,
  so they are **first-class observed evidence — not inferred**.
* **Corroboration (COR-09):** a Java-app operational signal can corroborate a
  finding in another connected system — e.g. a rising error rate in a Java
  service corroborating a spike in ServiceNow incidents for the same service —
  and **elevates confidence**, exactly as cross-system corroboration does
  elsewhere. Because the signal is observed (not inferred), COR-09 is an
  *elevating* rule (unlike the Slack-only ceiling, COR-05).

---

## Implementation map

| Concern | File |
| --- | --- |
| Ingestor (Actuator + logs, incremental) | `backend/discovery/ingest/java_app.py` |
| Per-deployment config + vault credential resolution | `backend/discovery/ingest/java_app_config.py` |
| Operational signal extraction + evidence pointer + corroboration payload | `backend/discovery/ingest/java_app_signals.py` |
| Offline fixture | `backend/discovery/ingest/fixtures/java_app_sample.json` |
| Corroboration rule COR-09 | `backend/discovery/packs/corroboration_rules.py`, `backend/app/corroboration_engine.py` |
| Runner wiring (`_ingest_java_app_corroboration`) | `backend/discovery/runner.py` |
| Live-credential wiring (`_resolve_java_app`) | `backend/app/live_ingest_credentials.py` |
| Tests | `backend/discovery/tests/test_java_app_*.py` |

### Acceptance criteria → tests

| AC | Where it is verified |
| --- | --- |
| AC1 reads endpoints + logs, produces signal | `test_java_app_ingestor.py` |
| AC2 incremental; idle yields empty delta | `test_java_app_ingestor.py` |
| AC3 configured per deployment; vault credentials, never logged | `test_java_app_config.py` |
| AC4 valid observed EvidencePointer on every signal | `test_java_app_evidence_pointer.py` |
| AC5 Java-app signal corroborates another system, contributes to confidence | `test_java_app_corroboration.py` |
| AC6 changed artifacts emit `ingestion.artifact_changed` | `test_java_app_artifact_changed_events.py` |
| AC7 discovery finding grounded in Java operational data | `test_java_app_corroboration.py::test_ac7_discovery_finding_grounded_in_java_operational_data` |
| AC8 operational surfaces only — no source code | `test_java_app_ingestor.py::test_records_describe_operational_surface_not_source_code` + this document |

---

## Running a Java app source live

1. Configure the in-scope applications in `JAVA_APP_TARGETS` (JSON array, no
   secrets).
2. Store the credential in the vault under the `java_app` connector key (or the
   per-target `credential_ref`).
3. Run discovery with `java_app` among the connected systems and
   `INGEST_MODE=live`.

Offline mode needs none of the above — it reads the fixture deterministically.
