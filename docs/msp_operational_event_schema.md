# MSP Operational Event Schema (MSP-B0)

The **Operational Event Schema** is the single normalised shape every MSP
cloud-event source maps onto before an event reaches a detector. AWS (MSP-B1),
Azure (MSP-B2), and the Event History export bridge (MSP-B8) each translate their
provider-native payload into an `OperationalEvent`, so detectors, scoring,
corroboration, and reporting see **one** contract and never a provider-specific
payload.

Code: [`backend/discovery/signals/operational_event.py`](../backend/discovery/signals/operational_event.py)
and [`backend/discovery/signals/event_signature.py`](../backend/discovery/signals/event_signature.py).

This schema is a **profile of the common signal model** — it reuses the R16-B1
`EvidencePointer` provenance spine (`backend/app/provenance.py`) rather than
inventing new source-tracking.

---

## 1. Model

### `CommonSignal` (the spine)

| Field | Type | Notes |
|-------|------|-------|
| `org_id` | `str` | Tenancy scoping — every signal is org-scoped. Required. |
| `source_system` | `str` | `aws` \| `azure` \| `event_bridge` \| … |
| `signal_id` | `str` | Stable id **within** the source system. Per-occurrence. |
| `observed_at` | `str` | UTC ISO-8601 observation time. |
| `provenance` | `dict` | A valid **OBSERVED** `EvidencePointer` spine. |

### `OperationalEvent(CommonSignal)` (the profile)

| Field | Type | Notes |
|-------|------|-------|
| `resource_type` | `str` | Normalised — must be in `RESOURCE_TYPES`. |
| `event_class` | `str` | Normalised — must be in `EVENT_CLASSES`. |
| `severity` | `str` | Normalised — must be in `SEVERITY_LEVELS`. |
| `event_type` | `str` | Provider-native event name, preserved for trace-back. |
| `resource` | `ResourceRef?` | The cloud resource the event concerns. |
| `message` | `str?` | Optional human-readable summary. |
| `payload` | `dict` | Free-form provider detail (e.g. `principal` for access events). |
| `event_signature` | `str` | Deterministic recurrence fingerprint (see §3). Auto-derived. |

### `ResourceRef`

`provider`, `resource_type` (normalised), `resource_id` (provider-native ARN /
resource URI, kept verbatim), optional `region` and `name`.

---

## 2. Normalised vocabularies (T1-AC1)

Closed frozen sets — an out-of-vocabulary value fails at construction. Providers
map their native taxonomy on with the `normalize_*` helpers.

- **`resource_type`** — `compute`, `container`, `serverless`, `storage`,
  `database`, `network`, `identity`, `messaging`, `monitoring`, `security`, `other`.
- **`event_class`** — `lifecycle`, `configuration`, `state_change`, `access`,
  `error`, `performance`, `security`, `audit`, `other`.
- **`severity`** — `critical` > `high` > `medium` > `low` > `info` (rank via
  `SEVERITY_ORDER`).

---

## 3. `event_signature` construction rules (AT-636 — the mapping contract)

`event_signature` is a deterministic fingerprint that uniquely identifies a
**recurring** operational event. It is the join key for **deduplication,
recurrence detection, and hotspot correlation**.

**Format:** `"{VERSION}:{sha256_128bit_hex}"` (e.g. `1:9f2c…`).
`EVENT_SIGNATURE_VERSION` is bumped whenever the recipe or a normalisation rule
changes, so signatures from different rule versions never compare equal.

> **One signature discipline across the pack.** `event_signature` (B0, this
> section) and the ITSM `resolution_signature` / `incident_identity_signature`
> (MSP-B4) share the same rules — deterministic, explainable, tested,
> conservative, versioned. The B4 contract is documented alongside this one in
> [`docs/msp_resolution_signature.md`](msp_resolution_signature.md).

### Deliberately excluded from the signature

So that repeated occurrences of one recurring event collapse to a single
signature (**T2-AC1**):

- `observed_at` / `source_timestamp` — recurrence is over time.
- `signal_id` — per-occurrence id.
- `severity` — the same fault may escalate/de-escalate between occurrences.
- `message` and free-form `payload` (except discriminators a per-class rule names).

### Components (ordered)

Every signature begins with the always-present prefix, then appends the
per-class discriminators:

```
[ provider_family, event_class, resource_type ] + <per-class recipe>
```

**Per provider family** (resolved from `source_system`) — governs how the
provider-native `event_type` is folded to one canonical token:

| Family | `source_system` examples | `event_type` normalisation |
|--------|--------------------------|----------------------------|
| `aws` | `aws`, `aws_cloudtrail`, `aws_cloudwatch` | lower-case, whitespace runs → `_` (e.g. `EC2 Instance State-change Notification` → `ec2_instance_state-change_notification`) |
| `azure` | `azure`, `azure_monitor`, `azure_activity` | lower-case, `/` path preserved (e.g. `Microsoft.Compute/virtualMachines/write` → `microsoft.compute/virtualmachines/write`) |
| `event_bridge` | `event_bridge`, `event_history_bridge` | lower-case, whitespace runs → `_` (bridge forwards a canonical token) |
| `generic` | anything else | lower-case, whitespace runs → `_` |

**Per event class** — governs which discriminators participate:

| Event class | Recipe (after prefix) |
|-------------|-----------------------|
| `lifecycle`, `configuration`, `state_change`, `error`, `performance`, `other` | `event_type`, `resource` |
| `access`, `audit`, `security` | `event_type`, `resource`, `principal` |

- `resource` = the resource id (ARN / resource URI) lower-cased & trimmed; empty
  when the event concerns no specific resource. Distinct resources ⇒ distinct
  signatures — this is what drives **hotspot correlation**.
- `principal` = the acting identity, read from `payload` under `principal` /
  `actor` / `user` / `user_identity` / `caller`. The same action by two
  different principals is two different events (**T2-AC2**).

### Guarantees

- **Deterministic** (T2-AC1): pure function of the components — no clock, no
  randomness. Repeated occurrences ⇒ identical signature.
- **Unique** (T2-AC2): any change to a participating component (resource,
  event_type, event_class, provider family, principal) changes the signature.
- **Documented** (T2-AC3): this section; `signature_components()` returns the
  resolved recipe + components for any inputs, for audit/debugging.
- **Fixture-validated** (T2-AC4): provider-specific fixtures in
  [`backend/discovery/tests/fixtures/msp_event_signatures.json`](../backend/discovery/tests/fixtures/msp_event_signatures.json)
  assert recurring occurrences collapse and distinct events differ.

---

## 4. Usage

