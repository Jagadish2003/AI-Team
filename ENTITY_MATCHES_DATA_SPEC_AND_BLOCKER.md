# Entity Matches (2.0-B2 T3) — why the page is empty, and what data would fill it

**Prepared for:** SME data team + the 2.0-B2 owner
**Date:** 9 August 2026
**Environment examined:** dev (`agentiqdev`), org `d64ee43b-5235-4894-a336-fc1cc26d9fa6`

---

## Summary

The **Entity Matches** page (`/entity-matches`) is empty for two separate reasons. One has
been fixed. The other is a code blocker that **no amount of source data can work around**.

| # | Cause | Status |
|---|---|---|
| 1 | The `entity_match_proposals` table did not exist — six migrations were unapplied | **Fixed** (DB now at `0041`) |
| 2 | The upstream entity resolver merges same-named entities across sources on **name alone**, so the pair the review queue needs can never exist | **Open — needs an engineering decision** |

**Please read section 2 before commissioning any SME data work.** Feeding perfect data today
will still produce an empty page.

---

## 1. Missing tables — fixed

The dev database was at Alembic revision `0035`; the repo head is `0041`. Four tables were
absent, including the one this page reads:

- `entity_match_proposals`
- `entity_match_proposal_history`
- `entity_unmerges`
- `finding_reevaluation_flags`

Migrations `0036`–`0041` have been applied. The database is now at `0041` and the tables
exist. Audit rows were unaffected (2,720 before and after).

> **Note for whoever provisions environments:** `opportunity_feedback` and
> `ranking_adjustments` already existed despite their migrations being unapplied, so the
> recorded Alembic revision was not a reliable description of the real schema. Worth checking
> other environments the same way rather than trusting the version number.

---

## 2. The blocker — the resolver merges on name alone

### What happens

`app/entity_resolution.py` resolves an incoming entity against existing rows using:

```sql
SELECT * FROM entities
 WHERE org_id = %s AND entity_type = %s AND canonical_name = %s
```

`app/entity_resolution.py`, line 230.

**`source_system` is not part of the key.** So when a Jira entity called "Payments Operations"
arrives after a ServiceNow entity of the same name and type, the resolver finds the existing
row and folds the new one into it. The result is **one row, not two**.

### Why that empties the page

The Entity Matches page shows only **tier-3 proposals** — pairs whose only evidence of shared
identity is a matching name. Tier 3 requires, by design, **two separate entity rows from two
different source systems**.

The resolver has already merged them before the proposal engine ever runs. Its input cannot
exist.

### Evidence

1. **Reproduced directly.** Creating the same-named entity twice through the application's own
   writer (`resolve_or_create_entity`) returned the **same entity id** both times. Producing
   two rows required bypassing the resolver with a direct database insert.
2. **Confirmed across the whole database.** Querying every organisation for a
   `canonical_name` + `entity_type` appearing under more than one `source_system` returns
   **zero rows**. Not few — none. The resolver has collapsed every such case.

### The design question this raises

The 2.0-B2 story states the rule explicitly:

> *"Explicit references and configured mappings resolve. Fuzzy name similarity **proposes** and
> never silently merges. A wrongly merged entity corrupts every finding built on it, and the
> corruption is invisible — the worst failure mode in the platform."*

`app/cross_source_resolution.py` enforces that rigorously — its `AUTO_MERGE_TIERS` set
structurally excludes the name tier, and a test sweeps every policy permutation to prove no
configuration can cross that line.

But the older resolver upstream performs exactly that merge, silently, before tier 3 sees
anything. **Two layers of the platform hold contradictory rules about whether a name match may
merge two entities.**

Two consequences worth putting in front of the B2 owner:

1. The Entity Matches page is structurally empty in **any** deployment, not only this one.
2. **B2's AC5 may still be open.** AC5 requires cross-source corroboration to rest on *resolved*
   identity — but identity is currently being established by name matching, which B2 itself
   declares insufficient. A finding labelled "corroborated across ServiceNow and Jira" may be
   resting on a name coincidence.

### What we are *not* recommending unilaterally

Changing the resolver's lookup key affects every finding, every graph edge and every
corroboration decision in the platform. That is a scoped engineering change with its own
testing and regression burden — **it should be a deliberate decision by the 2.0-B2 owner**, not
a quick patch. This note raises it; it does not presume the answer.

---

## 3. Data specification — for when the blocker is resolved

Everything below is required *in addition* to the fix above. It is accurate against the
current extractor (`app/entity_extractor.py`) and resolution engine
(`app/cross_source_resolution.py`).

### 3.1 Which entity types each connector can produce

