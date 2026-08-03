# The learning signal set (2.0-A3 T1)

What the ranking-adaptation layer is allowed to learn from, what each signal is
worth, and how two findings come to be called "similar". Read this before
touching `backend/app/learning_signals.py`, `learning_feedback.py`, or
`config/learning_signals.json`.

This subtask produces the signal set and applies **no adjustment**. The bounded
ranking adjustment is T2. Keeping the two apart is deliberate: a signal set that
also adjusted would be impossible to inspect without running the thing it feeds.

---

## 1. The two sources, and only two

| Source | Store | What it is evidence of |
|---|---|---|
| Analyst decisions | `opportunity_feedback` (`learning_feedback.py`) | a **judgement** — accept / dismiss / defer-with-reason |
| Outcome results | `opportunity_movements` (2.0-A2 T3/T5) | the **world** — the signal moved, or it did not |

Nothing else. In particular **nothing from `telemetry.py`**: page views, dwell
time, expand-clicks and every other engagement signal are excluded by
construction. A ranking layer trained on what was clicked is a recommendation
engine wearing an evidence platform's clothes.

That exclusion is enforced structurally, not by convention —
`tests/unit/test_learning_signal_isolation.py` fails the build if any module in
the layer imports telemetry, names it in executable code, or reads from any
source outside the permitted set. The guard is **proven to fail**: injecting a
telemetry import turns three of its tests red.

Adding a third source changes what this feature *is*. It belongs in a story, not
in an import.

---

## 2. The governing principle: an outcome outweighs an opinion

A measured result is evidence about the world; an analyst decision is evidence
about a judgement. Both are worth learning from. They are not equals.

This lives in a relationship between numbers, which is exactly the kind of
invariant that gets tuned away in good faith. So it is enforced in two places,
neither of them a comment:

**At config load.** `validate_config` REFUSES a config in which any decision
weight meets or exceeds any non-zero outcome weight. A deployment that inverts
the principle fails loudly and falls back to the shipped defaults, rather than
quietly reweighting itself while nothing in the product looks different.

**At weighting time — the outcome floor.** Config validation alone is not
enough, and the gap is not hypothetical: it was found by a test during
implementation. A `below_band` verdict (2.0) that is `weakly_comparable` (×0.5)
and carries both severities of caveat (×0.54) lands at **0.54** — below a clean
analyst accept at **1.0**. Caveats are common in real measurement, so opinions
would have quietly won most of the time, and no amount of validating the config
would have revealed it.

So a weighted outcome is floored at `outcome_floor_ratio` × the strongest
decision weight (default 1.05). The justification is not convenience: a caveat
says *this comparison is imperfect*, not *this is not a measurement*. 2.0-A2 T3's
own rule is that a poor comparability verdict still REPORTS with its caveat
attached — it never becomes a non-measurement. Caveats may therefore order
outcomes against **each other**, and must not push one across the evidence-class
boundary.

**The floor is applied before recency decay, not after.** Decay is identical for
both classes, so at equal age an outcome always outweighs a decision, while a
three-year-old measurement and a three-year-old opinion fade together. Flooring
after decay would claim an ancient measurement about a since-rebuilt system beats
a fresh judgement about today's, which is not defensible.

The resulting invariant — *at equal age, every weighted outcome outweighs every
decision* — is tested exhaustively over the cross product of every verdict,
comparability verdict and caveat mix against every action and defer reason.

---

## 3. The weights

All in `backend/app/config/learning_signals.json`. Every section declares a
`basis` of `measured` / `operationally_justified` / `provisional`, the same
convention as `outcome_confounders.json` and `ops_calibration.py`.

**Almost everything here is `provisional`.** No production outcome data exists
yet; 2.0-A2 running in the field is what will produce it. The ORDERING of the
weights is reasoned; the MAGNITUDES are first guesses. `GET /api/learning/config`
exposes the bases so a customer can see that too.

### Outcome signals, keyed on the T5 projection-validation verdict

| Verdict | Weight | Direction | Why |
|---|---|---|---|
| `within_band` | 3.0 | positive | worth doing **and** the model of it was right |
| `above_band` | 2.5 | positive | worked, more than expected — but the projection missed, and A1's bands are calibrated from these |
| `below_band` | 2.0 | negative | acted on, moved less than projected |
| `not_projected` | 0.0 | neutral | measured, but nothing to compare against |
| `too_early` | 0.0 | neutral | learning from an unfinished experiment |

