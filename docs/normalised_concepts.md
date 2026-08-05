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
  families) are separate tickets. **AC2 is now delivered — see the next section.**
* **No CI conformance-fixture gate.** AC4 is separate. The declaration mechanism here
  is what that gate will read.

---

## Detector portability (2.0-B4 T3 / AT-812 — AC2)

**AC2:** *two existing detectors, ported to normalised concepts, produce identical
findings on golden fixtures.* This is the proof that the concept set is not just a
data model but a *portable* one — a detector re-expressed against concepts behaves
exactly as its connector-bound original.

Two shipped detectors are ported in
[`backend/discovery/concepts/portable_detectors.py`](../backend/discovery/concepts/portable_detectors.py):

| Original (connector-bound) | Concept-native port | Concepts it reads |
|---|---|---|
| `APPROVAL_BOTTLENECK` — [`detectors/approval_delay.py`](../backend/discovery/detectors/approval_delay.py) | `detect_approval_bottleneck` | `Approval` |
| `PERMISSION_BOTTLENECK` — [`detectors/permission_bottleneck.py`](../backend/discovery/detectors/permission_bottleneck.py) | `detect_permission_bottleneck` | `Approval` + the `ActorGroup` its `approver_group` points at |

The originals read `sf_data['approval_processes'][i]['avg_delay_days']` — bound, by the
field names they know, to Salesforce. The ports read a concept stream: a flat list of
`ConceptSignal` filtered to `Approval` gates and their approver `ActorGroup`s. They name
no source and no source field path (a test sweeps the module to prove it), and they emit
nothing when handed the raw connector dicts — they respond only to concept instances.

**Same logic, only the input is normalised.** The ports import the threshold constants
from the original modules rather than re-declaring them, so the calibration is provably
identical and cannot drift: change a threshold in the original and the port changes with
it. The one thing that differs is the shape the detector reads.

**Where the discriminating numbers live.** `approver_count` — what `PERMISSION_BOTTLENECK`
keys on — is read from the normalised `ActorGroup.member_count`, a first-class concept
aggregate, and the mapper drops the source's individual approver roster entirely (groups,
never individuals). The source's own pre-computed scores (`avg_delay_days`,
`bottleneck_score`, `pending_count`) ride on the `Approval.attributes` bag — B0's `payload`
rule: the source computed them, so the faithful mapping carries them rather than fabricating
per-approval detail the source never recorded. The port never reaches into a connector dict
for any of them.

The mapper is [`backend/discovery/concepts/mappers.py`](../backend/discovery/concepts/mappers.py)
(`map_service_cloud_approvals`). It is a *proof* mapper over a detector-visible shape, and
deliberately does **not** flip the Salesforce connector's conformance to `supported` — the
conformance registry tracks the shipping ingest mapper, which is T2's remit.

The proof — [`backend/tests/unit/test_r2_0_b4_t3_detector_portability.py`](../backend/tests/unit/test_r2_0_b4_t3_detector_portability.py)
— feeds the golden fixture
([`concepts/fixtures/portability_approvals_golden.json`](../backend/discovery/concepts/fixtures/portability_approvals_golden.json))
to both the original and the port and asserts `DetectorResult` lists are byte-identical, across
every firing branch (combined-delay, severe-delay, concentration-only, the `approver_count == 0`
guard, and a non-firing negative control). An explicit expected firing set is asserted against
**both** sides, so a shared bug that agreed on the wrong answer would still fail.

**Not in this ticket.** AC3 (one concept-native detector running unchanged across three source
families) is a separate ticket; it builds on this port and the T2 per-connector mappers.

---

## Conformance fixture suite + CI gate (2.0-B4 T5 / AT-814 — AC4)

**AC4:** *every connector has conformance fixtures; CI fails if a connector lacks them.* The
discipline the story states plainly: *a new connector ships with its conformance fixtures or
does not ship.*

**One golden fixture per shipped connector**, at
[`backend/discovery/concepts/fixtures/conformance/<connector_id>.json`](../backend/discovery/concepts/fixtures/conformance/),
one per entry in `conformance.CONFORMANCE` (13 today). Each fixture carries:

* a **locked snapshot** of that connector's per-concept declaration — `status`, `reason`,
  `mapper` for all seven concepts — pinned to `discovery/concepts/conformance.py` by the gate;
  and
* zero or more **mapping cases**: a `raw` source sample, the `mapper` that maps it (named as a
  `"module:function"` string), and the exact normalised concepts (`expected`) it must produce.

**The CI gate** is
[`backend/tests/contract/test_r2_0_b4_t5_conformance_fixtures.py`](../backend/tests/contract/test_r2_0_b4_t5_conformance_fixtures.py).
It lives under `tests/contract` on purpose — CI runs `pytest tests/contract/`
(`.github/workflows/contract-tests.yml`), so the gate actually runs in CI. It enforces:

1. **Presence (the AC4 gate)** — every shipped connector has a fixture; a missing one fails,
   an orphan fixture fails. A negative control removes a fixture and asserts the gate trips,
   so the gate is known to be a gate.
2. **The fixture is a true lock on the registry** — status/reason/mapper for every concept must
   match the declaration, so a conformance change cannot ship without updating its golden
   fixture, and a newly-shipped connector cannot arrive with no fixture.
3. **Gaps and not-applicables carry reasons** (AC5's honesty, re-checked at the fixture).
4. **Mapping cases prove the mapper** — the named mapper is run on `raw` and its output must
   equal `expected` exactly; this is how a fixture *proves the mapping is correct* rather than
   only asserting a status. Today one real mapping is proven: Salesforce's approval block →
   `Approval` + approver `ActorGroup`, via the T3 `map_service_cloud_approvals` mapper.
5. **A `supported` claim needs a proving case** — the only status that asserts conformance cannot
   be set without a mapping case that produces the concept (a forward guard for the T2 mappers;
   vacuous today because nothing is `supported` yet).

The loader and mapper resolution live in
[`backend/discovery/concepts/conformance_fixtures.py`](../backend/discovery/concepts/conformance_fixtures.py)
(not in the test), so the T2 per-connector mappers register against these same fixtures as they
land — flipping a `declared` concept to `supported` is then exactly: add a mapping case, run the
gate, flip the status.
