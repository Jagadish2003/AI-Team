# Storing the projection — the 2.0-A2 handoff

**Story:** 2.0-A1 (Intervention Modelling), task 6 — *"Store the final projection with the opportunity so future outcome tracking in 2.0-A2 can compare actual results against the original projection."*
**Implementation:** `backend/discovery/projection/provenance.py` (the identifying stamp) and `backend/app/projection_store.py` (write + read).
**Acceptance criterion:** AC6.

> The story's own note: *"AC6 is small and load-bearing: without a stored projection there is nothing to validate against, and the flywheel never starts. Store the projection even if the UI does not display all of it."*

---

## 1. What is stored

The whole computed projection, plus a provenance stamp that identifies it.

| Part | Where |
|------|-------|
| Direction | `projection.direction` |
| Magnitude band | `projection.magnitudeBand` (`null` when direction is `no_material_change` — stored either way) |
| Observation horizon | `projection.observationHorizonDays` |
| Assumption ledger | `projection.assumptionLedger` |
| Computation basis | `projection.basis` |
| Evidence / corroboration label | `basis.corroborationLabel`, `basis.corroborationStatus`, `basis.corroborationSources`, `basis.corroborationRuleIds`, `basis.tripleCorroboration`, `basis.evidenceLabel` |
| Created timestamp + run reference | `projection.provenance.createdAt`, `.runId` |
| Opportunity identity for future comparison | `projection.provenance.oppId`, `.opportunityIdentity` |

Also stored, whether or not any screen renders them: the band-width derivation and its per-axis drivers (T4), projection strength, and the full recommendation with all five parts (T5). Storage is not gated on display.

**The corroboration label is stored, not re-derived.** 2.0-A2 reports what a projection *rested on*. Re-deriving it at comparison time would read today's corroboration from a cross-run graph that has since moved — which is a different fact.

---

## 2. Why provenance is stamped at store time

`build_projection` is pure: no clock, no run context, no DB. That purity is what makes **AC5** hold — *"re-running against unchanged signal reproduces identical bands and bases"*.

A timestamp inside the computed payload would make every recomputation differ from its stored twin and quietly destroy that guarantee. So the pipeline splits into three steps:

```
1. compute   project_opportunities(opps)          # pure, deterministic (T1–T5)
2. identify  stamp_projections(opps, run_id, …)   # adds provenance (T6)
3. store     run KV  +  opportunity_instances row # AC6
```

### The consequence for 2.0-A2, stated plainly

**Compare `projection_core(...)` values, not whole payloads.** Two projections of the same unchanged signal have identical cores and *different* provenance — that is the design, not staleness. `projection_store.projection_matches_stored(stored, recomputed)` implements the rule so A2 does not have to rediscover it; getting it wrong would make every stored projection look changed.

---

## 3. Two storage locations, on purpose

| Location | Role | Why both |
|----------|------|----------|
| **Run-scoped KV** (`opps`) | the *serving* copy | What every read surface already returns — opportunities API, roadmap, executive report, blueprint, PDF. The copy an analyst sees. |
| **`opportunity_instances.metadata`** | the *tracking* copy | Keyed `(opportunity_identity, run_id)`. Queryable **across runs by identity**, which run-scoped KV structurally cannot be: KV cannot answer *"every projection ever made about this problem"*. |

Both are written from the same payload in the same pipeline step, so they cannot disagree about what was projected.

The instance write is **non-blocking**, matching every other Stage-2 writer: a failure is logged and never breaks a run. The KV copy is written first, because that is the one the run depends on.

**An opportunity with no stable identity still stores its projection** in KV. It cannot be followed across runs, so `provenance.crossRunComparable` is `false` — recorded explicitly rather than left for a reader to infer from a null.

---

## 4. The read surface for 2.0-A2

```python
from app.projection_store import (
    get_stored_projection,       # one projection: (run_id, opp_id)
    get_projections_for_run,     # every projection in a run, keyed by opp id
    get_projection_history,      # THE cross-run series for one identity
    projection_matches_stored,   # core comparison — use this, not ==
)
```

`get_projection_history(identity, org_id)` returns `[{runId, createdAt, projection}]` ordered oldest-first, so a caller can walk the series forward without re-sorting. It returns `[]` rather than raising when the table is absent, so a dev DB without migrations degrades instead of erroring.

---

## 5. A defect this task fixed

The roadmap artifact was built and stored **early** in materialization — before temporal enrichment, and therefore before any projection could exist. `_apply_intervention_projection` ran ~130 lines later.

Consequences:

* `GET /api/runs/{run_id}/roadmap` served opportunities with **no projection at all**, so the Agent Roadmap screen showed none;
* 2.0-A1 **T4's capped-confidence ordering rule**, which reads each opportunity's projection, was ordering a stage in which every projection was absent — it silently did nothing.

`_rebuild_roadmap_with_projections` re-stores the roadmap after projections attach, in both materialization paths (`materialize_t2.py` and `routes_sprint4_t1.py`). Rebuilding rather than moving the original build keeps the roadmap available early for the pipeline steps that read it, while making the *stored* artifact the complete one. `build_roadmap` is deterministic over the same opportunities, so the re-store changes nothing except the presence of the projections.

---

## 6. Tests

| Test file | Covers |
|-----------|--------|
| `backend/discovery/tests/test_projection_provenance.py` | The stamp's required fields, the core/provenance separation that protects AC5, readback helpers, and stored-payload completeness including the no-band case. |
| `backend/tests/contract/test_a1_projection_storage.py` | Completeness and identification on the wire; every named API surface (opportunities, **roadmap**, executive report, enrichment, blueprint); the instance row and the cross-run history; reproducibility of the core; the non-blocking contract. |

---

## 7. Rules for anyone touching this

1. **Never read the clock inside `build_projection`** or anything it calls. The stamp is applied at store time for exactly this reason.
2. **Never compare stored projections with `==`.** Use `projection_matches_stored` or compare `projection_core` values.
3. **Bump the schema versions** (`PROJECTION_SCHEMA_VERSION`, `BAND_WIDTH_MODEL_VERSION`, `RECOMMENDATION_SCHEMA_VERSION`, `PROVENANCE_SCHEMA_VERSION`) when a change makes a stored projection non-comparable with a fresh one. They are stamped into provenance precisely so 2.0-A2 can tell a *model* change from an *evidence* change.
4. **If you add a step that rewrites `opps` after projection**, re-store the roadmap too — or reintroduce the defect in §5.