The two zero-weighted verdicts are **counted, not excluded** — they stay visible
in the signal set as counted-but-unweighted inputs. A signal that vanishes
silently is one nobody can ask about later.

### Decision signals

| Action | Weight | Direction | Why |
|---|---|---|---|
| `accept` | 1.0 | positive | the baseline unit of decision evidence |
| `dismiss` | 1.0 | negative | symmetric on purpose — asymmetry would bias towards surfacing more |
| `defer` | 0.35 | negative | "not now" is weaker than "no", but not neutral |

### Defer reasons — a closed vocabulary

Free text is refused. A reason the layer cannot group on teaches it nothing, and
free text in a learning input is an unbounded PII surface. `reason_detail`
carries elaboration for the review surface and is never parsed or learned from.

| Reason | Multiplier | Why |
|---|---|---|
| `no_capacity` | 0.0 | a fact about the team, not the finding |
| `blocked_by_dependency` | 0.0 | blocked externally |
| `awaiting_approval` | 0.0 | in flight, not declined |
| `needs_more_evidence` | 0.6 | a judgement about this finding's evidence |
| `timing_not_right` | 0.4 | part finding, part calendar |
| `lower_priority` | 0.8 | an explicit relative-value judgement — nearly a dismissal |
| `other` | 0.3 | the honest escape hatch, deliberately weak |

The **zero-weighted reasons are the important ones**. They are deferrals that
carry no information about the finding at all; treating them as weak dismissals
would teach the layer to demote findings for reasons entirely about the
customer's calendar. They are still recorded, still linkable, and still shown.

A defer with **no** reason is refused outright — defaulting one would mean the
layer invented the thing it then learned from. A defer with an *unrecognised*
reason carries zero, for the same reason: guessing a weight for an unknown is how
a learning layer starts learning from noise.

### Multipliers

* **Recency** — exponential decay, 180-day half-life, floored at 0.1. Never
  reaches zero: an old outcome is weak evidence, not disproven evidence, and a
  signal decaying to exactly zero would silently leave the explainability surface
  (the customer sees a reason cite four decisions, then three, with nothing having
  changed). An undated signal is treated as fully decayed — the conservative
  direction, since the alternative rewards missing data.
* **Comparability** — `comparable` 1.0 / `weakly_comparable` 0.5 /
  `not_comparable` 0.2. None may be zero; the config loader refuses a zero, because
  that would silently discard a caveated measurement — the blocking A2 T3 refused.
  An **unrecognised** verdict takes the most conservative multiplier, so a verdict
  this code does not know cannot read as a clean comparison.
* **Confounders** — ×0.6 if any material caveat, ×0.9 if any advisory caveat.
  Applied **once per severity present**, not once per caveat, so a measurement
  with six advisory caveats is not weighted into oblivion.

Every multiplier applied is recorded on the signal (`multipliers`). An
unexplainable weight is not usable in an explainability feature.

---

## 4. The join key: `opportunity_identity`

Both sources key on it, which is the only reason this works: it is computed from
run-invariant inputs (`discovery/opportunity_identity.py`), so a decision made on
one run and an outcome measured three runs later resolve to the same problem.

**A finding with no stable identity is skipped, never mis-keyed.** A signal keyed
on a run-scoped id would count towards the cold-start threshold while informing
nothing — worse than its absence.

Note the signal set counts a team's **current position** on each finding, not its
clicks: `latest_feedback_by_identity` takes the most recent decision per
opportunity. An analyst who deferred and then accepted holds one position. The
full history is preserved for audit and explanation, not for accumulating weight.

The learning layer also inherits A2 T3's read-side guard for free: `list_movements`
filters out any measurement whose action date no longer matches a current
lifecycle action, so a measurement whose action was reversed stops being a
learning signal too.

---

## 5. Similarity — conservative and declared

What makes "your team accepted 4 similar findings" a defensible sentence.

| Rule | Score |
|---|---|
| same detector, same pack | 1.0 |
| same detector, other pack | 0.6 |
| same signal concept | 0.4 |
| **minimum to be called similar** | **0.4** |

The signal concept comes from A1's `signal_registry` — the field the detector
actually measures — rather than a second mapping invented here. A detector with
no profile has no concept and is similar only to itself, which is the honest
answer.

Two deliberate absences:

