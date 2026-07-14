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
