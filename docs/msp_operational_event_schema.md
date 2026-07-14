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
