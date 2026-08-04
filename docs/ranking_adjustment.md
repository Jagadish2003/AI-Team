# The bounded ranking adjustment (2.0-A3 T2)

How accumulated learning is allowed to move a finding, and why base scoring is
still the answer to "what would this have ranked without learning?".

Read this before touching `backend/app/learning_adjustment.py` or the
`ranking_adjustment` block in `config/learning_signals.json`.

---

## 1. A layer, not an edit

Base scoring is untouched and always recoverable:

* `discovery/scorer.py` — `_compute_impact()` and `_rescale_impact()`, which map
  a detector result onto the 1–10 impact score — learn nothing and did not change.
* `discovery/packs/cloud_ops_scorer.py`'s `ops_impact_rank` is likewise a BASE
  rank. Learning applies above it, never inside it.

Both directions are enforced structurally, not by convention:

| Guard | Direction |
|---|---|
| `tests/unit/test_base_scoring_does_not_learn.py` | base scoring may not reach the learning layer |
| `tests/unit/test_learning_signal_isolation.py` | the learning layer may not write evidence/confidence/corroboration/scores |

The temptation these exist to block is specific: the moment learning looks too
weak, the obvious fix is to nudge `impact` by the accepted count. Once base
scoring learns, there is no untouched number left to recover, and every promise
in this story collapses quietly.

---

## 2. Applied at serve time

The layer runs over stored findings on the way out; it never writes into them.
That is what makes the recoverability guarantee structural rather than
conventional:

* the stored order **is** the base order — nothing to reconstruct;
* turning learning off restores base order exactly, with nothing to undo;
* every served finding carries its own `_ranking.baseRank` and
  `_ranking.baseImpact`, so the question is answerable inline.

**Applied before display shaping**, deliberately. `with_display_scores` adds a
deterministic per-id offset (up to +0.6) to spread bubbles on the matrix chart.
Running the adjustment after it would compute the score cap from that cosmetic
offset, making the cap vary by opportunity id for no reason. The cap must be a
fraction of the real base score.

---

## 3. One application point

Ordering is decided in several places, and a learned component in more than one
would compound into movement nobody could explain. So there is exactly one
adjustment function — `learning_adjustment.adjust_ranking` — and every
presentation surface routes through it.

| Surface | Role |
|---|---|
| `discovery/scorer.py` | base producer — **not** an application point |
| `discovery/packs/cloud_ops_scorer.py` | base producer — **not** an application point |
| `app/main.py::list_opportunities` | call site, via `_apply_learned_ranking` |
| `app/main.py::get_roadmap` | call site, via `roadmap_engine.apply_learned_adjustment` |
| `app/roadmap_engine.py::build_roadmap` | base producer — **not** an application point |

`test_base_scoring_does_not_learn.py::TestTheLayerIsAppliedInExactlyOnePlace`
asserts that `adjust_ranking` is defined in exactly one module.

**`build_roadmap` must stay learning-free.** It runs during materialization and
its result is PERSISTED (`run_kv_set("roadmap", …)` in `materialize_t2` and
`routes_sprint4_t1`). Adjusting inside it would bake the learned order into
storage — and then the stored roadmap would no longer be base order, so disabling
learning could not restore it and the recoverability guarantee would hold for the
opportunity list but silently not for the roadmap. Materialization also runs
without a request-scoped tenancy context, so the org would be wrong or absent. So
the roadmap is built and stored in base order, and `apply_learned_adjustment`
reorders a copy on the way out — the same shape as `list_opportunities`. Two
tests pin this.

**Order within the roadmap matters**: A1 T4's capped-confidence demotion runs at
build time, learning at serve time. The demotion is a *correctness* rule (a
finding whose confidence is capped for want of corroboration must never present
above a corroborated equivalent); learning is a *preference*. Learning arriving
last means it reorders within the space the correctness rules leave, rather than
overturning them. Stage membership is tier-driven and never touched.

---

## 4. The caps — both of them

The story offers a maximum rank move **or** a bounded score fraction. Both ship,
because they fail differently and the weaker binding is the conservative choice.

| Cap | Default | What it is |
|---|---|---|
| `max_score_fraction` | 0.15 | the mathematical bound — provable at the point of computation, stable at any list length |
| `max_rank_move` | 3 | the customer-facing promise — intuitive, but nearly unbounded on a short list, which is why it is not the only cap |

The score cap is **proportional**, not absolute, so the layer is least free to
reorder the findings the base scorer is most confident about.

### Why the rank cap needs its own enforcement

