# R17-A4 — .NET Application Ingestion: Phase-One Scope & Boundaries

AgentIQ 2.0 · Release 1.7 · Track A — Connectors & Enterprise Technology
Story: **R17-A4 — .NET Application Ingestion (Operational)** · Tasks **T2/T8** (design review) · Criteria **AC3, AC8**

> **Purpose — prevent scope confusion.** This document states, in one place, what
> the .NET application ingestion story **does** and **does not** include, how the
> extraction is **shared with Java** (not duplicated), and how **engineering, QA,
> and product** should evaluate the work. It is the scope contract for the story.
> For setup/how-to, see [`INTEGRATE_DOTNET_APP.md`](./INTEGRATE_DOTNET_APP.md).

---

## TL;DR

R17-A4 is the **OPERATIONAL phase** and the **.NET counterpart to R17-A3 (Java)**.
AgentIQ reads what a *running* .NET application reports **about itself** — ASP.NET
Core **health checks** + **EventCounters/diagnostics** and **application logs** —
and turns runtime friction into discovery signal, using the **same signal
extraction** as the Java ingestor.

It does **not** read the application's **source code** (that is the later **1.8
code-and-structure** phase, which pairs Java and .NET again) and it does **not**
pull from **external APM / observability platforms** (Datadog, New Relic,
Dynatrace, AppDynamics, Application Insights, OpenTelemetry collectors, and
similar — out of scope for this phase).

A .NET-app finding is correct when it is grounded in **diagnostics and logs**. It
must **not** be rejected for failing to inspect source code or APM data — those are
out of scope by design.

---

## Share the extraction, not just the idea (AC3)

The defining design constraint of this story is that the .NET ingestor **reuses the
Java ingestor's operational-signal extraction wherever the signal shape is
identical — it does not duplicate it.** Java and .NET differ only at the
**collection edge**; the interpretation of runtime friction is genuinely shared
code.

| Concern | Shared / platform-specific | Location |
| --- | --- | --- |
| Error clustering, latency-degradation detection, throughput decline, resource-pressure flags, recurring-exception clustering, run-level friction rollup | **SHARED** | `backend/discovery/ingest/operational_signals.py` |
| Change-based foundation: opaque checkpoint cursor, delta windowing, resumable batching, provenance stamping, tolerant log-body parsing | **SHARED** | `backend/discovery/ingest/operational_ingest.py` |
| Credential handling: inline-secret rejection, vault-first/env-fallback resolution | **SHARED** | `backend/discovery/ingest/operational_config.py` |
| Which endpoints, which log formats, which native metric/level names | **platform-specific collection** | `dotnet_app*.py` (and `java_app*.py`) |

Because the extraction is shared, AgentIQ explains Java and .NET runtime friction
in **the same signal language**, so downstream scoring, corroboration, and
reporting need no per-technology logic. A future bug fix or improvement to the
extraction lands for both platforms at once and cannot drift between them. This is
pinned by
`backend/discovery/tests/test_operational_signals_shared.py`, which asserts the
extraction functions are the *same objects* (not copies) and that both platforms
produce byte-identical signal for identical records.

---

## This is the operational phase

R17-A4 reads the **operational surface** of a running .NET enterprise application:
the live behaviour the application exposes about itself while it runs. Together with
R17-A3 (Java) it completes the **operational phase** of the enterprise-application
scope; the **1.8 code-and-structure** phase pairs Java and .NET again.

---

## ✅ In scope (phase one)

| Surface | What AgentIQ reads | What it yields |
| --- | --- | --- |
| **Health / diagnostics endpoints** — ASP.NET Core **health checks** + **EventCounters/diagnostics** | Service health state, runtime metrics (throughput, latency, error rates, GC/resource pressure), application metadata | The live operational behaviour of the running application |
| **Application logs** | Error/exception lines, retry/failure markers, structured log fields (level, category, exception type) over time | Error patterns, exception clustering, retry/failure signals, process-level friction |

From these two surfaces the ingestor produces **operational SIGNAL** — where a
running .NET application shows runtime friction (rising error rates, latency
degradation, recurring exceptions, resource pressure) that an agent could help
address. These signals are **observed** (directly measured), so they are
first-class evidence that can corroborate and elevate findings in other systems.