```python
from discovery.signals import OperationalEvent, ResourceRef

event = OperationalEvent.build(
    org_id="acme",
    source_system="aws",
    signal_id="aws-evt-0001",
    event_type="EC2 Instance State-change Notification",
    event_class="stop",          # normalised -> "lifecycle"
    severity="Warning",          # normalised -> "medium"
    resource=ResourceRef(provider="aws", resource_type="compute",
                         resource_id="arn:aws:ec2:us-east-1:...:instance/i-abc"),
)
# event.event_signature is deterministic and stable across recurrences.
```

`build()` normalises provider-native tokens and mints the OBSERVED provenance
pointer; construct `OperationalEvent(...)` directly when the caller already holds
canonical values.

---

## 5. Provider mapping contract (AT-637 — for connector implementers)

A **mapper** converts one raw provider payload into an `OperationalEvent`. The
reference mappers in
[`backend/discovery/signals/reference_mappers.py`](../backend/discovery/signals/reference_mappers.py)
are the executable contract: a connector implementer (B1 AWS, B2 Azure, B8 export
bridge) either reuses them or matches their behaviour. They run over **golden
fixtures** ([`msp_provider_mapping_golden.json`](../backend/discovery/tests/fixtures/msp_provider_mapping_golden.json)),
not live connections.

Every mapper resolves the same target fields and calls `OperationalEvent.build()`,
so **all providers emit the identical detector-visible structure** — a detector
never branches on provider (T3-AC3).

### Reference mappers

| Mapper | Provider surface | `source_system` | family |
|--------|------------------|-----------------|--------|
| `map_cloudwatch` | CloudWatch alarm state change | `aws_cloudwatch` | aws |
| `map_eventbridge` | EventBridge event (e.g. EC2 state change) | `aws` | aws |
| `map_cloudtrail` | CloudTrail management/API record | `aws_cloudtrail` | aws |
| `map_azure_monitor` | Azure Monitor common-alert-schema alert | `azure_monitor` | azure |
| `map_azure_activity_log` | Azure Activity Log administrative record | `azure_activity` | azure |
| `map_service_health` | Azure Service Health event (service issue / maintenance / advisory) | `azure_service_health` | azure |

### Field-by-field mapping

| Schema field | CloudWatch | EventBridge | CloudTrail | Azure Monitor | Azure Activity Log |
|--------------|-----------|-------------|-----------|---------------|--------------------|
| `signal_id` | `id` | `id` | `eventID` | `data.essentials.alertId` | `eventDataId` \| `correlationId` |
| `event_type` | `detail-type` | `detail-type` | `eventName` | `essentials.alertRule` \| `signalType` | `operationName` |
| `event_class` | `state_change` | classified from `detail-type` | `access` (or `error` if `errorCode`) | `state_change` | classified from operation verb (or `error` if failed) |
| `resource_type` | from alarm ARN | from `resources[0]` ARN | from resource ARN / `eventSource` | from `alertTargetIDs[0]` | from `resourceId` |
| `severity` | from alarm `state.value` | `info` | `high` if error else `info` | `essentials.severity` (`Sev0..4`) | `level` |
| `observed_at` | `time` | `time` | `eventTime` | `essentials.firedDateTime` | `eventTimestamp` |
| `resource` | alarm ARN | `resources[0]` | resource ARN / service | `alertTargetIDs[0]` | `resourceId` |
| `payload.principal` | — | — | `userIdentity.arn` | — | `caller` |

**Resource-type derivation.** `aws_resource_type_from_arn()` maps an ARN's
service token (`ec2`→compute, `s3`→storage, `rds`→database, `iam`→identity,
`cloudwatch`→monitoring, …); `azure_resource_type_from_id()` maps the resource
id's provider type (`virtualMachines`→compute, `storageAccounts`→storage,
`virtualNetworks`→network, …). Both fall back to `normalize_resource_type()`
then `"other"` — they never raise.

**Severity/class normalisation.** Providers' raw tokens are folded to the
schema vocabulary by `build()` (`Sev2`→`high`, `Informational`→`info`, `stop`→
`lifecycle`, …). See §2 / §3.

**Resilience.** Mappers are tolerant: a missing optional field degrades to a
sensible default (no resource → `resource=None`; no timestamp → now) and never
crashes a run, consistent with the connector conventions.

### Adding a new provider surface

1. Write `map_<surface>(payload, *, org_id) -> OperationalEvent` resolving the
   fields above and calling `OperationalEvent.build(...)`.
2. Register it in `reference_mappers.MAPPERS`.
3. Add a golden fixture case (raw payload + expected normalised fields) to
   `msp_provider_mapping_golden.json`.

---

## 6. Raw-payload storage + evidence resolution (AT-638)

The normalised event is provider-agnostic and, by design, does **not** embed the
raw provider payload (T4-AC4). To keep every finding auditable, the raw payload
is persisted separately and reached through the event's evidence pointer. Code:
[`backend/discovery/signals/evidence_store.py`](../backend/discovery/signals/evidence_store.py).

### How it fits together

- **Evidence pointer** — `OperationalEvent.build()` already mints an OBSERVED
  `EvidencePointer` whose `(source_system, source_artifact)` uniquely names the
  raw event (T4-AC1). No schema change; it reuses the R16-B1 provenance spine.
