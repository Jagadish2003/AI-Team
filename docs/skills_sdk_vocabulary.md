# The AgentIQ concept vocabulary (partner reference)

**Audience:** partners authoring an AgentIQ pack (2.0-C3 Skills SDK).
**Published by:** 2.0-B4 T6. **Source of truth:** `GET /api/concepts/vocabulary`.

A pack does not read connectors. It reads **concepts** — normalised shapes that every
connector maps onto — so one detector works across ServiceNow, Jira, Salesforce, chat,
documents, code and cloud events without a per-source copy. This page is the contract
for those concepts.

> Everything here is served live at `GET /api/concepts/vocabulary` (viewer+). Read the
> endpoint, not this page, when it matters: the endpoint is generated from the same
> registry the platform validates against, so it cannot drift. This page explains it.

---

## The seven concepts

| Concept | What it is | Notes |
|---|---|---|
| `work_item` | A tracked unit of work — incident, request, issue, case, change, task | The concept most detectors start from |
| `actor_group` | A team, queue, department, role or vendor | **Never an individual** — see below |
| `artifact` | A document, page, attachment, code file, commit, runbook or thread | A *reference* to content, never the content |
| `state_transition` | One recorded change of state on a work item | `reopen` is distinct from `status_change` |
| `approval` | One approval gate | `pending` is a first-class decision |
| `assignment` | Work arriving at a group | `initial` vs the rest is the handoff signal |
| `entity_reference` | A pointer to a graph entity or source record | A **value type** — it carries no observation of its own |

Six are observation profiles carrying the standard spine (`org_id`, `source_system`,
`signal_id`, `observed_at`, `provenance`). `entity_reference` is a value type: it is
always carried *by* an observation, so it has no provenance of its own.

## Two rules that will shape your pack

**1. Groups, never individuals.** `actor_group` is the only actor concept, and no
concept offers a field that can hold a person. This is not a style preference — it is a
platform guarantee, and there is deliberately no way around it. If a source only records
an individual (a Jira assignee, a Salesforce user owner), the group field arrives
**empty** and the gap is declared. Your detector can still count the handoff; it cannot
name who took it.

**2. Closed vocabularies.** `status_category`, `priority`, `group_type`,
`artifact_type`, `content_type`, `transition_type`, `decision`, `assignment_type` and
`entity_type` are closed sets, validated at construction. The published vocabulary lists
every allowed value. Two are worth internalising:

- `cancelled` is **not** `resolved`. Abandoned work is not delivered work; a throughput
  detector that treats them alike overstates every number it produces.
- `status_category` is deliberately **coarse**. The source's own string survives on
  `native_status` for display and trace-back, but branch your logic on the category.

## Availability: what you can actually build on

A concept exists in the vocabulary; whether a *customer's estate* can supply it depends
on their connectors. The vocabulary publishes availability per concept, and it lists
only connectors that genuinely map it today.

| Concept | Available from |
|---|---|
| `entity_reference` | servicenow, jira, salesforce, confluence, sharepoint, slack, teams, github, aws_events, azure_events |
| `artifact` | confluence, sharepoint, slack, teams, jira, github |
| `actor_group` | servicenow, salesforce, slack, teams |
| `work_item` | servicenow, jira, salesforce |
| `state_transition` | servicenow, salesforce |
| `assignment` | servicenow, salesforce |
| `approval` | salesforce |

*(Live values come from the endpoint; this table is a snapshot at publication.)*

Declare the concepts your pack requires. `sources_for_required_concepts()` answers
"which connectors satisfy all of them", and `unsupported_requirements()` names what a
given connector is missing — which is how an incompatible pack is refused with an
actionable reason rather than installed to find nothing.

## Declared gaps: read these before you design

Availability alone is not enough. A connector can support a concept and still be unable
to fill a particular field. Those are published as **field gaps**, in two kinds:

- **`absent`** — never populated by that connector, for any record.
- **`partial`** — populated only under a stated condition, and the condition is the
  useful half of the record.

Examples you will meet immediately:

| Connector | Concept | Field | Kind | What it means for you |
|---|---|---|---|---|
| servicenow | `state_transition` | `actor_group` | absent | You cannot know which group moved an item |
| salesforce | `work_item` | `assigned_group` | partial | Only queue-owned cases carry a group; person-owned ones do not |
| salesforce | `assignment` | `assigned_to` | partial | Only when the new owner is a queue |
| jira | `work_item` | `assigned_group` | absent | Jira assigns to people, so there is no group |
| jira | `work_item` | `closed_at` | absent | Jira has no close event separate from resolution |

An empty field on a declared gap is **deliberate, not broken ingestion**. That
distinction is the point of publishing them: design for the gap, and never infer a value
to fill it. AgentIQ enforces this on its own side — a mapper that populates a field it
declared absent fails the build.

## Versioning and pinning

Three version numbers, because they break different things:

| Version | Changes when | Effect on your pack |
|---|---|---|
| `vocabulary_version` | this published document's shape changes | your tooling may need updating |
| `concept_set_version` | a concept is added or removed | added: nothing breaks. Removed: anything requiring it breaks |
| `contract_versions[concept]` | that concept's required fields or closed vocabularies change | a required field added: your mapping assumptions may no longer hold |

The response also carries a **`digest`** — a `sha256` over the canonical content. Pin it
in your pack manifest. Any change you could observe (a concept, a required field, a
vocabulary value, a connector gaining or losing availability, a gap appearing) moves the
digest; internal refactors on our side do not, so a digest change always means something
that concerns you.

### The stability promise

- A published concept is not removed without a set-version bump stating its replacement.
- A value is never removed from a published closed vocabulary without a contract bump.
- A field never becomes required without a contract bump; adding an optional field does not bump.
- Availability improving is additive and needs no bump. Availability being **withdrawn**
  is breaking for any pack that required it, and shows up in the digest.
- A gap being **added** to a concept a connector already supported means that mapping got
  narrower — the case pinning a digest is meant to catch.

## What B4 gives you, and what it does not

**Provided here:** the concepts and their closed vocabularies; a versioned mapping
contract per concept including rules a field constraint cannot express; per-connector
availability; declared gaps at concept and field level; a digest to pin.

**Not here — these are 2.0-C3's:** the detector primitive library (recurrence, ageing,
threshold-vs-baseline, concentration, co-occurrence), the pack manifest schema and its
validator, and packaging/signing/installation.

**Deliberately never provided:** any executable extension point. Partner packs are
declarative configuration; no partner-supplied code runs inside a customer deployment.
That is why this vocabulary publishes **capability and never a module path** — you will
not find a Python import here to call, and that absence is intentional rather than an
omission.

## Reading it

```
GET /api/concepts/vocabulary      # the partner contract (this document, live)
GET /api/concepts/by-concept      # "which sources can carry my detector?"
GET /api/concepts/gaps            # every declared gap, both orientations
```

In Python, inside the platform:

```python
from discovery.concepts import (
    publish_vocabulary, vocabulary_digest,
    sources_for_required_concepts, unsupported_requirements,
)

sources_for_required_concepts("work_item", "actor_group")   # ('salesforce', 'servicenow')
unsupported_requirements("jira", "approval")                # ('approval',)
```

---

*Internal reference: `backend/discovery/concepts/sdk_vocabulary.py`; validated by
`backend/tests/unit/test_r2_0_b4_t6_acceptance.py`. The internal conformance view —
which names the mapper behind each claim — is `GET /api/concepts/conformance`, and is
not part of the partner contract.*
