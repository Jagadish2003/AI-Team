# Cross-Source Entity Resolution — the ranked engine (2.0-B2 T1)

The knowledge graph holds entities from individual sources. The same service,
application, team, or customer therefore shows up in ServiceNow, Jira,
Salesforce, code, and cloud events as several unrelated entities — which caps how
much cross-system corroboration is honestly possible. A finding "corroborated
across ServiceNow and Jira" only means something if both sides are about the same
thing.

`backend/app/cross_source_resolution.py` decides whether two entities ARE the same
real thing. It decides in three tiers, **ranked by the strength of the evidence**,
and only the top two are allowed to merge without a human.

> **The governing risk.** A wrongly merged entity corrupts every finding built on
> it, and the corruption is invisible. Every rule below exists to make a wrong
> merge hard, and to make an uncertain one *visible* instead of quiet.

---

## The three tiers

| Rank | Tier | Evidence | Action | Confidence |
|---|---|---|---|---|
| 1 | `explicit_reference` | The source data itself points at the other entity's record | **auto-merge** | 1.0 |
| 2 | `alias_mapping` | The org's Owner-managed alias table says they are the same | **auto-merge** | 0.95 |
| 3 | `name_similarity` | Exact normalised name match **plus** a corroborating observed relationship | **propose only — never merged** | 0.7 |

Tiers are consulted strongest-first and **the first tier to produce a decision
wins**; a weaker tier can never override a stronger one's answer.

### Tier 1 — explicit cross-references (auto-merge)

A machine stated the identity, so no human is needed. Three exact shapes:

- `subject_references_candidate` — the subject cites the candidate's own
  `(source_system, source_record_id)`;
- `candidate_references_subject` — the reverse (which side carries the reference
  is an accident of the connector);
- `shared_external_reference` — both cite the **same third-party record** that is
  neither entity's own identity (e.g. a Jira project entity and a git repo entity
  both carrying the same CMDB CI sys_id).

### Tier 2 — org-configured alias mappings (auto-merge)

A **human** stated the identity — recorded, attributable, and reversible — which
is why it is allowed to merge. Tier 1 cannot cover the cases where two systems
simply never reference each other (ServiceNow calls it `Payments API`, the repo
calls it `payments-api`), and tier 3 is not allowed to merge.