- **Raw-event store** — `RawEventStore` persists raw payloads keyed by
  `(org_id, source_system, source_artifact)` — the exact tuple the pointer
  carries. `InMemoryRawEventStore` is the offline/default implementation
  (deep-copies on put/get so stored payloads can't be mutated via an alias); a
  DB-backed store drops in for live ingestion (B1/B2/B8) with the same interface.
- **Resolution** — `resolve_raw_event(store, org_id, event)` walks the pointer
  back to the stored raw payload (T4-AC2).

### Organization isolation (T4-AC3)

Evidence is hard-partitioned by `org_id`. The store key includes `org_id`, so a
`get` under a different org never returns another org's payload; and
`store_raw_event` / `resolve_raw_event` raise `OrgScopeError` if asked to act
under a different org than the event owns. Two orgs with the same provider event
id never see each other's raw payload.

### Usage

```python
from discovery.signals import InMemoryRawEventStore, map_and_store, resolve_raw_event, MAPPERS

store = InMemoryRawEventStore()
event = map_and_store(MAPPERS["map_cloudtrail"], raw_payload, org_id="acme", store=store)
# ... detectors consume `event` (no provider payload visible) ...
raw = resolve_raw_event(store, "acme", event)   # audit: back to the original payload
```

`map_and_store` maps + persists in one step (the connector entry point). To
persist an already-mapped event, use `store_raw_event(store, org_id, event, raw)`.

---

## 7. Resource entities into the graph (AT-639)

When an operational event references a cloud resource, that resource is promoted
to a knowledge-graph entity so downstream discovery (relationship mapping, graph
context, correlation) can reason about it. Code:
[`backend/discovery/signals/resource_graph.py`](../backend/discovery/signals/resource_graph.py).

### Conservative, event-driven creation

- A resource becomes an entity **only** when an observed event references it
  (T5-AC1/AC2) — `create_resource_entities(events, run_id=...)` skips any event
  with no `resource`.
- **No speculative estate modelling** (T5-AC3): we promote exactly the resources
  events name — never inferred parents/children/siblings or an estate topology.
  The full estate map is **B3's CMDB**, not event inference. This step draws
  *nodes* only, no speculative edges.
- Distinct resources are de-duplicated per `(org_id, resource_id)`, so one node
  is created per resource however many events reference it; org isolation rides
  on each event's `org_id`.

### How resources map to entities

Each resource is created through the existing conservative resolver
(`app.entity_resolution.resolve_or_create_entity`), so it lands in the standard
`entities` table — resolved, org-scoped, with an OBSERVED evidence pointer — and
is immediately usable by every downstream graph consumer (T5-AC4).

| Entity field | Value |
|--------------|-------|
| `entity_type` | `system` (cloud resources are infrastructure systems; the entity schema is locked — no new type) |
| `display_name` / `source_record_id` | the resource's globally-unique provider id (ARN / Azure resource id) — so repeat sightings resolve to one node and distinct resources never false-merge |
| `source_system` | the resource `provider` (`aws` / `azure`) |
| `metadata` | `{cloud_resource, provider, resource_type, region, resource_name, observed_via, event_signature}` — marks an event-observed estate node and preserves the friendly name for display |

### Usage

```python
from discovery.signals import create_resource_entities

entities = create_resource_entities(events, run_id=run_id)
# events referencing resources -> graph nodes; events without -> nothing.
```

The resolver is injectable for testing and resolved lazily, so importing
`discovery.signals` stays dependency-light.

---

## 8. Dedup at admission — active-signal folding (MSP-B7 / AT-669)

Cloud event streams are orders of magnitude noisier than any business system:
a stuck alarm re-fires every few minutes. **Deduplication at the door** — the
first discipline of MSP-B7 — collapses those re-fires into **one active signal**
per `(event_signature, resource, active period)`, carrying an occurrence count
and first/last timestamps. A stuck alarm firing every five minutes is ONE fact
with a count of 288/day, not 288 facts. Code:
[`backend/discovery/signals/ops_stream.py`](../backend/discovery/signals/ops_stream.py).

### The fold key

| Component | Meaning |
|-----------|---------|
| `org_id` | Tenancy — two orgs sharing a signature never fold together. |
| `event_signature` | The recurrence fingerprint (AT-636): same recurring event → same signature. |
| `resource` | The `resource_id` the event concerns (`""` when the event names none). |
| `active period` | An epoch-anchored time bucket, **default one day** (`DEFAULT_ACTIVE_PERIOD_SECONDS`, tunable per stream; MSP-B7 T6 calibrates it). Same alarm on two days → two active signals. |

### The honesty rule (aggregation compresses volume, never evidence)

An `ActiveSignal` carries its proof: `occurrence_count` (over **distinct**
provider event ids), the `first_seen`/`last_seen` span, and the provider event id
of every folded firing. The raw provider payloads stay in the raw-event store
(§6) — never embedded — and `ActiveSignal.resolve_raw_instances(store)` walks each
member's evidence pointer back to its stored raw payload, so *"this alarm fired
200 times"* opens to the real instances on click.

### Deterministic & org-scoped

- **Deterministic** — folding depends only on event fields, never arrival order:
  the active period is a pure function of the timestamp, the count is over
  distinct provider ids, the span is min/max of observation times, and the
  detector-visible representative is the earliest firing by
  `(observed_at, signal_id)`. Admitting a set of events in any order yields the
  identical active signals.
- **Org-scoped** — the fold key includes `org_id`; `admit` refuses an event under
  a different org, and `resolve_raw_instances` refuses to cross an org boundary.
- **Idempotent** — an at-least-once redelivery of an already-counted firing
  (same provider event id) is a no-op (`disposition == "duplicate"`).

### Usage

```python
from discovery.signals import OpsEventStream, fold_events

stream = OpsEventStream()            # default: one active signal per day
for event in events:
    stream.admit(event)             # re-fires fold; new keys open a signal
signals = stream.active_signals()   # the detector-visible, deduplicated units

# or, over a whole batch:
signals = fold_events(events)
raws = signals[0].resolve_raw_instances(store)   # audit → the raw firings
```

This is MSP-B7 **T1** only. Aggregation roll-ups (T2), noise floors (T3), per-run
budgets (T4), and correlation windows (T5) layer on top of admission in later
tasks.

---

## 9. Aggregation roll-ups for high-cardinality classes (MSP-B7 / AT-670)

Some event classes flood at cloud volumes — **audit floods** and **state-change
storms**. Their T1 active signals are rolled up into a compact
`AggregateSignal` that becomes the **detector-visible unit**, so a detector
reasons about *"this audit action fired 9 000 times this window"* as ONE fact.
Code: [`backend/discovery/signals/aggregation.py`](../backend/discovery/signals/aggregation.py).

### What an aggregate carries (the traceable-aggregate rule)

| Field | Meaning |
|-------|---------|
| `member_count` | The **exact** number of distinct firings (never compressed). |
| `first_seen` / `last_seen` | The time span the aggregate covers. |
| `severity_profile` | `{severity: count}` — the spread the signature ignores (AT-636), preserved. |
| `sample_pointers` | A **bounded** sample of member evidence pointers (`DEFAULT_EVIDENCE_SAMPLE_SIZE`, default 10) — each resolves to a stored raw payload via `resolve_sample_raw(store)`. |
| `sampled_from` / `is_sampled` | The true member count the sample was drawn from, so the compression ratio is never hidden. |

**Raw retention is unchanged** — every raw payload stays stored in the raw-event
store (§6); only the *pointers held on the aggregate* are bounded. `'this fired
9 000 times'` still opens to real instances on click.

### Which signals are rolled up

`roll_up(active_signals)` aggregates only signals whose event class is in
`HIGH_CARDINALITY_CLASSES` (`audit`, `state_change`) by default — low-cardinality
signals (individual alarms) keep their full T1 traceability. The class set and
the sample size are tunable per call (T6 calibrates the defaults from B8's
month-scale measurements). Pass `only_high_cardinality=False` to aggregate every
signal.

### Deterministic & span-anchored sampling

The roll-up is a pure projection of the (deterministic, org-scoped) T1 active
signals: count/span/severity-profile pass straight through, and the evidence
sample is chosen deterministically — members sorted by `(source_timestamp,
source_artifact)`, then evenly spaced **including both span endpoints** so the
earliest and latest instance are always reachable. Same members → same sample,
regardless of arrival order. `resolve_sample_raw` refuses to cross an org
boundary.

### Usage

```python
from discovery.signals import aggregate_events, roll_up, OpsEventStream

# batch convenience: fold (T1) then roll up the high-cardinality classes (T2)
aggregates = aggregate_events(events)
raws = aggregates[0].resolve_sample_raw(store)   # audit → sampled raw instances

# or over an existing stream:
stream = OpsEventStream()
for e in events:
    stream.admit(e)
aggregates = roll_up(stream.active_signals())
```

This is MSP-B7 **T2**. Noise floors (T3), per-run budgets (T4), and correlation
windows (T5) are separate tasks.

---

## 10. Noise floors per event class (MSP-B7 / AT-671)

Cloud streams carry vast low-value chatter — one-off audit records, single state
flips, access noise. A per-event-class **noise floor** sets the minimum number of
times a signature must recur within its active period to be worth a detector's
attention. A signature **below its class floor never becomes a detector-visible
signal**. Code:
[`backend/discovery/signals/noise_floor.py`](../backend/discovery/signals/noise_floor.py).

### The loud-skip rule applied to noise

Suppression is a *decision*, not a silent drop. Every suppressed signature and
every suppressed event is **counted and reported per run, per class**, in a
`SuppressionReport` — so *"we ignored 40 000 events across 12 000 one-off
signatures"* is a visible, tunable decision an operator can challenge. The report
names the floors it applied and is JSON-serialisable for the run record /
run-health surface (R18-C2).

### Where it sits & the defaults

Per the MSP-B7 pipeline (**dedup → floor → budget → aggregate**), the floor is
applied to the T1 **folded** active signals — after folding (so each signature's
count is known) and before aggregation (so only survivors are rolled up and reach
detectors). `apply_noise_floors(signals)` returns `(visible, report)`.

| Class | Default floor | Rationale |
|-------|---------------|-----------|
| `audit` | 5 | audit floods — an action recurring < 5× a window is chatter |
| `state_change` | 5 | state-change storms |
| `access` | 5 | access / API chatter |
| *any other* | `DEFAULT_FLOOR` = 1 | surfaced even at a single occurrence — **`error` and `security` are never floored by default** (you never silently drop a security finding) |

Floors are configurable per policy (`NoiseFloorPolicy({"lifecycle": 5})`); the
calibrated defaults come from B8's month-scale measurements in T6. A signature is
suppressed when its occurrence count is **strictly below** its class floor (count
== floor is visible). The split is pure and order-independent.

### Usage

```python
from discovery.signals import apply_noise_floors, NoiseFloorPolicy, fold_events

signals = fold_events(events)                        # T1 fold
visible, report = apply_noise_floors(signals)        # T3 floor (default policy)
# ... detectors consume `visible`; `report.to_dict()` goes to the run record ...

policy = NoiseFloorPolicy({"audit": 10})             # tune per org/run
visible, report = policy.apply(signals)
```

This is MSP-B7 **T3**. Per-run budgets (T4) and correlation windows (T5) are
separate tasks.

---

## 11. Per-run event-volume budgets (MSP-B7 / AT-672)

A run has an event-volume **budget** — the hard backstop that keeps a single
run's cost bounded when a month of cloud events would otherwise make it slow and
expensive. The run processes the **budgeted window** (the first `budget` events,
in admission order) and, on breach, **defers the rest** — never silent
truncation. Code:
[`backend/discovery/signals/budget.py`](../backend/discovery/signals/budget.py)
(enforced inside `OpsEventStream.admit`).

### Loud degradation, never silent truncation

A budget breach is *an operator decision surfaced, not a data loss hidden*.
`OpsEventStream(budget=N)` enforces the cap during admission: while it has
capacity an event is folded and charged; once the budget is exhausted every
further event is **deferred-and-counted** (`Admission.is_deferred`, `signal is
None`). `stream.budget_report()` returns a `BudgetReport`:

| Field | Meaning |
|-------|---------|
| `budget` | The configured limit (`None` = unbounded). |
| `processed` / `deferred` / `seen` | Events processed, deferred, and total seen. |
| `breached` | True iff any event was deferred. |
| `deferred_by_source` | Per-`source_system` deferred counts. |
| `deferred_window` | `{first, last}` observation times of the deferred events. |
| `reason` | Human-readable explanation (`None` when not breached). |

`to_dict()` is the JSON shape written into the run record and the R18-C2
run-health content panel. The run always *completes* — it never crashes and
never quietly drops events off the end.

### Where it sits & why it's arrival-ordered

Per the pipeline (**dedup → floor → budget → aggregate**), the budget is enforced
**during admission** — it must be, because its job is to stop the run from
*processing* everything (post-hoc filtering would already have paid the cost).
The budget is **volume-based**: every event (including re-fires) counts against
it, so a stuck alarm firing past the budget is deferred like anything else.
Because the budgeted window is the first `budget` events in arrival order, budget
enforcement is deliberately arrival-ordered — unlike the order-independent
dedup/floor/aggregate stages, a budget is a statement about processing order. The
limit is tunable; T6 calibrates the default from B8's month-scale measurements.

### Usage

```python
from discovery.signals import OpsEventStream

stream = OpsEventStream(budget=100_000)     # per-run event-volume budget
for event in events:
    stream.admit(event)                     # deferred-and-counted once full
report = stream.budget_report()             # → run record + R18-C2 panel
if report.breached:
    log.warning(report.reason)
```

This is MSP-B7 **T4**. Correlation windows (T5) are a separate task.

---

## 12. Correlation windows (MSP-B7 / AT-673)

Cross-stream joins turn separate facts into one story — an AWS event and a
ServiceNow incident, two provider events about the same resource. But two things
happening *near* each other in a noisy stream is not the same as two things
happening *because* of each other. The correlation-window service is the honesty
discipline applied to time: **a join is valid only within a configurable time
window**, the window and observed delta are **recorded in the joined claim's
evidence trace**, and **out-of-window agreement contributes zero confidence**.
Code:
[`backend/discovery/correlation/windows.py`](../backend/discovery/correlation/windows.py).

> *Windows are epistemology, not plumbing.* Corroboration means independent
> sources agreeing about the **same moment**; recording the window in the trace
> lets a reviewer challenge the join itself.

### Per-join-type, per-org windows

| Join type | Default window | Meaning |
|-----------|----------------|---------|
| `event_incident` | 2 h | a cloud event ↔ a ServiceNow-style incident |
| `event_event` | 15 min | a cloud event ↔ another cloud event (cross-provider) |
| *any other* | `DEFAULT_WINDOW_SECONDS` = 1 h | fallback |

`CorrelationWindowPolicy` holds the per-join-type defaults and per-org overrides
(`set_org_window(org, join_type, seconds)` → the `window_config(org_id,
join_type)` resolver of the MSP-B7 sketch). Defaults are tunable; T6 calibrates
them from B8's month-scale measurements.

### The join and its evidence trace

`join_within_window(a, b, join_type, org_id=…)` returns a `WindowJoin` carrying
`(join_type, window_seconds, delta_seconds, within_window, a_at, b_at)`;
`to_trace()` is the `correlation_window` fragment attached to the joined claim's
evidence — recorded whether the join succeeds **or fails**, so a rejected
coincidence is auditable, never silent. Timestamp handling is tolerant
(`OperationalEvent`, active signals, incident dicts, ISO strings, `Z` suffix,
`datetime`); an unparseable timestamp yields `within=False, delta=None` (a join
that cannot be confirmed never counts). The boundary is inclusive (delta ==
window is within).

### Corroboration integration (coincidence never inflates confidence)

`gate_operational_corroboration(event, incident, …)` is the surface the
operational corroboration rules consult: an event↔incident agreement **inside**
the window elevates confidence (`MEDIUM → HIGH`, like an observed
system-of-record corroborator — the same bar as COR-09/COR-10); the identical
agreement **outside** the window contributes **zero** and confidence stays at the
base. The confidence vocabulary is shared verbatim with the corroboration rule
registry (`discovery/packs/corroboration_rules.py`). Either outcome — elevation or
rejection — is recorded on the trace.

### Usage

```python
from discovery.correlation import within_window, gate_operational_corroboration

if within_window(aws_event, servicenow_incident, "event_incident", org_id=org):
    ...  # valid join

gate = gate_operational_corroboration(aws_event, servicenow_incident, org_id=org)
# gate.confidence == "HIGH" inside window, "MEDIUM" (zero contribution) outside
claim_evidence.append(gate.to_trace())
```

This is MSP-B7 **T5**, the reusable surface the MSP event corroboration rules
(B4/B6) consult. It does not rewire the existing app-friction corroborators
(COR-09/COR-10), which are 30-day freshness rules, not cloud event↔incident
joins. Calibration of all floors/budgets/windows against B8's month-scale sample
is **T6** (§13).

---

## 13. Calibration from B8's month-scale sample (MSP-B7 / AT-674, T6)

The five disciplines each ship a *default* — noise floors, the per-run event
budget, correlation windows. T6 makes those defaults **evidence-based, not
guessed**: they are derived from MSP-B8's measured month-scale volume run and
documented with their rationale. The single source of truth is
[`backend/discovery/signals/ops_calibration.py`](../backend/discovery/signals/ops_calibration.py);
the T3/T4/T5 modules import their defaults from it, so there is no divergent
hardcoded guess anywhere.

### The measured input (MSP-B8)

B8 ran a representative month of AWS+Azure exports end to end and recorded the
numbers in [`MSP-B8_VOLUME_VALIDATION.md`](MSP-B8_VOLUME_VALIDATION.md) (its
T5/AC7 output). The load-bearing figures, captured verbatim in `B8_MEASUREMENTS`:

| Measurement | Value |
|-------------|-------|
| Events in a representative month (generated) | 30,225 |
| Normalized events ingested (post skip+dedupe) | 29,553 |
| Ingest throughput | 678.5 events/s |
| Per-event ingest cost (tracemalloc active → conservative) | 1.474 ms |
| Peak memory (flat) | 89.61 MB |

### What is derived, and how

| Default | Calibrated value | Derivation |
|---------|------------------|------------|
| **Per-run event budget** (T4) | **250,000 events** | Quantitatively derived: `ceil(8 × 30,225)` = 241,800, rounded up to a clean 250,000. ×8 headroom tolerates an 8×-noisier month or an ~8-month backfill in one run; at the measured 1.474 ms/event that is a ~6-min worst-case ingest ceiling, and flat ~89.6 MB memory means volume drives time, not memory. |
| **Noise floors** (T3) | `audit`/`state_change`/`access` = 5; else 1 | B8 measured aggregate volume (~1,000 events/day) but **not** a per-class recurrence histogram, so floors are set conservatively for the demonstrably-noisy high-cardinality classes (≥5×/day to surface) and everything else stays at 1. `error`/`security` are never floored — no silent drop. Refined to exact per-class values once per-class recurrence telemetry exists. |
| **Correlation windows** (T5) | `event_event` = 15 min; `event_incident` = 2 h; else 1 h | The measured density (~42 events/hour) is the evidence for keeping the cross-provider `event_event` window **tight** — a 15-min window already admits ~10 unrelated events, so it is a ceiling, not a guess. `event_incident` = 2 h is the operationally-justified incident-creation lag (B8 did not measure lag directly); per-org tunable. |

### Honesty about the evidence

The calibration states plainly where the evidence stops: the **budget** is a
direct quantitative derivation from measured volume; the **floors** and the
`event_incident` **window** are operationally-justified defaults consistent with
the measured density, pending finer per-class-recurrence and incident-lag
telemetry. `calibration_summary()` exposes the measured input, the derived
defaults, and the derivation string for run-health/audit — so a reviewer can
trace every default back to a real measurement (AC7).

Calibration reruns whenever B8 re-measures: update `B8_MEASUREMENTS` and the
derived defaults move with it.

### The VR scan-cycle analogue (MSP-B11 / AT-701, T6)

MSP-B11 reads ServiceNow Vulnerability Response as workflow signal, and a single
scan cycle can create **thousands** of vulnerable-item updates at once. Rather than
invent an independent SecOps limit, VR volume answers to the SAME per-run budget as
cloud events: [`backend/discovery/signals/secops_volume.py`](../backend/discovery/signals/secops_volume.py)
(`SecOpsVolumeStream`) reuses the MSP-B7 `RunBudget`/`BudgetReport` verbatim and folds
each record into ONE workflow aggregate per MSP-B11 T4 `remediation_signature`
(`vulnerability_class` + `ci_class` + `remediation_path`) — a fold key that by
construction carries no host, CVE, or per-item id, so aggregation **cannot enumerate
host×vulnerability pairs** (AC6). A budget breach defers loudly (per-table breakdown,
deferred window, safe checkpoint), and the deferred tail resumes exactly.

The AT-701 reference scan-cycle run (the VR analogue of B8's AC7, recorded in
[`MSP-B11_VR_VOLUME_VALIDATION.md`](MSP-B11_VR_VOLUME_VALIDATION.md) and captured
verbatim in `B11_VR_MEASUREMENTS`):

| Measurement | Value |
|-------------|-------|
| Scan-cycle records (items + groups + tasks) | 7,000 |
| Distinct workflow aggregates folded into | 60 |
| Aggregate ratio (patterns / processed) | 0.0086 |
| Peak memory (bounded by patterns, not volume) | 1.43 MB |
| Shared per-run budget reused | 250,000 |

The conclusion is a **budget-adequacy confirmation, not a new derivation**: a scan
cycle folds into a few dozen workflow patterns (memory is bounded by patterns, not
record volume) and fits within the shared 250,000 budget with ~35× headroom, so VR
reuses the calibrated per-run budget. `calibration_summary()` surfaces this under
`vr_scan_cycle_volume`.

---

## 14. Contract suite (MSP-B7 / AT-675, T7)

[`backend/discovery/tests/test_msp_b7_contract.py`](../backend/discovery/tests/test_msp_b7_contract.py)
is the consolidated contract for the five disciplines — one test per Section-3
acceptance criterion (AC1–AC7), each reproducing that criterion's scenario as
stated, plus an end-to-end composition test (dedup → floor → aggregate under a
budget). It is pure-Python (in-memory evidence store, no DB) and runs alongside
the per-task suites (`test_ops_stream_dedup.py`, `test_ops_stream_aggregation.py`,
`test_ops_stream_noise_floor.py`, `test_ops_stream_budget.py`,
`test_correlation_windows.py`, `test_ops_calibration.py`).

---

## 15. Shared native cloud-connector skeleton (MSP-B1 / AT-641, T1)

The MSP-B8 bridge normalises *exported* event history through the B0 mappers;
MSP-B1 (AWS) and MSP-B2 (Azure) do the same for a *live, checkpointed* feed. Those
two native connectors do the same four things and differ only in the provider
edge, so the common shape is built ONCE in
[`backend/discovery/ingest/cloud_event_connector.py`](../backend/discovery/ingest/cloud_event_connector.py)
and both consume it — the direct application of the R17-A3/A4 "share the
extraction, not just the idea" discipline to clouds. The skeleton IS the contract
with MSP-B2: if B2 must fork it, that is a design defect to surface early (AT-641).

### The four responsibilities

* **Poll loop.** For each configured *scope* (a managed account/subscription ×
  provider surface, e.g. CloudWatch in `us-east-1`) the connector pages forward
  from that scope's last position via an injectable `CloudPollSource` — the ONLY
  provider-specific edge (a live boto3 / Azure SDK client in production, an
  in-memory `StaticCloudPollSource` offline/in tests).
* **Per-scope checkpoints.** One `(org_id, connector_id)` checkpoint row is
  persisted by the runner, but a deployment polls many scopes. The opaque value
  encodes a per-scope position MAP (`{"v":1,"scopes":{scope_key: position}}`); a
  scope absent from the map is polled from the beginning, so a first load is
  resumable. The runner never interprets it (R16-A1 AC5).
* **Mapper invocation.** Each raw payload is normalised through the B0 reference
  mapper named on its scope, so every event is the identical detector-visible
  `OperationalEvent` shape (§5) — a detector never branches on provider.
* **Admission hand-off (B7).** Every mapped event is handed to an `OpsEventStream`
  (§8), so re-fires fold into one active signal with a count at the door and the
  per-run budget (§11) is enforced. `active_signals()` exposes the deduplicated
  view; `budget_report()` exposes the deferral proof.

### Transport equivalence with the bridge (AC4)

The connector re-stamps each event's `source_system` to the provider family
(`'aws'` / `'azure'`) while PRESERVING the mapper's `event_signature` — exactly
mirroring the way the bridge (§8 of the B8 story) re-stamps to `'bridge:<provider>'`.
So a natively-ingested event and its bridged twin are detector-identical except
for that one field (`'aws'` vs `'bridge:aws'`). The raw payload is stored against
the event's OBSERVED evidence pointer (§6) — reachable for trace-back, never
embedded in the detector-visible model.

Deletes: `reports_deletes = False` — a cloud event stream is append-only
observation history; a fired alarm or logged API call is never retracted, so
there is no deletion to propagate (the limitation is declared, not faked).

Retrieval: `produces_retrieval_content = False` — a cloud event is an observation,
not an indexed retrieval artifact. Nothing chunks it, nothing resolves it, and it
can never be updated or deleted upstream, so the change runner emits no per-event
`ingestion.artifact_changed` telemetry and drives no retrieval-freshness
invalidation for these records (it reports the volume once per batch instead). At
event volume the per-record path cost a telemetry row plus a
mark-stale-and-enqueue transaction per event, and parked unresolvable rows in the
retrieval refresh queue.

### The poll phase is bounded per run

A scope's backlog can be far larger than one poll — `cloudtrail:LookupEvents`
retains 90 days and permits only a couple of calls per second — so the
continuation loop is bounded by three rules, each checked only *between* polls of
the same scope, and each reported rather than silent:

1. **Event budget.** The B7 per-run budget (§11) stops the *fetching*, via
   `OpsEventStream.has_capacity()`, not just the admission. Enforcing it at
   admission alone bounded the data but never the work: once exhausted the
   connector kept paying for provider pages whose every event it then deferred.
2. **Per-scope continuation cap** — `max_polls_per_scope`
   (`CLOUD_EVENT_MAX_POLLS_PER_SCOPE`, default 4): one scope's backlog can never
   monopolise a run.
3. **Wall-clock deadline** — `poll_deadline_seconds`
   (`CLOUD_EVENT_POLL_DEADLINE_SECONDS`, default 180): volume is not time. A
   throttled provider can spend minutes on a single page, so a volume bound alone
   does not bound a run. The deadline is consulted for CONTINUATION polls only, so
   every scope still gets its first poll and a late scope is never starved by an
   earlier one's backlog.

Stopping early is **resume, not truncation**: the scope's advanced position rides
the terminal batch's checkpoint, so the next run continues exactly where this one
stopped. Every early stop logs at WARNING and appears in `poll_report()` — which
names each undrained scope and the bound that stopped it, and which the runner
merges into the run's `cloudOpsRuntime.awsEvents.poll` health block, degrading the
connector's reported status so a partial ingest never reads as a clean one.

Without these bounds a first run against a real multi-account estate walked the
provider's entire retention inside one ingestion stage: the run's progress froze
and the discovery run never reached the detectors.

### AWS instantiation

[`backend/discovery/ingest/aws_event_connector.py`](../backend/discovery/ingest/aws_event_connector.py)
is the thin MSP-B1 binding: `AWSEventConnector` is the skeleton with
`provider='aws'` and the three AWS surfaces (CloudWatch / EventBridge / CloudTrail)
wired to their B0 mappers. `build_offline_aws_source()` reads the deterministic
[`aws_native_events_sample.json`](../backend/discovery/ingest/fixtures/aws_native_events_sample.json)
fixture so a run works with no AWS account (offline-first). MSP-B2's Azure
connector is the SAME skeleton with `provider='azure'`.

### AWS auth & cross-account access (MSP-B1 / AT-642, T2)

The **live** poll source is
[`aws_poll_source.py`](../backend/discovery/ingest/aws_poll_source.py)'s
`AWSLivePollSource`, backed by the auth layer in
[`aws_auth.py`](../backend/discovery/ingest/aws_auth.py). It implements the MSP
access model — **one connection, many accounts, each account a scope**:

* **Hub credentials** (the management identity's long-lived key) are vaulted as a
  static credential under connector id `aws_events`; **direct per-account keys**
  (the fallback) under the reserved id `aws_events:account:{account_id}`. No AWS
  secret ever lives in config or an `.env` credential.
* Per managed account, `AWSAuthenticator` has the hub call `sts:AssumeRole` on that
  account's read-only role (ExternalId-gated), yielding short-lived scoped
  credentials; if a role is absent or an AssumeRole attempt fails, it **falls back
  to direct per-account keys**. Credentials are cached per `(org, account)` — one
  assumption per account per run.
* Each `(account, region, surface)` is a scope. The surface readers use EXACTLY
  the granted read-only calls — `cloudwatch:DescribeAlarmHistory` (reconciled to
  the Alarm State Change shape `map_cloudwatch` consumes), `events:ListRules` +
  `events:DescribeRule` (the bounded EventBridge rule surface), and
  `cloudtrail:LookupEvents`. Across-run resume rides a per-scope time watermark.
* **CloudWatch polling (AT-643, T3).** V1 ingests CloudWatch **alarm state changes
  only** — `DescribeAlarmHistory` filtered to `HistoryItemType='StateUpdate'`, NOT
  metrics or CloudWatch Logs. `StartDate` narrows the server window to each
  account's checkpoint, and a client-side `ts > watermark` filter is the
  authoritative incremental guard so a second run re-reads nothing; each account's
  checkpoint advances independently. Live `Timestamp` values (botocore returns
  aware datetimes) are normalised to the same ISO string a fixture carries so
  watermarks compare across fixture and live runs.
* **CloudTrail + EventBridge polling (AT-644, T4).** CloudTrail: `LookupEvents`
  ingests **management (audit) events only** — data events are never returned by
  LookupEvents, and `_is_management_event` defensively drops any explicit
  `Data`/`Insight` record; incremental by the same `StartTime` + `ts > watermark`
  time watermark as CloudWatch, with `MaxResults` clamped to the documented API
  maximum of 50. Because `LookupEvents` is newest-first with no sort control, a
  backlog larger than one poll is walked BACKWARDS (a descending ceiling, with the
  watermark pinned until the window drains). That walk is bounded in DEPTH by
  `AWS_EVENT_MAX_BACKFILL_DAYS` (default 30), measured from the backfill's own
  newest event: an unbounded walk of the full 90-day retention keeps the watermark
  pinned across many runs, so every NEW event queues behind the entire history.
  On reaching the depth the window closes, the high-water mark is promoted, normal
  incremental polling resumes, and the bounded initial load is logged at WARNING —
  a declared, configurable boundary, never a silent thinning. EventBridge: bounded reads over the scoped rule set
  (`ListRules` + `DescribeRule`) → `map_eventbridge`; because a rule set is
  configuration not a time series, its per-scope checkpoint is a compact
  `{rule_key: signature}` map and a rule is emitted only when NEW or CHANGED — an
  unchanged rule set re-reads nothing (AC3). V1 scope is enforced end to end: only
  CloudWatch alarms, bounded EventBridge operational events, and CloudTrail
  management events — no data events, no GuardDuty/Security Hub streams (never
  called, never granted), no logs/metrics.
* The boto3 clients are built through an injectable `AWSClientFactory`
  (`Boto3ClientFactory` lazily imports boto3; tests inject seeded fakes), so the
  whole auth + ingest path is proven with no AWS account.

The minimal read-only IAM policy the connector needs ships as a partner-security
artifact — [`deployment/aws_readonly_iam_policy.json`](../deployment/aws_readonly_iam_policy.json)
+ [`deployment/AWS_READONLY_IAM_POLICY.md`](../deployment/AWS_READONLY_IAM_POLICY.md)
— minimal (exactly the calls used, no wildcard/write actions) and requiring
independent security-review sign-off (AT-642 AC9).

### Partition-aware endpoints (AT-645, T5)

AWS is two partitions: commercial (`aws`) and GovCloud (`aws-us-gov`).
[`aws_partitions.py`](../backend/discovery/ingest/aws_partitions.py) makes endpoint
configuration partition-aware from day one — a pure, config-level surface (no
boto3/network):

* `endpoint_map(partition, region)` resolves every connector service endpoint
  (`monitoring`/`events`/`cloudtrail`/`sts`.`{region}`.`amazonaws.com`), so GovCloud
  endpoints (`*.us-gov-west-1.amazonaws.com`) are resolved correctly and testable
  without a live call (AC7).
* `AWSAccountConfig.partition` is selectable per connection; when unset it is
  derived from the account's region, and a region contradicting its partition (a
  GovCloud region under commercial, or vice-versa) is rejected at config time.
* The commercial partition has a global STS endpoint; GovCloud is regional-only.
* Resource ARNs the connector builds (e.g. a CloudWatch alarm ARN) carry the
  correct partition (`arn:aws-us-gov:…` in GovCloud).

This is the config surface **MSP-B9's live verification consumes** (its
follow-through, including FIPS endpoint variants, is referenced in
`aws_partitions.B9_LIVE_VERIFICATION_NOTE`).

