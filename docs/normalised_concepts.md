# The normalised concept set (2.0-B4 T1)

**Concept set version: 1.** Every contract version: 1.

MSP-B0 proved the principle on one source family: define one normalised shape, make
every provider map onto it, and detectors stop branching on provider. This document
widens that pattern to the families AgentIQ now ingests — business workflow,
documents, conversations, code structure.

Why it matters more than it sounds. Today a recurrence detector written for
ServiceNow reads `sys_id` / `assignment_group` / `opened_at`; the same logic for Jira
reads `key` / `fields.assignee` / `created`. Two detectors exist because two dialects
do. A partner authoring a pack (2.0-C3) cannot be asked to learn fifteen dialects,
and should not have to.

---

## The set

| Concept | What it is | Kind |
|---|---|---|
| `work_item` | A tracked unit of work: incident, request, issue, case, change, task | observation |
| `actor_group` | A team, queue, department, role or vendor — **never an individual** | observation |
| `artifact` | A document, page, attachment, code file, commit, runbook or thread | observation |
| `state_transition` | One recorded change of state on a work item | observation |
| `approval` | One approval gate on a work item or change | observation |
| `assignment` | Work arriving at a group | observation |
| `entity_reference` | A pointer to a graph entity or source record | **value type** |

Defined in [`backend/discovery/concepts/model.py`](../backend/discovery/concepts/model.py).

### Six profiles and one value type, not seven profiles

Each observation specialises `CommonSignal` — MSP-B0's shared spine carrying
`org_id` (tenancy), `source_system`, a stable `signal_id`, an observation time, and a
valid OBSERVED `EvidencePointer`. Nothing reinvents source tracking or provenance,
and `OperationalEvent` remains a **sibling** profile on the same spine, unchanged: if
it had become a base class, every cloud connector would have inherited workflow
fields that mean nothing to it.

`EntityReference` is deliberately *not* a profile. A reference has no observation of
its own — it is always carried by one — so modelling it as a signal would force every
construction to invent a provenance spine it does not have, and an invented spine is
exactly what R16-B1 forbids. B0 made the same split for the same reason: `ResourceRef`
is not a signal, it is how a signal points at the thing it concerns.

### Groups, never individuals

`ActorGroup` is the only actor concept, and it is incapable of naming a person: no
value in `ACTOR_GROUP_TYPES` denotes an individual, `member_count` is an aggregate,
and there is no member list at any version. Every group-bearing field on the other
concepts (`assigned_group`, `approver_group`, `assigned_to`, `actor_group`) is an
`EntityReference` rather than a string, so a mapper cannot put a person's name there
without failing.

This is deliberate rather than incidental. The platform's standing rule is that
detector output names groups, queues and processes only; a concept set that offered a
bare "actor" would make violating that rule the path of least resistance for every
future pack author.

---

## Closed vocabularies

Every normalised token comes from a frozen set, validated at construction. A value
outside it **fails at the connector** rather than flowing downstream as an
unrecognised string a detector would silently mishandle — B0's rule, and the reason a
mapping gap surfaces where it can be fixed.

| Vocabulary | Values |
|---|---|
| `WORK_ITEM_TYPES` | incident, request, problem, change, task, issue, case, other |
| `STATUS_CATEGORIES` | open, in_progress, waiting, resolved, closed, cancelled, other |
| `PRIORITY_LEVELS` | critical, high, medium, low, none |
| `ACTOR_GROUP_TYPES` | team, queue, department, role, vendor, other |
| `ARTIFACT_TYPES` | document, page, attachment, code_file, commit, runbook, report, conversation, other |
| `CONTENT_TYPES` | prose, code, conversation, structured |
| `TRANSITION_TYPES` | status_change, reassignment, priority_change, escalation, reopen, other |
| `APPROVAL_DECISIONS` | pending, approved, rejected, withdrawn, expired, delegated |
| `APPROVAL_TYPES` | managerial, compliance, technical, financial, other |
| `ASSIGNMENT_TYPES` | initial, reassignment, escalation, delegation, other |
| `ENTITY_REFERENCE_TYPES` | person, team, project, object, process, system |

Three of these are shared rather than copied, and tests pin the sharing:

* `ENTITY_REFERENCE_TYPES` **is** the knowledge graph's `ENTITY_TYPES`, so a concept
  reference and a graph entity cannot disagree about what kinds of thing exist.
* `CONTENT_TYPES` covers the retrieval substrate's chunk vocabulary (plus
  `structured`, since a record is neither prose nor code nor conversation), so an
  artifact classified once needs no re-classification to be chunked or retrieved. It
  is also the vocabulary 2.0-B3's assembly source-type precedence keys on.