* **No "same pack" tier.** Two findings sharing only a pack have nothing
  meaningful in common, and calling them similar in a customer-facing explanation
  would be indefensible.
* **Nothing is inferred from titles or narrative text.** A name-similarity match
  is exactly the silent fuzzy inference 2.0-B2 refuses for entity resolution, and
  it is refused here for the same reason: a claim of similarity the customer
  disagrees with makes the whole explainability surface untrustworthy.

---

## 6. Cold-start honesty (AC4)

Learning activates only when **both** thresholds are met:

* `minimum_signals` (10) — weighted signals; zero-weight ones do not count
* `minimum_distinct_identities` (5) — distinct opportunities, not rows

The second is what stops a single enthusiastically-reviewed opportunity from
switching learning on for a whole org. A threshold one finding can satisfy is not
a threshold.

`SignalSet.is_active` is the gate T2 must consult. `inactive_reason` is
plain-language and is what the UI shows, so the "learning not yet active" message
and the gate come from one source of truth rather than two counts that could
disagree.

---

## 7. Why a new record rather than a widened `decision` enum

The decision was made deliberately (the subtask asks for that), and against
extending `("APPROVED", "REJECTED", "UNREVIEWED")`:

1. **The existing field is not durable.** It lives in the run-scoped `opps` KV
   blob that materialization rewrites wholesale and `replay.py` resets. A learning
   signal a replay can erase is not a signal — the same argument A2 T2 made for
   the baseline artifact.
2. **Wrong key.** Decisions are addressed by `(run_id, opp_id)`; learning joins
   on `opportunity_identity`.
3. **No per-decision identity.** AC2 requires linking to contributing decisions.
   A single mutable enum field has no id, actor or timestamp to link to.
4. **`defer` does not belong in that enum.** The same literal tuple is validated
   for EVIDENCE decisions in `main.py`, where deferring is meaningless.

So `opportunity_feedback` is **additive**. The analyst review flow is untouched,
and `set_opp_decision` mirrors `APPROVED`→`accept` / `REJECTED`→`dismiss` into
the learning record so the existing UI feeds learning with no frontend change.
`UNREVIEWED` is deliberately not mirrored: clearing a decision is the absence of a
judgement, not a third kind of one. The mirror is non-blocking — a learning
failure never breaks a review.

The record is **append-only**. Changing your mind appends; the earlier judgement
survives, because what the team thought at the time is part of the record and a
store that edits its own history cannot answer "why was this ranked higher last
month?".

---

## 8. API

All analyst+, org from the tenancy middleware (never from the request body).

| Route | Purpose |
|---|---|
| `POST /api/learning/feedback/{identity}` | record accept / dismiss / defer |
| `GET /api/learning/feedback/{identity}` | that opportunity's decision history |
| `GET /api/learning/feedback` | the org's decisions, newest first |
| `GET /api/learning/feedback/entry/{id}` | one decision — what an AC2 link resolves to |
| `GET /api/learning/signals` | the signal set, with its cold-start state |
| `GET /api/learning/vocabulary` | actions and reason codes, advertised not hardcoded |
| `GET /api/learning/config` | the weights in force, and each section's basis |

There is deliberately **no route that applies an adjustment** and none that
returns an adjusted ranking. That is T2.

`GET /config` exists because A3's whole discipline is that learning must never
become invisible drift: a customer asking "why is this ranked here?" is entitled
to see the weights, and — via each `basis` — to see that most of them are still
provisional first guesses.

---

## 9. Storage

* Table `opportunity_feedback`, migration `0036`, mirrored in `provision.sql`.
* DDL in `database/models/opportunity_feedback.py`, which also documents the
  production `REVOKE UPDATE, DELETE` that makes append-only a capability rather
  than a convention.
* Audit event `opportunity_feedback_recorded`, registered in
  `middleware/audit.py` before its first emission site.

---

## 10. When changing any of this

* Bump `configVersion` in `learning_signals.json` on any weight change — it is
  reported on every signal set, so a customer can tell which weighting produced a
  given explanation.
* Bump `SIGNAL_SET_SCHEMA_VERSION` when the signal shape changes.
* Adding a learning module means adding it to `LEARNING_LAYER_MODULES` in
  `tests/unit/test_learning_signal_isolation.py` — and that list has its own
  guard, so a new `learning_*.py` that is not listed fails the build.