### Failure loudness & outbound-only (AT-646, T6)

Failures are loud, never silent ([`aws_health.py`](../backend/discovery/ingest/aws_health.py)).
The poll source records a per-account health outcome that
`AWSEventConnector.health_report()` exposes as the run-record / R18-C2
connector-panel artifact (same pattern as the B7 `budget_report`):

* A per-account **auth failure** (revoked role, expired/missing credentials) marks
  that account `auth_failed` and degrades only its scopes — **other accounts
  continue** and their data is still ingested (AC8). A failure is logged at WARNING
  and reported; it is never a silent skip that hides missing data.
* **Throttling** backs off (bounded exponential retry, injectable sleeper) and is
  counted per account; a scope that recovers stays `ok` with its `throttle_events`
  reported, so the back-off is visible and the data is retried, not thinned. A
  throttle budget that is exhausted marks the scope `failed` (status `partial` if
  other scopes succeeded) — loud, never a silent partial that reads as complete.
  The back-off wraps the **individual API call** (`_ThrottleRetryingClient`), not
  the multi-page reader: retrying the reader discarded every page already fetched
  and re-read it, so on an API that permits ~2 calls/second — where throttling is
  routine, not exceptional — the work became quadratic and the poll appeared hung.
* **Outbound-only** (AC6): the connector only makes checkpointed polling calls —
  no SNS subscriptions, webhooks, or inbound listeners — so it works by
  construction under `NETWORK_PROFILE=no_public_inbound`. A structural test scans
  the connector modules for any push/inbound API.

