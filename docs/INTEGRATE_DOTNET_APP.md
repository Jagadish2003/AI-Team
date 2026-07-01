# R17-A4 — .NET Application Ingestion → Corroboration (Operational)

AgentIQ 2.0 · Release 1.7 · Track A — Connectors & Enterprise Technology

.NET application ingestion is the **.NET counterpart to R17-A3 (Java)**. This
increment connects **.NET operational signals into the existing cross-system
corroboration flow** so that a running .NET application's runtime friction can
**support and strengthen** a finding that already exists in another system.

The value: AgentIQ moves from *"a ticket system shows a problem"* to *"the ticket
system **and** the actual application runtime both show the same problem"* — a more
credible finding.

---

## What corroboration means here (the worked example)

If **ServiceNow** shows an incident spike for a service, and the **.NET
application** shows a matching rise in errors or latency for the same service, the
two signals **corroborate** and the finding's confidence is elevated
(MEDIUM → HIGH). Concretely, both `COR-01` (ServiceNow) and `COR-10` (.NET) fire.

`COR-10` is the .NET rule and the direct counterpart to Java's `COR-09`.

---

## The .NET signal is shaped so the engine can understand it (AC5)

The corroboration engine reads a single, well-defined block. The .NET signal is
shaped to carry everything the engine needs:

| The engine needs… | …and the .NET signal provides |
| --- | --- |
| **Source system** | the payload is keyed under `dotnet_app`; every record's `source_system` and evidence pointer say `dotnet_app` |
| **Application identity** | the per-service rollup, keyed by `service` / `app_id` |
| **Signal type** | the friction `reasons` (elevated error rate, latency degradation, throughput decline, resource pressure, recurring exception cluster, unhealthy health check) |
| **Timestamp** | `operational_friction.timestamp` (the engine windows it — 30 days) |
| **Confidence-related data** | `operational_friction.fired` plus the per-service `metrics` gauges (error rate, latency, heap/CPU pressure, …) |
| **Provenance** | a fully-populated **OBSERVED** `EvidencePointer` on every underlying record |

Because the signal is read **directly** from the running application's logs and
diagnostics, it is **first-class observed evidence** (`origin='observed'`, no
`extraction_job_id`) — never inferred. That is what lets it count as first-class
support for a related finding.

The corroboration payload is built by
`backend/discovery/ingest/dotnet_app_signals.py`
(`build_dotnet_app_corroboration_payload`), keyed under `dotnet_app`.

---

## No separate .NET confidence model (AC6)

The .NET evidence **plugs into the same cross-system corroboration approach** used
by ServiceNow, Jira, and the Java source — it does **not** introduce a new
confidence mechanism. `COR-10` is an **elevating, observed-evidence** corroborator
(like `COR-01`/`COR-02`/`COR-09`), subject to the same rules:

* It only *feeds* the signal; the engine decides whether (and how much) it elevates.
* The **single-source ceiling still applies**: a run with only `dotnet_app`
  connected has no finding to corroborate and stays MEDIUM (`COR-08`). HIGH is
  reached only when the .NET signal **co-fires with the finding's own (non-.NET)
  source** — i.e. two independent systems agree.
* It is **not** subject to the Slack/Teams conversation-source ceiling (`COR-05`),
  because operational signals are directly measured, not inferred conversation.

The friction *interpretation* is **reused from the Java ingestor**
(`java_app_signals.build_java_app_signal`, whose output block is platform-agnostic)
rather than re-implemented, so both enterprise-application sources speak the same
signal language and corroborate uniformly.

---

## Implementation map

| Concern | File |
| --- | --- |
| .NET signal shaping (provenance + corroboration payload) | `backend/discovery/ingest/dotnet_app_signals.py` |
| .NET ingestor (produces the observed records) | `backend/discovery/ingest/dotnet_app.py` |
| Per-deployment config + vault credential resolution | `backend/discovery/ingest/dotnet_app_config.py` |
| Offline fixture | `backend/discovery/ingest/fixtures/dotnet_app_sample.json` |
| Corroboration rule **COR-10** | `backend/discovery/packs/corroboration_rules.py`, `backend/app/corroboration_engine.py` |
| Runner wiring (`_ingest_dotnet_app_corroboration`) | `backend/discovery/runner.py` |
| Live-credential wiring (`_resolve_dotnet_app`) | `backend/app/live_ingest_credentials.py` |
| Tests | `backend/discovery/tests/test_dotnet_app_*.py`, `backend/tests/contract/test_dotnet_app_corroboration.py` |

### Acceptance criteria → tests

| AC | Where it is verified |
| --- | --- |
| AC5 every .NET signal carries a valid OBSERVED EvidencePointer; corroboration-ready shape | `test_dotnet_app_evidence_pointer.py`, `test_dotnet_app_corroboration.py::test_ac5_*` |
| AC6 a .NET signal corroborates another system and contributes to confidence | `discovery/tests/test_dotnet_app_corroboration.py`, `tests/contract/test_dotnet_app_corroboration.py::test_ac6_*` |

---

## Running a .NET app source live

1. Configure the in-scope applications in `DOTNET_APP_TARGETS` (JSON array, no
   secrets — each target names a `diagnostics_url` + `log_source`).
2. Store the credential in the vault under the `dotnet_app` connector key (env
   fallback `DOTNET_APP_TOKEN` for CLI use).
3. Run discovery with `dotnet_app` among the connected systems and
   `INGEST_MODE=live`. When a finding in another connected system matches a service
   showing .NET operational friction, `COR-10` corroborates it.

Offline mode needs none of the above — it reads the fixture deterministically.