The table lives in `backend/app/entity_alias_mappings.py`. See
[The alias table](#the-alias-table) below.

### Tier 3 — name similarity (proposal only)

"Similarity" is deliberately **exact normalised equality**, not a fuzzy distance.
A distance threshold is a dial someone will turn up, and every turn silently
widens what the platform is willing to call the same thing.

Two further requirements before a name match becomes a proposal:

- **a corroborating observed relationship** — both entities have an observed edge
  to a *common third entity* of the *same relationship type* ("both depend on the
  same CI"). Without it, every reused word (`admin`, `core`, `billing`) in two
  systems becomes a proposal, and a review queue nobody can finish is a review
  queue nobody uses;
- **different source systems** — two same-named entities within one source are
  already `app.entity_resolution`'s business.

**Inferred edges never corroborate.** An inferred edge is a co-firing hypothesis;
corroborating a proposal with one would stack a guess on a guess.

A name match that fails either requirement is not silently dropped — it is
recorded on the decision under `considered.name_matches_not_proposed` with the
reason, so the engine's silence is explainable.

---

## Why tier 3 can never merge — structurally, not by convention

- `AUTO_MERGE_TIERS` is a frozenset that **excludes** `TIER_NAME_SIMILARITY`.
- `action_for_tier()` is the single place an action is decided, and it **fails
  closed**: anything not in that set — including a tier added later — can at most
  be proposed.
- `ResolutionPolicy` carries no field that can move a tier across the boundary,
  and there is deliberately **no env var and no `force` parameter** anywhere in
  the module.
- A test iterates every policy permutation and asserts a name match never yields
  a merge.

Policy fields can only make the engine *more* conservative (propose less,
consider fewer candidates).

---

## Gates applied before any tier

| Gate | Rule | Why |
|---|---|---|
| org | A candidate from another org is dropped and **counted** | Cross-tenant identity leakage is both wrong and a breach |
| entity type | A `team` "Payments" is never the `system` "Payments" | Different kinds of thing |
| self | An entity never resolves to itself | — |
| status | Only a `resolved` candidate can be a merge target | An `ambiguous` row is the standing engine's recorded uncertainty; merging onto it would launder that into confidence |

## Ambiguity never merges

Two or more distinct targets at a tier → `ambiguous`, no merge, **every candidate
recorded** so a human can see exactly what collided (the N+1 discipline
`app.entity_resolution` already applies).

An ambiguous tier **stops** resolution — it does not fall through to a weaker
tier. Disagreeing explicit references are a source-data problem to fix at the
source, not licence to merge on a weaker signal.

---

## The cross-reference metadata convention

Tier 1 needs the source data's own statement of identity. An entity carries its
identity in `(source_system, source_record_id)` and its references to other
systems' records in `metadata`. These are the **only** three forms read — an
unrecognised key is ignored, because a reference is never inferred from a field
that merely looks like an id:

```jsonc
{
  // 1. Preferred, first-class form — populate this from new producers.
  "cross_references": [
    {"system": "jira", "record_id": "PAY-1", "field": "correlation_id"}
  ],

  // 2. Convenience map form.
  "external_ids": {"jira": "PAY-1", "servicenow": "9f2c..."},

  // 3. Enumerated single-field keys whose NAME states the target system.
  "ci_sys_id": "9f2c..."
}
```

Enumerated keys (`KNOWN_CROSS_REFERENCE_KEYS`): `jira_issue_key`, `jira_key`,
`ci_sys_id`, `cmdb_ci_sys_id`, `servicenow_sys_id`, `salesforce_id`,
`github_repo_id`, `repo_id`.

`correlation_id` is **deliberately absent**: it is a free-form ServiceNow field
whose target system is configured per org (`SERVICENOW_JIRA_KEY_FIELD`), so a
producer must publish it through `cross_references` with the system named.

A **self-reference** (a reference to the entity's own system) is dropped — it
carries no cross-source information and would let an entity match every sibling
from its own source that happens to share a field value.

---

## The alias table

Owned by `backend/app/entity_alias_mappings.py`. One entry asserts that several
names mean one thing, for one entity type:

```json
{
  "entity_type": "system",
  "canonical": "payments-api",
  "aliases": ["Payments API", "svc-payments"],
  "note": "confirmed with the platform team",
  "created_by": "owner@example.com"
}
```

Rules that keep it trustworthy enough to auto-merge:

- every mapping is scoped to **one** `entity_type`;
- aliases normalise through the **same** canonicalisation the entity layer uses
  (`entity_resolution.canonical_name_for`), so the table and the graph cannot
  disagree about what a name is;
- a group whose aliases are all identical to its canonical name is **rejected** —
  it asserts nothing and would make the table look configured when it is not;
- **a conflicting table is rejected, not resolved.** If one alias were claimed by
  two groups of the same type, the merge target would depend on iteration order —
  that is how a wrong merge ships invisibly. `normalize_alias_mappings` raises
  `AliasMappingConflict`.

**Storage.** Org-scoped, read through the existing `kv` layer under
`entity_alias_mappings:{org_id}`. `put_alias_mappings` is the only writer, so an
invalid table can never be persisted. A stored table that fails validation
degrades to "tier 2 contributes nothing" with a loud warning — never a broken run
and never a merge on a half-read table.

**`ENTITY_ALIAS_MAPPINGS`** (env) overrides the stored table for offline/dev, or
for a deployment that prefers to declare it as configuration. A JSON array, or an
object keyed by org id with a `default`/`*` fallback — the `ENTERPRISE_APP_REPOS`
shape. A malformed override **raises**: an operator who configured it deliberately
must see the mistake.

---

## Using it

```python
from app.cross_source_resolution import resolve_org_entity_type, merge_decisions, proposal_decisions

decisions = resolve_org_entity_type(org_id, "system")   # read-only
for decision in merge_decisions(decisions):
    ...   # decision.merge_target — authorised by tier 1 or 2
for decision in proposal_decisions(decisions):
    ...   # decision.proposals — for human confirmation ONLY
```

The engine (everything above "DB-backed loaders" in the module) is **pure** — no
DB, no writes, no clock — so a decision is reproducible and testable without
PostgreSQL. `load_resolution_entities` / `load_relationship_index` /
`resolve_org_entity_type` are the read-only loaders that feed it, org-scoped in
SQL so a candidate pool cannot contain another tenant's entity in the first
place.

## The review surface (T3)

Tier 3 produces questions, not answers. `backend/app/entity_match_proposals.py`
is where those questions are parked so an Owner/Analyst can answer them, and
where the answer is kept.

| | |
|---|---|
| Queue + counts | `GET /api/entity-match-proposals[?status=pending\|confirmed\|rejected]` |
| One proposal + history | `GET /api/entity-match-proposals/{proposal_id}` |
| Confirm / reject | `POST /api/entity-match-proposals/{proposal_id}/decision` |
| Recompute proposals | `POST /api/entity-match-proposals/scan` |
| Role | `analyst` or above (the story's Owner/Analyst surface; a viewer has nothing actionable) |
| UI | **Entity Matches** (`/entity-matches`) |

Three rules shape the store:

1. **One question per pair, not one per direction.** The engine resolves each
   entity independently, so a proposed pair arrives twice (A→B and B→A).
   `proposal_id_for` derives a deterministic id from the *order-independent* pair,
   so the two collapse into one row an analyst answers once — and a later scan
   upserts that row instead of growing a duplicate queue.
2. **An answered question is never asked again.** The `ON CONFLICT` update is
   gated on `status = 'pending'`, so a later scan cannot revert an answer or
   overwrite the evidence it was given against. The count of pairs left alone is
   *reported* (`skipped_already_decided`), not hidden.
3. **Recording a decision is not applying one.** Confirming records a durable,
   attributable statement that two entities are the same thing.
   `confirmed_pairs(org_id)` is the read a merge applier consumes; this module
   never writes to `entities` or `entity_relationships`, and a test greps for that.

History is append-only: reversing a decision appends a new forward row, so the
original answer and its author survive. Every decision also emits the
`entity_match_proposal_decided` audit event. The scan deliberately does not — it
can only add or refresh pending questions, never change an answer.

### Durability across runs (T4)

Two things were missing for "durable **across runs**" to be true rather than
merely intended.

**1. Runs now refresh the queue.** `discovery/runner.py` calls
`scan_for_proposals(org_id)` after relationship mapping — after, because tier 3's
corroboration reads the observed edges the run just wrote. Non-blocking, for the
same reason entity extraction is: a review queue is not worth failing a run over.
Before T4 the only producer was the manual `POST …/scan`, so no run ever exercised
the durability rule.

**2. A decision is keyed on the pair's stable source identity, not its row ids.**
`proposal_id` hashes entity **row ids**, which is right for addressing a row and
wrong for remembering a decision, because row ids churn. The clearest case:

> A connector starts supplying record ids. `upsert_source_entity` keys on
> `(source_system, source_record_id)`, does not match the name-only row already
> there, and inserts a **second resolved row** for the same real entity. The pair
> now has a different `proposal_id` — so on T3's logic alone it is asked again,
> despite having been answered.

`identity_key` closes that. Per side the identity is `{source_system}|name:{canonical_name}`:

| Part | In the key? | Why |
|---|---|---|
| source system | yes | the pair only exists because the two sides are different systems |
| canonical name | yes | **invariant for the pairs this table can hold** — only `name_similarity` can propose, and it requires exact canonical-name equality, so the name *is* the pair's joint identity, and it is what the reviewer answered about |
| source record id | **no** | it is the part that *churns*; keying on it would change the key exactly when durability is needed |

A rename does produce a new key. That is correct rather than a gap: once the names
diverge the tier's premise is gone and the pair is not proposed at all; if both
sides are renamed alike, it is a genuinely new question.

`decided_identity_keys(org_id)` is the read `record_proposals` consults before
writing — confirmed *and* rejected both count, since "not the same thing" is as
durable an answer as "the same thing".

**Existing installs heal themselves.** Rows written before T4 have
`identity_key IS NULL`, so the check above cannot see them. `backfill_identity_keys`
recomputes the key from the row's own `evidence_payload` — T3 already stored both
sides' source system and name for the reviewer, which is exactly what the key needs
— and `record_proposals` calls it every pass. It only touches NULL rows, so it is a
no-op once healed, and a row whose snapshot cannot supply an identity is left NULL
rather than given a wrong key (a wrong key would silently suppress a *different*
pair's question).

Schema: `identity_key VARCHAR(64)` + `idx_entity_match_proposals_org_identity`,
migration `0033`, mirrored in `provision.sql`.

`SCANNABLE_ENTITY_TYPES` excludes `person` on purpose: two real people share a
name far more often than two systems do, so those proposals would be both the
highest-risk merge and the hardest to judge from a screen.

## Applying a merge, with provenance (T2)

`backend/app/entity_merge.py` is the **only** place a decision becomes a change to
the graph. T1 decides and writes nothing; T3 records a human answer and writes
nothing to the graph; a merge happens here or not at all.

| | |
|---|---|
| One entity's provenance | `GET /api/entities/{entity_id}/provenance` |
| Many at once (the finding-view seam) | `POST /api/entities/provenance` |
| Apply what T1/T3 authorised | `POST /api/entity-merges/apply` |
| Role | `analyst` or above |

### What a merge writes

On the **survivor**, `metadata.merge_provenance`:

```json
{
  "version": 1,
  "entity_id": "e1",
  "constituents": [
    { "entity_id": "e1", "source_system": "servicenow", "source_record_id": "sn-1",
      "is_origin": true,  "rule": null },
    { "entity_id": "e2", "source_system": "jira", "source_record_id": "PAY",
      "is_origin": false, "rule": "explicit_reference",
      "merged_at": "…", "merged_by": "system", "confidence": 1.0 }
  ],
  "rules": ["explicit_reference"],
  "source_systems": ["jira", "servicenow"],
  "is_merged": true
}
```

On the **constituent**, `metadata.merged_into` — a pointer, not a deletion.

Three properties that block specific ways provenance goes wrong:

* **The list is complete.** The survivor's own identity is a constituent
  (`is_origin: true`). Without it the node cannot honestly say which systems it
  speaks for — the exact fact the corroboration uplift (T5) has to trust.
* **The rule is per constituent.** A node merged from three sources may have been
  merged by three rules, on three days, by three actors. One rule field would have
  to lie about two of them. An earlier rule is never rewritten by a later merge.
* **A human confirmation is its own rule** (`confirmed_proposal`), never the tier
  that proposed it. A name match cannot authorise a merge; the person who
  confirmed it did, and the provenance says so.

### What a merge deliberately does NOT do

* **Delete anything.** The constituent row, its identity, and its edges survive.
  Deleting would destroy the evidence AC2 requires and make unmerge impossible.
* **Change `resolution_status`.** That records how the *standing* engine resolved
  the row — a different fact from "this was merged".
* **Hide the constituent** from any list. Display consolidation is a separate
  concern; nothing should vanish from a customer's graph because a rule fired.

Merges are **deterministic** (`choose_survivor`: existing survivor → stable
`source_record_id` → oldest → lowest id, a total order), **transitive** (both sides
resolve to their current survivor first, cycle-guarded), and **idempotent**
(re-applying reports `already_merged` and writes nothing). Every applied merge
emits the `entity_merged` audit event.

Applying is a deliberate, explicit step — it is not wired into the discovery run.
That is an operational decision, and a merge is irreversible until unmerge ships.

## The corroboration identity gate (T6)

Cross-source corroboration is the platform's strongest confidence signal: two
independent systems agreeing takes a finding to HIGH. That only means something if
both are talking about the same thing.

Before T6, COR-01 (ServiceNow) and COR-02 (Jira) fired on a **detector** link and
nothing checked identity — so a ServiceNow team "Payments" and a Jira project
"Payments", two unrelated things sharing a word, produced "Corroborated across
ServiceNow and Jira" and a HIGH. `backend/app/corroboration_identity_gate.py`
closes that: **a same name is not a shared identity.**

### When the gate engages

Only when *both* cross-source rules fired **and** the two sides' entity references
claim to be one thing — equal normalised names from different source systems. That
is the shape a reader interprets as "about one entity", and the only shape this
gate judges.

| Both sides reference an entity? | Same normalised name, different sources? | Genuinely resolved? | Cross-source elevation |
|---|---|---|---|
| no | — | — | unchanged (no identity claim) |
| yes | no | — | unchanged (no identity claim) |
| yes | yes | **yes** | **allowed**, basis recorded |
| yes | yes | **no** | **refused** — the AC5 case |

Corroboration that never made an identity claim (a ServiceNow *team* and a Jira
*process*) still rests on the pre-existing detector link and is untouched — T6 is
not a licence to silently downgrade it. But the result always records
`identity_verified`, so a reviewer can tell a HIGH resting on a **proven shared
identity** from one resting only on a shared detector.

### What counts as genuinely resolved

Strongest first, and every one is a statement a machine or a human actually made:

1. the two references are the **same entity row**;
2. they resolve to the same **T2 merge survivor** — the merge that actually
   happened, reported with the rule its provenance recorded. Authoritative over
   any re-derivation (an alias table edited after a merge could otherwise make the
   gate disagree with what the graph already did);
3. a human **confirmed** the pair in the T3 review surface;
4. the **T1 ranked engine auto-merges** them — an explicit cross-reference or the
   org alias table (the answer for a pair not yet applied).

A name match can never be a basis. `RESOLVED_BASES` has no name entry, step 4 asks
T1 for a *merge* and T1's name tier is structurally incapable of producing one, and
step 2 reads only rules T2 was permitted to merge on. A test pins
`RESOLVED_BASES == T1's AUTO_MERGE_TIERS + {same_entity, confirmed_proposal}` so
the two layers cannot drift into the gate trusting something T1 does not.

### Degradation fails CLOSED

An identity claim that is not *positively* resolved never elevates — whatever the
reason (unresolved, resolver error, no resolver). Elevating on an unreadable graph
would reopen this hole exactly when the system is unhealthy, and a wrong HIGH is
the harmful direction: it gets quoted in a board paper, while a conservative MEDIUM
merely waits for the next run. Every refusal records its reason, so a lost
elevation is visible as a refusal rather than looking like a genuine downgrade.

The one fail-open path is the gate module failing to **import** — a packaging fault
CI catches, logged with the consequence named.

### Where it shows up

`CorroborationResult.identity_gate` carries `applied`, `identity_verified`,
`identity_claim`, `blocked_rules`, `basis`, `reason`, and both references.
Blocking removes the **elevation**, not the evidence: COR-01/COR-02 stay on the
result and the card still explains what each system found.

## What is still to come

**Unmerge** (restore constituents, flag dependent findings for re-evaluation) is
the remaining 2.0-B2 task. `merged_constituents()` and the `merged_into` pointer
are what it will read. An Owner-facing editor for the alias table is also still
open — `put_alias_mappings` is the validated seam it will write through.