### Deliberately coarse

`STATUS_CATEGORIES` is coarse on purpose. Systems disagree wildly on status names and
a detector almost never needs the native one — ageing and backlog logic needs "is
this still open?", not forty status strings. The source's own value is always kept on
`native_status` for trace-back, so normalising never destroys it.

Two distinctions inside these vocabularies carry weight:

* **`cancelled` is not `resolved`.** Treating abandoned work as completed would
  overstate throughput for every detector downstream.
* **`reopen` is not `status_change`.** Rework is precisely the signal a detector
  hunts; collapsing it into a generic transition erases it.

---

## The mapping contracts

Defined in [`backend/discovery/concepts/contracts.py`](../backend/discovery/concepts/contracts.py).

A concept definition alone is not a contract. What a connector implementer needs is:
which fields must I populate, which are optional, which vocabulary does each token
come from, and how do I know if the rules changed under me.

**The contracts are data, not prose.** B0's contract lives in a markdown table plus
reference mappers — fine for one concept and three providers. At seven concepts across
a dozen connectors, a prose-only contract drifts the moment someone adds a field, and
nothing fails when it does. Here a test asserts every field a contract names exists on
the class implementing the concept, *and* that every model field has a contract entry.
A contract cannot describe a model that is not there, and a field cannot appear
undocumented.

Each contract carries `rules` — obligations a field constraint cannot express, which
are exactly the ones that get violated:

> "assigned_group is a group reference. If the source only records an individual
> assignee, do NOT synthesise a group from their name; leave it None and declare the
> gap."

### Versioning

Two levels, because they answer different questions:

| Version | Bump when | Effect on existing declarations |
|---|---|---|
| `CONCEPT_SET_VERSION` | a concept is added or removed | adding: none. removing: invalidates every declaration naming it |
| `MappingContract.version` | a concept's required fields or vocabularies change | invalidates every declaration for **that** concept |

The distinction matters at the moment it usually gets blurred. Adding a *concept* does
not invalidate any connector's conformance; adding a *required field* to an existing
concept invalidates every declaration for it. One number could not express both
without overstating or understating the breakage.

`BREAKING_CHANGE_RULES` states what obliges a bump, in the module — a rule kept only
in a reviewer's head is one that gets forgotten at the point it costs a connector
author their conformance. Summarised:

* adding a concept bumps the SET version, no contract version;
* adding a **required** field bumps that contract; adding an **optional** field does not;
* **removing** a value from a closed vocabulary bumps that contract (a connector may
  have been emitting it); **adding** one does not;
* renaming a field is a removal plus an addition — there is no in-place rename that
  preserves conformance.

`contract_summary()` is the serialisable audit surface: what a pack author reads to
learn the vocabulary, and what a reviewer diffs to see whether a bump was owed.

---

## Connector conformance

Declared in [`backend/discovery/concepts/conformance.py`](../backend/discovery/concepts/conformance.py).

The design question that decides whether this is worth anything: **conformance to
what — the connector's data or its code?** Both, kept apart, because conflating them
is how a conformance registry becomes a lie. A connector whose source genuinely
carries approvals but has no mapper yet is in a completely different position from one
whose source has no notion of approval at all, and a single boolean reports them
identically.

| Status | Meaning | Requires |
|---|---|---|
| `supported` | the source carries it **and** a mapper exists — the only status that claims conformance | a named mapper |
| `declared` | the source carries it; the mapper is not built yet | — |
| `gap` | the source cannot supply it | a reason |
| `not_applicable` | the concept does not apply to this source family | a reason |

**Nothing is `supported` yet, and that is the honest state at T1.** This ticket
defines the set, the contracts and the declaration mechanism; the mappers are T2/T3.
Recording `declared` now makes the remaining work visible instead of implied, and when
a mapper lands, flipping one status is the whole change. A test refuses `supported`
without a named mapper, so the strongest claim cannot be made by editing a comment.

