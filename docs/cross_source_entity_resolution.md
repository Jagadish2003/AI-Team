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

## What T1 does NOT do

**T1 decides; it never writes.** Applying a merge with its provenance, the
proposal review/confirmation workflow, unmerge, and the corroboration uplift are
the later 2.0-B2 tasks that consume these decisions. An Owner-facing surface for
editing the alias table is likewise out of scope here — `put_alias_mappings` is
the validated seam it will write through.