---

## ❌ Explicitly out of scope (phase one)

### 1. Application source code — reserved for the 1.8 code-and-structure phase

Reading the application's **source to find where the application itself could be
improved** is the separate **Release 1.8 code-and-structure** story (paired with
Java). This story does **not** read repositories, classes/methods, dependency or
import graphs, application structure, or source/config files for their content. The
ingestor has **no code path that reads source or IL** — the only things it touches
are the configured health/diagnostics endpoints and the configured log sources.

### 2. External APM / observability platforms — a possible later extension

This story does **not** ingest from external Application Performance Monitoring or
observability platforms, including **Datadog, New Relic, Dynatrace, AppDynamics,
Azure Application Insights**, OpenTelemetry collectors, and similar systems.
Operational signal in this story comes only from the application's **own** health
checks, EventCounters, and logs — not from a third-party monitoring product sitting
in front of it.

---

## How to evaluate a .NET-app finding (engineering · QA · product)

### ✅ Accept a finding when…

* It is grounded in **health checks / EventCounters** (health state, error rate,
  latency, throughput, GC/resource pressure) **and/or application logs** (error
  patterns, exception clusters, retry/failure signals).
* Its evidence traces back to a configured .NET application via an
  `EvidencePointer` with `source_system='dotnet_app'` and `origin='observed'`.
* It uses the **shared** signal language (the same friction reasons and rollup
  shape as a Java finding) — that is by design, not a defect.

### 🚫 Do NOT reject a finding because…

* **It did not read the source code.** Source-code analysis is the 1.8 phase — out
  of scope here.
* **It did not inspect classes, methods, dependency graphs, or app structure.**
  Same reason — the 1.8 code-and-structure phase.
* **It did not pull APM / observability-platform data** (Datadog, New Relic,
  Dynatrace, AppDynamics, Application Insights, …). External APM is explicitly out
  of scope for this phase.
* **Its extraction logic looks identical to Java's.** That is the point of AC3 —
  the extraction is shared, not duplicated.

### Legitimate reasons a finding *can* still be challenged

A finding should be questioned if it is **not** grounded in the operational
surface: e.g. it is not backed by a diagnostics sample or log evidence, its
evidence is stale/out of the corroboration window, or it claims source/APM
grounding (which this phase cannot and does not provide).

---

## How the boundary is enforced (not just documented)

* **Operational-only records.** Every record the ingestor emits is either a
  `metrics` sample or a `log` entry — there is no source-code surface.
  See `backend/discovery/ingest/dotnet_app.py`.
* **Configured, not auto-discovered.** Targets come from deployment configuration
  (`DOTNET_APP_TARGETS` / the offline fixture); AgentIQ never scans the network for
  apps. See `backend/discovery/ingest/dotnet_app_config.py`.
* **Shared extraction (AC3).**
  `backend/discovery/tests/test_operational_signals_shared.py` and
  `backend/tests/contract/test_dotnet_app_ingestion.py::test_ac3_*`.
* **Operational-only / no-APM contract tests (AC8).**
  * `backend/tests/contract/test_dotnet_app_ingestion.py::test_ac8_records_only_describe_operational_surfaces`
  * `backend/tests/contract/test_dotnet_app_ingestion.py::test_ac8_no_record_carries_source_code_fields`
  * `backend/tests/contract/test_dotnet_app_ingestion.py::test_ac8_no_external_apm_or_code_analysis_dependency`
  * `backend/discovery/tests/test_dotnet_app_ingestor.py::test_records_describe_operational_surface_not_source_code`

---

## References

* Story scope, tasks, and acceptance criteria: **R17-A4 — .NET Application
  Ingestion (Operational)** (§ Scope — phase one of two; Tasks T2/T8; AC3, AC8).
* Integration / setup guide: [`INTEGRATE_DOTNET_APP.md`](./INTEGRATE_DOTNET_APP.md).
* The Java counterpart: [`R17-A3_JAVA_APP_SCOPE.md`](./R17-A3_JAVA_APP_SCOPE.md).
* Phase two (out of scope here): **Release 1.8 — code-and-structure** reading.