Capping each item's delta at N does **not** bound how far items actually move. An
item can be passively displaced by others jumping over it, by up to roughly 2N —
verified by fuzzing in
`test_learning_adjustment.py::TestTheCapIsRealNotDecorative`. A cap applied to the
sort key but not to the outcome reads as enforced while permitting double the
promised movement.

So placement uses a bounded-window algorithm (`_bounded_placement`) rather than a
sort. At each output slot only items still within reach are eligible, and an item
on its last legal slot is forced into it before any preference is consulted. The
bound `|adjusted_rank - base_rank| <= max_rank_move` therefore holds by
construction, and the tests assert it over real output.

The sort key is `(target_slot, -rank_delta, base_index)`. The middle term is
load-bearing: an item asking for a slot ties with the item already sitting there,
and without a tie-break favouring the claim over the incumbent, **every
single-rank move would compute correctly and then change nothing**.

### The caps are asserted, not merely configured

`validate_config` refuses a config that removes the bound (a zero or >1 score
fraction, a negative rank cap). More importantly the caps are asserted over real
output — the first version of the rank-cap test put thirty findings in one group
with one weight, which gives every item the same delta; a uniform shift moves
nothing, and that test passed just as happily with the bounded placement replaced
by a plain sort. The fuzz now uses mixed groups with differing weights, and a
companion test proves that same data breaks the cap under a plain sort.

---

## 5. Clipping is recorded, never silent

When the cap prevents the full move, the record says so:

```
requestedDelta  what learning asked for
appliedDelta    what survived the score cap
wasCapped       true
cappedBy        "score_fraction" | "rank_move"
```

That case is the interesting one. A base impact of 9 with accumulated feedback
pushing hard the other way means the learned signal and the base scorer are in
genuine tension — worth surfacing for the tuning conversation rather than
quietly resolving, and consistent with A2's posture that a constrained result
should say it was constrained.

---

## 6. The state is stored, not derived

Table `ranking_adjustments`, keyed `(org_id, detector_id, pack_id)` — T1's
similarity group, because the learned adjustment is a statement about a finding
TYPE, not about one finding.

Deriving it at read time would be cheaper and would need no table. It would also
mean a customer's ranking changed silently as history accrued, with no record of
what was applied when — so "why did this move last Tuesday?" would have no
answer, and T4's audit and reset would be impossible to satisfy honestly. A reset
in particular has nothing to reset if the state is an expression rather than a
value.

`ranking_adjustment_history` is append-only and present from the start, because
history cannot be reconstructed retroactively: a table added later begins with a
hole exactly where the first questions will be asked.

**Recomputation is explicit** (`POST /api/learning/adjustment/recompute`). Nothing
recomputes on the serving path — a ranking that shifted because someone opened a
page is precisely the invisible drift this story exists to prevent, and a contract
test pins it.

**Cold start is stored, not inferred.** An inactive signal set still writes rows,
with `learning_active = FALSE`. A zero that means "not enough evidence yet" and a
zero that means "learning weighed this and arrived at neutral" are different
facts, and a reader who cannot tell them apart will misread the first as the
second.

---

## 7. API

All analyst+, org from the tenancy middleware.

| Route | Purpose |
|---|---|
| `GET /api/learning/adjustment` | current state + the caps in force |
| `GET /api/learning/adjustment/history` | every value the adjustments have held |
| `POST /api/learning/adjustment/recompute` | recompute from the current signal set |
| `GET /api/learning/adjustment/preview/{runId}` | what the layer would do to one run, and why |
| `GET /api/learning/adjustment/base-order/{runId}` | "without learning" — nothing to undo |

No reset route: that is T4, and the history table this writes is what it reads.

---

## 8. AC coverage

| AC | Status |
|---|---|
| AC1 | **Met** — reorders within the cap; base scores unchanged and retrievable |
| AC2 | Groundwork — every adjustment carries `contributingRefs`; the rendered explanation is T3 |
| AC3 | **Met** — contract test over evidence, confidence and corroboration, plus a structural guard |
| AC4 | **Met** — `SignalSet.is_active` gates the layer; below threshold, base order with a stated reason |
| AC5 | Not yet — reset and the audit surface are T4; the history table exists for it |
| AC6 | **Met** — org-scoped in the SQL; two-org contract tests on state, history and served order |

---

## 9. When changing any of this

* Bump `configVersion` in `learning_signals.json` on any cap change — it is
  stored with each adjustment, so a value computed under different caps is
  identifiable.
* A new learning module must be added to `LEARNING_LAYER_MODULES` in
  `test_learning_signal_isolation.py`; that list has its own completeness guard.
* If you add an ordering surface, route it through `adjust_ranking`. Do not add a
  second adjustment.