### Contract suite (AT-647, T7)

[`backend/discovery/tests/test_msp_b1_contract.py`](../backend/discovery/tests/test_msp_b1_contract.py)
is the consolidated Section-3 contract for MSP-B1 — one labelled test per
acceptance criterion (AC1–AC8), each reproducing that criterion's scenario, with
the **B8-bridge transport equivalence (AC4)** as its headline: B0's golden fixtures
run through the native connector are detector-identical to the bridge path except
`source_system` (`'aws'` vs `'bridge:aws'`). It sits alongside the per-task suites
(T1–T6) and restates the whole contract in one place, mirroring
`test_msp_b7_contract.py`. Pure-Python (seeded fakes; the bridge runs over an
in-memory staging sink). AC9 (the read-only IAM policy) is a human design-review
gate on the [`deployment/AWS_READONLY_IAM_POLICY.md`](../deployment/AWS_READONLY_IAM_POLICY.md)
artifact.

### Contract suite

[`backend/discovery/tests/test_cloud_event_connector.py`](../backend/discovery/tests/test_cloud_event_connector.py)
proves AC4 (the B0 golden fixtures run through the native connector are equivalent
to their bridged twins except `source_system` — for AWS *and*, through the same
skeleton with `provider='azure'`, for Azure) and AC5 (a seeded re-firing alarm
folds into one active signal with a count, live through the native poll path, and
the aggregate still opens back to its raw instances), plus the poll loop, opaque
per-scope checkpoints, resumable first load, and loud-skip robustness.