| Entity type | Salesforce | Jira | ServiceNow |
|---|:--:|:--:|:--:|
| `team` | ✅ | ✅ | ✅ |
| `project` | ✅ | ✅ | — |
| `object` | ✅ | — | ✅ |
| `system` | — | — | ✅ **only** |

**Important limitation.** The intuitive scenario — *"the Payments API appears in both
ServiceNow and Jira"* — **cannot arise today**. Only ServiceNow produces `system` entities, and
a proposal pair must share an entity type.

### 3.2 Viable pairings

Target one of these instead:

| Pairing | Entity type | Difficulty |
|---|---|---|
| Salesforce ↔ Jira | `team` or `project` | **Easiest** |
| Salesforce ↔ ServiceNow | `team` or `object` | Moderate |
| Jira ↔ ServiceNow | `team` | Moderate |

`person` entities are **deliberately excluded** from scanning — two real people share a name far
more often than two systems do.

### 3.3 Names must match exactly

Names are compared after normalisation (case and surrounding whitespace folded). Nothing
fuzzier. No abbreviation, stemming, or partial matching.

| ✅ Will match | ❌ Will not match |
|---|---|
| Salesforce team `Payments Operations`<br>Jira project `Payments Operations` | `Payments Operations` vs `Payments Ops` |
| | `Payments Operations` vs `PAYOPS` (project key) |
| | `Payments Operations` vs `Payments Operations Team` |

**Action for the SME team:** agree one canonical spelling per team/project/object and use it
verbatim in every system.

### 3.4 Where each name is read from

| Connector | Entity created | Source field |
|---|---|---|
| Jira | `team` and `project` | `issue_metrics.project` — the project **name**, not the key |
| Salesforce | `team` | team record `name` / `Name` |
| Salesforce | `project` | project name field |
| Salesforce | `object` | record display-name fields |
| ServiceNow | `system` | CMDB CI `name` |

### 3.5 The requirement most often missed — a shared relationship

**A matching name alone is not enough.** Both entities must additionally have an **observed
relationship to the same third entity**:

```
    Payments Operations                Payments Operations
    team / salesforce        <->       team / jira
             \                                /
              \                              /
               ---->   a shared entity   <---
                     (observed edges, both sides)
```

So the seeded data must also produce a relationship — for example both sides linked to the same
service, or both owning the same object. Without that shared neighbour, no proposal is created
even with a perfect name match.

The edge must be **observed**, not inferred. Inferred edges do not corroborate, on the grounds
that a proposal resting on an inferred edge would stack a guess on a guess.

### 3.6 Full checklist

For a proposal to appear, **all** of these must hold:

- [ ] Two entities of the **same** `entity_type`
- [ ] From **different** `source_system` values
- [ ] With **exactly equal** normalised names
- [ ] Both `resolution_status = 'resolved'`
- [ ] Entity type is one of `system`, `team`, `project`, `object` (**not** `person`)
- [ ] Both linked by an **observed** relationship to the same third entity
- [ ] Both in the same organisation
- [ ] **And the resolver blocker in section 2 has been resolved**

---

## 4. Demonstration currently in the dev database

A qualifying pair was seeded manually so the page could be seen working. It is **fabricated
data**, inserted directly into the database — no connector produced it, and its record ids
(`seed-jira-1`, `seed-sn-1`) are hand-written, not from Jira or ServiceNow.

Identifiable by: display names beginning `Seeded Demo`, `first_seen_run_id =
'run_seed_entity_match'`, and `metadata.seeded = true`.

**To remove it:**

```sql
DELETE FROM entity_relationships WHERE first_seen_run_id = 'run_seed_entity_match';
DELETE FROM entity_match_proposals WHERE org_id = 'd64ee43b-5235-4894-a336-fc1cc26d9fa6';
DELETE FROM entities WHERE first_seen_run_id = 'run_seed_entity_match';
```

Anyone else using this dev organisation will currently see these rows in their graph and a
proposal they did not create. Worth removing once the demonstration has served its purpose.

---

## 5. Recommended sequence

1. **Decide the resolver question** (section 2) — 2.0-B2 owner. Nothing else is worth starting
   until this is settled.
2. **Assess the AC5 exposure** — whether existing "corroborated across two systems" findings are
   resting on name-only identity.
3. **Then** commission the SME data work in section 3.
4. Re-run discovery, then either wait for the automatic scan or use **Scan for matches** on the
   page.
5. Remove the seeded demonstration rows (section 4).

Steps 2 and 3 can proceed in parallel once step 1 is decided. Consistent cross-system naming is
worth doing on its own merits regardless of the outcome — the entire cross-source story depends
on it.
