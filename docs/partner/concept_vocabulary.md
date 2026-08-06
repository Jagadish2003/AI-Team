# Concept vocabulary

What your detectors read. A primitive never sees a connector payload — it reads
**normalised concept records**, and your manifest names the concept, never the
system it came from.

That indirection is the whole reason a partner pack is viable. There are a dozen
connectors and more coming; a pack written against ServiceNow's field names would
break the day it met a customer whose incidents live somewhere else, and no
partner should have to learn fifteen connector dialects to ship one detector.

---

## 1. The concepts

Each concept is stamped with the platform version that introduced it. Declaring a
concept in `compatibility.requiredConcepts` is what makes your pack refuse to
activate on a platform that cannot supply it — with a refusal that names the
concept, rather than a run that quietly finds nothing.

<!-- generated:concepts — regenerate with `python scripts/pack_sdk.py docs --write`; do not edit by hand -->
Platform capability version **2.0.0** — 15 normalised concepts.

| Concept | Since | Available here | What it normalises |
|---|---|---|---|
| `benefit_administration_workflow` | 1.0.0 | yes | STRS benefit application, election, and disbursement normalisation |
| `case_workflow` | 1.0.0 | yes | Salesforce case, flow, and approval-process workflow normalisation |
| `code_activity_signal` | 1.0.0 | yes | Source-control activity signals (pull requests, commits, branches) |
| `cross_system_link` | 1.0.0 | yes | Cross-system signal linking between ServiceNow, Jira, and Salesforce |
| `db_operational_signal` | 1.0.0 | yes | Native database operational signals (ticket volume, SLA, queue depth) |
| `incident_workflow` | 1.0.0 | yes | ServiceNow incident workflow normalisation (state, category, close code, time-to-resolve) |
| `loan_origination_workflow` | 1.0.0 | yes | nCino commercial-lending origination, checklist, and spreading normalisation |
| `assignment_group_routing` | 1.9.0 | yes | MSP-B4 assignment-group routing history (group-level reassignment hops) |
| `cmdb_dependency` | 1.9.0 | yes | MSP-B3 CMDB configuration items and dependency edges |
| `incident_identity_signature` | 1.9.0 | yes | MSP-B4 deterministic incident-identity signature — what kind of incident this is |
| `operational_event` | 1.9.0 | yes | MSP-B0 normalised operational cloud event (with MSP-B7 dedup and volume disciplines) |
| `resolution_signature` | 1.9.0 | yes | MSP-B4 deterministic resolution signature — how an incident was resolved |
| `runbook_match` | 1.9.0 | yes | MSP-B5 runbook match states (observed / proposed / confirmed) |
| `security_incident_workflow` | 1.9.0 | yes | MSP-B11 ServiceNow security-incident (SIR) workflow normalisation |
| `vulnerability_workflow` | 1.9.0 | yes | MSP-B11 ServiceNow vulnerability-response workflow normalisation |
<!-- /generated:concepts -->

**Required versus optional.** Declare a concept *required* only when your pack
cannot produce anything honest without it. A concept your pack merely uses when
present belongs in `optionalConcepts`: the platform degrades a finding whose
optional input is absent, which is the behaviour you want, whereas a required
concept turns the same situation into a refusal.

**A concept is a platform capability, not a promise of data.** It being listed
here means the platform can normalise it. Whether a given customer has connected
a system that produces it is a separate question, answered by their connector
setup — and a disconnected source degrades your findings, it does not make your
pack incompatible.

## 2. What a record looks like

Every concept instantiates the same record shape. Your fixtures write these
directly, and this is exactly what the primitives read:

| Field | Meaning |
|---|---|
| `concept` | The normalised concept this record instantiates |
| `record_id` | Stable id of the underlying source record |
| `source_system` | The system it was observed in — this is what corroboration counts |
| `observed_at` | When the fact was observed; the anchor for window arithmetic |
| `opened_at` | Lifecycle timestamp: when the item was raised |
| `last_state_change_at` | Lifecycle timestamp: when it last moved |
| `due_at` | Lifecycle timestamp: when it falls due |
| `signature` | Deterministic recurrence fingerprint, where the source provides one |
| `actor_group` | The group or queue holding the work — never a person |
| `artifact` | The artifact the record is about (a queue, a document) |
| `entity_reference` | The entity or CI it touches; the concentration anchor |
| `state` | Normalised lifecycle state |
| `metrics` | Numeric measures, including `*_baseline` companions |
| `transitions` | Ordered state/assignment changes, for oscillation |
| `attributes` | Anything else the mapping recorded — individual-free |

A minimal record:

```json
{
  "concept": "incident_workflow",
  "record_id": "INC-4001",
  "source_system": "servicenow",
  "observed_at": "2026-06-05T09:00:00Z",
  "opened_at": "2026-06-01T09:00:00Z",
  "last_state_change_at": "2026-06-05T09:00:00Z",
  "actor_group": "payments-ops",
  "artifact": "payments-queue",
  "entity_reference": "svc-payments",
  "state": "open"
}
```

### Baselines

The `threshold_vs_baseline` primitive compares a metric against **that subject's
own** baseline, supplied as a `_baseline` companion in `metrics`:

```json
"metrics": {"backlog_depth": 180, "backlog_depth_baseline": 90}
```

A subject with no baseline does not fire. Unbaselined is not the same as
compliant, and a detector that treated it as compliant would report a clean
result for the estate it knows least about.

## 3. Three admission rules you will meet immediately

These are enforced when a record is *admitted*, before any primitive sees it — so
they fail in your fixtures, at authoring time, rather than in a customer's run.

**Individual-free.** A record carrying an individual-person field (`assignee`,
`caller`, `user_email`, …) or an email-shaped value in `actor_group`, `artifact`,
`entity_reference`, or a transition participant is refused outright. Checking only
the finding would be too late: a pack could group *by* an individual and emit a
"group" whose identity is a person. Packs describe groups, queues, services, and
entities.

**Unknown concepts are refused.** A misspelled concept is an error, not an empty
result. A detector that silently reads nothing is the hardest kind of bug to see.

**Nothing reads the clock.** Age and window arithmetic resolve against an
evaluation instant — either the `asOf` your case pins, or the latest timestamp in
your own seeded signal. Use fixed timestamps in fixtures and your suite cannot
start failing on a date nobody chose.

## 4. Sources, corroboration, and the conversation ceiling

`source_system` is load-bearing: confidence is derived from how many *independent*
systems agree about a subject.

* One source → **MEDIUM**, capped, with the cap stated on the finding.
* Two or more → eligible for **HIGH**.
* Conversational sources (`slack`, `teams`) → **never above MEDIUM on their own**,
  however many of them agree. Chat is a real signal and a poor witness; the
  standing platform ceiling applies to your pack exactly as to a first-party one.

You cannot raise any of these from a manifest. You *can* lower them — see
`scorerCalibration.confidence` — and a pack whose domain deserves more caution is
welcome to.