## 16. Cloud events are CORROBORATION, not a standalone finding source

A recurring question when a cloud connector is first switched on is: *"AWS is
connected and events are arriving — why are there no findings and no Source
Intelligence signals?"* That outcome is the DESIGN, not a break in the pipeline.
This section records the execution path and the deliberate gate, so the absence
of AWS-only findings is never re-investigated as a bug.

### 16.1 The execution path

A native cloud connector is driven from `discovery/runner.py`, not from a
separate scheduler:

```
AWS account
  └─ aws_poll_source.AWSLivePollSource.poll()        per (account, region, surface)
       └─ reference_mappers.map_cloudwatch/…         provider payload -> OperationalEvent
            └─ OpsEventStream.admit()                B7: dedup + noise floor + budget
                 └─ CloudEventConnector.ingest_changes()   DeltaBatch + per-scope checkpoint
                      └─ runner._ingest_aws_events()       (runner.py, gated — see below)
                           └─ aws_events_data["records"]
                                └─ build_cloud_ops_runtime(bridge_records=…)
                                     └─ sn_data["cloud_ops"]["event_signatures"]
                                          └─ cloud_ops detectors
```

`runner._ingest_aws_events` is called under a two-part gate:

```python
if _any_cloud_ops and "aws_events" in _systems:
    aws_events_data = _ingest_aws_events(org_id, run_id)
```