`gap` and `not_applicable` are reported separately on purpose. A cloud-event stream
having no approvals is not a shortcoming to be fixed, whereas an ITSM tool whose
approvals we cannot read is. `declared_gaps()` returns only the former — a backlog
conflating them fills with items nobody intends to do, and then gets ignored
wholesale. That function is the surface B4 AC5 ("unmappable concepts recorded as
declared gaps, never silently approximated") builds on.

Thirteen shipped connectors are declared, covering the whole concept set each — a
partial declaration is refused, because an omitted concept is silently unmapped, which
is the exact ambiguity the registry exists to remove. The connector list is anchored on
`connector_roadmap.SHIPPED_CONNECTOR_IDS` (R191-R1's honesty rule): a connector whose
ingestion does not ship cannot conform, because there is nothing to conform.

### Notable recorded gaps

| Connector | Concept | Why |
|---|---|---|
| jira | `actor_group` | Jira assigns to individuals; roles and components are not work queues, and mapping an assignee to a group would fabricate one |
| jira | `approval` | approval is a per-project workflow convention, not a first-class record |
| github | `assignment` | PR review requests are per-person; there is no group queue |
| confluence / sharepoint | `actor_group` | the ingest surface exposes individual actors only |
| postgresql / sql_server / oracle_db | most workflow concepts | schema is per-customer; whether a table holds work items cannot be known without org-specific scope configuration |

---

## What T1 does not do

* **No mappers.** Turning a ServiceNow incident into a `WorkItem` is T2/T3. This
  ticket makes the target exist, versioned, with conformance declarable.
* **No detector porting.** AC2/AC3 (two detectors ported, one running across three
  families) are separate tickets.
* **No CI conformance-fixture gate.** AC4 is separate. The declaration mechanism here
  is what that gate will read.

---

# Connector mapping (2.0-B4 T2)

T1 made the target exist. T2 maps the connectors onto it, and records at FIELD level
what each one cannot carry.

> **AC5** — *Unmappable connector concepts are recorded as declared gaps, visible to
> pack authors — never silently approximated.*

## Where the code lives

| Module | Job |
|---|---|
| `discovery/concepts/mappers/__init__.py` | the mapper registry — `@maps(connector, concept)` registers at definition; `resolve_mapper` turns a conformance claim into a verifiable reference |
| `discovery/concepts/mappers/_common.py` | provenance spine, group-reference funnel, timestamp passthrough — shared so they cannot drift per connector |
| `discovery/concepts/mappers/servicenow.py` | the richest source: work item, actor group, assignment, state transition, CI reference |
| `discovery/concepts/mappers/jira.py` | work item, attachment, issue reference |
| `discovery/concepts/mappers/salesforce.py` | case, case history (transition + assignment), ProcessInstance approval, queue, record reference |
| `discovery/concepts/mappers/content.py` | Confluence, SharePoint, Slack, Teams, GitHub — artifacts, channels, references |
| `discovery/concepts/mappers/cloud_events.py` | AWS / Azure — the resource reference, and nothing else |
| `discovery/concepts/gaps.py` | the AC5 surface: the concept-first report, and `assert_no_approximation` |
| `app/routes_concepts.py` | `GET /api/concepts/{contracts,conformance,gaps,by-concept,connectors/{id}}` |

## What a mapper is

A pure function `(org_id, record, **ctx) -> concept | None`. No I/O, no client, no
environment, no DB — the same posture as MSP-B0's `reference_mappers`, and what lets a
golden fixture pin the whole normalised output. A structural test walks the package's
AST and fails the build on an `os.environ` read or on any `app.*` import other than the
pure `provenance` dataclass.

Three rules, each enforced by a test rather than asserted:

1. **A missing field stays `None`** — never defaulted to something plausible.
2. **An unmapped native value raises** — the model's vocabulary check is not caught and
   downgraded to `"other"`, because a silent `"other"` is the approximation AC5 exists
   to prevent.
3. **An individual is never turned into a group.** Where a source records only a person,
   the group-shaped field stays `None` and the gap is declared.

## Field-level gaps

T1's four statuses answer *can this connector produce this concept at all?* Building the
mappers showed the real question is finer — *ServiceNow supports `state_transition`, but
can it tell me which group moved the item?* So a declaration also carries `FieldGap`
entries with a `kind`:

* **`absent`** — never populated, for any record;
* **`partial`** — populated only under a stated condition.

A field gap must name a real contract field, must carry a reason, and cannot sit on a
**required** field beside a `supported` claim (a connector that cannot populate a
required field does not support the concept). `assert_no_approximation` raises if a
mapper populates an `absent` field; `partial` never raises, because both branches of a
stated condition are legal.

### Every field gap currently declared

| Connector | Concept | Field | Kind | Why |
|---|---|---|---|---|
| servicenow | `state_transition` | `actor_group` | absent | the audit row records that the state changed, not who changed it; the only mover field is an individual |
| servicenow | `assignment` | `assigned_to` | partial | the audit row stores the group's NAME, not its sys_id, so the reference is name-keyed by the source's own construction |
| servicenow | `assignment` | `hop_index` | partial | position in the chain is a property of the ordered history, supplied by the caller mapping the sequence |
| jira | `work_item` | `assigned_group` | absent | Jira assigns to a person; `JIRA_TEAM_FIELD` is per-deployment config, not a guaranteed field |
| jira | `work_item` | `closed_at` | absent | Jira has no close event separate from resolution; mirroring it would give a resolve-to-close detector a manufactured zero |
| salesforce | `work_item` | `assigned_group` | partial | only when `OwnerId` is a Queue (`00G`); a person-owned case has no group to name |
| salesforce | `work_item` | `resolved_at` | absent | Salesforce records `ClosedDate` only |
| salesforce | `state_transition` | `actor_group` | absent | `CaseHistory` records the user who made the change, never a group |
| salesforce | `assignment` | `assigned_to` | partial | only when the new owner is a Queue; a handoff to a person still counts the hop |
| salesforce | `approval` | `approver_group` | absent | the approver is on `ProcessInstanceWorkitem.ActorId`, a User in the common configuration |
| slack / teams | `artifact` | `revision` | absent | a thread has no version; an edit re-renders the whole thread |

`approval_type` on Salesforce is deliberately **not** a field gap: it is always `other`
because Salesforce does not classify a process, and the field IS populated — with the
vocabulary's neutral value — so calling it absent would be false. That fact lives in the
position's `reason`, which the API serves alongside the gaps.

## Mapping decisions worth knowing

* **The ServiceNow `{value, display_value}` trap.** Datetimes and class identifiers take
  the RAW half (canonical `YYYY-MM-DD HH:MM:SS` UTC); names and states take the display
  half. The mapper imports `servicenow.py`'s own accessors rather than re-deriving them,
  and the golden fixture's two halves differ deliberately so reading the wrong one fails.
* **Cancelled is not resolved, on both sources.** ServiceNow state 8 maps to `cancelled`.
  Jira files "Won't Do" / "Duplicate" under `statusCategory='done'`, so the mapper reads
  `resolution.name` as well and routes the abandoning resolutions to `cancelled` —
  otherwise abandoned work counts as delivered.
* **An unmapped status raises; an unmapped issue TYPE does not.** Nothing branches
  open-vs-closed on `work_item_type`, and Jira types are freely invented per project, so
  `other` is safe there and unsafe for a status.
* **Salesforce's owner branch is deterministic.** Key prefix `00G` is a Group (a Queue),
  `005` is a User — so the group-vs-individual question that forces a gap on Jira is
  answerable here from the id itself, with no name matching.
* **A channel is a `team`, never a `queue`.** Work is not routed to or drawn from a chat
  channel, and a queue-ageing detector reading one as a queue would report backlog that
  does not exist.
* **Cloud events map one concept only.** An `OperationalEvent` is already the right
  normalised shape for an alarm; re-expressing it as a `work_item` or a work-item
  `state_transition` would let an ageing detector measure the dwell time of a server. The
  resource reference is the one piece that genuinely crosses source families.
* **`entity_id` is never set by a mapper.** Resolution is the graph's decision; asserting
  one would claim a resolution nobody made.

## Reading the gaps

```
GET /api/concepts/gaps          # both orientations
GET /api/concepts/by-concept    # "which sources can carry my detector?"
GET /api/concepts/connectors/jira
```

In code: `concepts_usable_by("servicenow")`, `unpopulated_fields("servicenow",
"state_transition")`, and `connectors_for_detector("work_item", "actor_group")` — which
answers T3's portability question from the declarations rather than by trying it.

## Current coverage

| Concept | Supported by |
|---|---|
| `entity_reference` | all ten non-database connectors |
| `artifact` | confluence, github, jira, sharepoint, slack, teams |
| `actor_group` | salesforce, servicenow, slack, teams |
| `work_item` | jira, salesforce, servicenow |
| `state_transition` | salesforce, servicenow |
| `assignment` | salesforce, servicenow |
| `approval` | salesforce |

Ten `declared` entries remain, each naming a source that carries the concept and a read
the connector does not yet perform — a work list, not a formality. The largest are
ServiceNow approvals (`sysapproval_approver` is unread), the Jira changelog (no
`expand=changelog`), GitHub's per-PR surface, and the native databases, whose schemas are
per-customer.

---

## What T2 does not do

* **No detector porting.** AC2/AC3 (two detectors ported, one running across three source
  families) are T3. `connectors_for_detector` is the surface they will read.
* **No CI conformance-fixture gate.** AC4 is separate; the golden fixture here
  (`discovery/tests/fixtures/concept_mapping_samples.json`) is the shape that gate will
  require of every connector.
* **No connector read-scope changes.** Every mapper reads fields the connector already
  requests. A concept needing a new read is `declared`, not mapped — mapping a field
  nobody fetches would produce a concept that is always empty.
