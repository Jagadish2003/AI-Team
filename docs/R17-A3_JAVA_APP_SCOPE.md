# R17-A3 — Java Application Ingestion: Phase-One Scope & Boundaries

AgentIQ 2.0 · Release 1.7 · Track A — Connectors & Enterprise Technology
Story: **R17-A3 — Java Application Ingestion (Operational)** · Task **T8** (design review) · Criterion **AC8**

> **Purpose — prevent scope confusion.** This document states, in one place, what
> the Java application ingestion story **does** and **does not** include, and how
> **engineering, QA, and product** should evaluate the work. It is the scope
> contract for the story. For setup/how-to, see
> [`INTEGRATE_JAVA_APP.md`](./INTEGRATE_JAVA_APP.md).

---

## TL;DR

R17-A3 is the **OPERATIONAL phase**. AgentIQ reads what a *running* Java
application reports **about itself** — Spring Boot **Actuator** health/diagnostics
endpoints and **application logs** — and turns runtime friction into discovery
signal.

It does **not** read the application's **source code** (that is the later **1.8
code-and-structure** phase) and it does **not** pull from **external APM /
observability platforms** (Datadog, New Relic, Dynatrace, AppDynamics, and
similar — a possible later extension, not this story).

A Java-app finding is correct when it is grounded in **diagnostics and logs**. It
must **not** be rejected for failing to inspect source code or APM data — those
are out of scope by design.

---

## This is the operational phase

R17-A3 reads the **operational surface** of a running Java enterprise application:
the live behaviour the application exposes about itself while it runs. This is
deliberately lower-risk and faster to value than reading the application's code —
keeping the operational phase separate lets the operational value land first,
while the code-and-structure phase (1.8) is delivered on its own track.

This story is also how AgentIQ 2.0 meets a release-gate criterion of its
definition of done: **discovery across at least one non-SaaS enterprise data
source.** A Java enterprise application is a *custom application*, not a SaaS
product or a database — so the operational surface is exactly the right, bounded
first step.

---

## ✅ In scope (phase one)

| Surface | What AgentIQ reads | What it yields |
| --- | --- | --- |
| **Framework health / diagnostics endpoints** — Spring Boot **Actuator** (`health`, `metrics`, `info`, and similar) | Service health state, runtime metrics (throughput, latency, error rates, resource pressure), application metadata | The live operational behaviour of the running application |
| **Application logs** | Error/exception lines, retry/failure markers, structured log fields (level, logger, exception type) over time | Error patterns, exception clustering, retry/failure signals, process-level friction |

From these two surfaces the ingestor produces **operational SIGNAL** — where a
running Java application shows runtime friction (rising error rates, latency
degradation, recurring exceptions, resource pressure) that an agent could help
address. These signals are **observed** (directly measured), so they are
first-class evidence that can corroborate and elevate findings in other systems.

---

## ❌ Explicitly out of scope (phase one)

### 1. Application source code — reserved for the 1.8 code-and-structure phase

Reading the application's **source to find where the application itself could be
improved** is the separate **Release 1.8 code-and-structure** story. This story
does **not** read any of the following:

* **Repositories** — no cloning, fetching, or checkout of application source.
* **Classes and methods** — no parsing of types, methods, or bodies.
* **Dependency / import graphs** — no build-file or import analysis.
* **Application structure** — no package layout, module graph, or architecture
  inspection.
* **Source-adjacent files** — no reading of source or configuration files for
  their content.

The ingestor has **no code path that reads source.** The only things it touches
are the configured Actuator endpoints and the configured log sources.

### 2. External APM / observability platforms — a possible later extension

This story does **not** ingest from external Application Performance Monitoring or
observability platforms, including **Datadog, New Relic, Dynatrace, AppDynamics**,
and similar systems. Such platforms may be considered in a later phase; they are
**not** part of R17-A3. Operational signal in this story comes only from the
application's **own** Actuator endpoints and logs — not from a third-party
monitoring product sitting in front of it.

---

## How to evaluate a Java-app finding (engineering · QA · product)

The single most common scope error is judging a Java-app finding against
capabilities that belong to a **later** phase. Use this rubric.

### ✅ Accept a finding when…

* It is grounded in **Actuator diagnostics** (health state, error rate, latency,
  throughput, resource pressure) **and/or application logs** (error patterns,
  exception clusters, retry/failure signals).
* Its evidence traces back to a configured Java application via an
  `EvidencePointer` with `source_system='java_app'` and `origin='observed'`.

### 🚫 Do NOT reject a finding because…

* **It did not read the source code.** Source-code analysis is the 1.8 phase —
  out of scope here. A finding is not "incomplete" for relying on operational
  data; that *is* the scope.
* **It did not inspect classes, methods, dependency graphs, or app structure.**
  Same reason — that is the 1.8 code-and-structure phase.
* **It did not pull APM / observability-platform data** (Datadog, New Relic,
  Dynatrace, AppDynamics, …). External APM is explicitly out of scope for this
  phase.

### Legitimate reasons a finding *can* still be challenged

Scope discipline cuts both ways — a finding should be questioned if it is **not**
grounded in the operational surface: e.g. it is not backed by a diagnostics
sample or log evidence, its evidence is stale/out of the corroboration window, or
it claims source/APM grounding (which this phase cannot and does not provide).

---

## Why this discipline matters (the value)

Keeping phase one to the operational surface is **clean delivery discipline**:

* **Focused** — one clear question ("what does the running app report about
  itself?"), not an open-ended "analyse everything about this application."
* **Testable** — operational surfaces (endpoints + logs) have well-defined,
  fixture-able shapes, so acceptance criteria can be proven.
* **Achievable** — bounded, secure (configured, not network-scanned), and
  predictable, so the operational value lands first.

…while still meeting the larger AgentIQ 2.0 goal: **discovering friction from a
non-SaaS enterprise source.**

---

## How the boundary is enforced (not just documented)

The scope is enforced in code and proven by tests, so it cannot silently drift:

* **Operational-only records.** Every record the ingestor emits is either a
  `metrics` sample or a `log` entry — there is no source-code surface.
  See `backend/discovery/ingest/java_app.py`.
* **Configured, not auto-discovered.** Targets come from deployment configuration
  (`JAVA_APP_TARGETS` / the offline fixture); AgentIQ never scans the network for
  apps. See `backend/discovery/ingest/java_app_config.py`.
* **Contract tests (AC8).**
  * `backend/tests/contract/test_java_app_ingestion.py::test_ac8_records_only_describe_operational_surfaces`
  * `backend/tests/contract/test_java_app_ingestion.py::test_ac8_no_record_carries_source_code_fields`
  * `backend/tests/contract/test_java_app_ingestion.py::test_ac8_no_external_apm_or_code_analysis_dependency`
  * `backend/discovery/tests/test_java_app_ingestor.py::test_records_describe_operational_surface_not_source_code`

---

## References

* Story scope, tasks, and acceptance criteria: **R17-A3 — Java Application
  Ingestion (Operational)** (§ Scope — phase one of two; Task T8; AC8).
* Integration / setup guide: [`INTEGRATE_JAVA_APP.md`](./INTEGRATE_JAVA_APP.md).
* Phase two (out of scope here): **Release 1.8 — code-and-structure** reading.