so BOTH a `cloud_ops`-domain pack must be selected AND `aws_events` must be in
the run's systems set (put there by
`app/live_ingest_credentials._resolve_aws_events`, which requires a pinned
account config *and* resolvable credentials). Native AWS records are merged with
the B8 bridge and B2 Azure records into ONE assembly call, where the
`OpsEventStream` folds identical `event_signature`s — so a native event and its
bridged twin never double-count.

### 16.2 The corroboration gate

Cloud events reach the detectors as `sn_data['cloud_ops']['event_signatures']`.
Whether a signature is detector-ELIGIBLE is decided by one field, set in
`cloud_ops_runtime.py`:

```python
"window_overlap": bool(joined_incidents),
```

An event signature earns `window_overlap=True` only when it joins a ServiceNow
incident inside the MSP-B7 T5 correlation window (§12). Every cloud-ops detector
gates on it — `cloud_ops_recurring_resolution_loop._recurring_event_index`
requires `window_ok and recurring`; `cloud_ops_alert_triage_toil._qualifies`
requires `window_ok` plus `0.0 < median_ttr_minutes <= max_resolve_minutes` and
exactly one distinct close code; `cloud_ops_shared_ci_hotspot` applies the same
window gate.

Those companion fields — `incident_count`, `median_ttr_minutes`, `close_codes`,
`assignment_group` — are all ITSM-derived. **A cloud event carries none of them.**
An events-only signature is therefore structurally ineligible:

```
incident_count: 0   median_ttr_minutes: 0.0   distinct_close_codes: 0
assignment_group: ""   window_overlap: false   window_gated: true
```

This is the intended contract. The cloud_ops pack measures **operational toil** —
repeated human resolution work, triage effort, queue ageing, reassignment
churn — which is a property of the ITSM record, not of the alarm. Cloud events
CORROBORATE that toil (elevating MEDIUM->HIGH within the window, the same bar as
COR-09/COR-10) and supply the resource/CI spine the shared-CI hotspot traverses.
They never assert a finding alone, because "an alarm fired 288 times" is not by
itself evidence that anyone did repeated manual work.

**Consequence, stated plainly:** an org with AWS connected but no ServiceNow
produces zero cloud_ops findings and no Source Intelligence signals from AWS,
however many events arrive. That is correct. AWS raises no finding of its own;
it strengthens findings anchored in ITSM data. Pairing AWS with ServiceNow is
what makes cloud events visible in results — `test_cloud_ops_runtime.py::
test_runtime_wires_b4_b5_and_b8_into_existing_cloud_detectors` is the pinned
proof of the positive path (events + incidents -> `window_overlap=True` ->
detector fires at HIGH confidence).

### 16.3 Runtime visibility

The connector reports each stage at INFO so a quiet run can be diagnosed from
logs alone, without attaching a debugger or reading the health blob:

| Stage | Log line | Source |
|---|---|---|
| Mode / config | `aws_events: offline mode …` / `live mode — N pinned account(s) …` | `aws_event_connector.py` |
| Scopes | `aws_events: org=… first run (full poll) — N scope(s)` | `cloud_event_connector.py` |
| Authentication | `aws_poll_source: authenticated account … via assumed_role\|direct_keys\|hub (partition=…)` | `aws_poll_source.py` |
| Per-surface poll | `aws_poll_source: polled cloudwatch\|eventbridge\|cloudtrail account=… region=… — N event(s)` | `aws_poll_source.py` |
| Mapping + admission | `aws_events: org=… mapped N OperationalEvent(s) -> M active signal(s)` | `cloud_event_connector.py` |
| Run summary | `AWS event connector: N event(s), M batch(es), checkpoint_advanced=…` | `runner.py` |
| Detector assembly | `Cloud Operations runtime assembly: status=… event_signatures=N` | `runner.py` |

The authentication line is emitted once per account (not once per scope) and
never logs a credential. A polled-but-empty surface logs `0 event(s)`, so it is
distinguishable from a surface that was never polled at all. Failures stay loud
and separate: per-account auth failure, per-scope failure, and throttling each
log at WARNING via `aws_health.AWSConnectorHealth` and land in the run-health
report.
