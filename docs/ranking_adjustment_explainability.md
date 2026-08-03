# Explaining a learned ranking adjustment (2.0-A3 T3)

Why a finding moved, as structured data, rendered into a sentence that never
overclaims.

Read this before touching `backend/app/learning_reason.py` or
`learning_reason_vocabulary.py`.

---

## 1. Why this is a correctness requirement, not a UI nicety

The story is explicit: if accumulated decisions silently reshape scoring,
findings stop being explainable and the evidence story collapses. That story is
not abstract in this codebase — the four-part criterion (evidence, confidence,
corroboration status, source trace) is enforced at pack boundaries, A1 built
projection provenance, and A2 built measurement traceability. An unexplained
reordering sitting on top of all that would undo it at the last mile.

---

## 2. The reason is data; the sentence is rendered from it

This follows A2 T4's confounder precedent exactly. Each caveat there carries
`type` / `severity` / `detail` / `detectedAt` so T6 can **count** them and B1 can
**render** them; a prose string would serve neither.

`AdjustmentReason` carries:

| Field | Why |
|---|---|
| `direction`, `ranksMoved`, `baseRank`, `adjustedRank` | what was done |
| `decisionCount`, `decisionsByAction` | how many decisions, and which kind |
| `outcomeCount`, `outcomesByVerdict` | how many measurements, and their verdicts |
| `hasOutcomeEvidence` | whether any of it is measured rather than opinion |
| `wasCapped`, `cappedBy` | whether a cap bound the move |
| `evidenceStrength` | how thin the basis is |
| `contributingDecisions`, `contributingOutcomes` | the links (§3) |
| `summary` | the sentence, rendered from the fields above |

A portfolio view can therefore count adjustments by direction, filter to capped
ones, or aggregate verdicts — none of which is possible against a string. The
`summary` travels on the payload so every surface shows identical wording rather
than each composing its own.

---

## 3. Links, not just counts (AC2)

AC2 requires the adjusted opportunity to link to the contributing decisions and
outcomes, which means identifiers must survive into the record.

* **Outcomes** were straightforward — A2's movement records carry
  `baselineRunId` and `currentRunId` as first-class columns precisely so a
  measured number resolves to the runs that produced it. Each contributing
  outcome also carries its `comparabilityVerdict`, so a caveated measurement
  never presents as a clean one.
* **Decisions** resolve by `feedbackId` → `/api/learning/feedback/entry/{id}`.
  This is the payoff for T1's decision on storage: a decision recorded only as a
  mutable enum field would have no id to link to at all.

One small change was needed in T1: the decision evidence ref carried
`reasonCode` but not the **action**, and "your team accepted 4 similar findings"
cannot be rendered from a reason code. `action` and `recordedAt` now travel on
the ref, so the sentence and the weight come from the same record.

Contract tests fetch every link and assert it resolves — and that a cross-org
reader gets a 404.

---

## 4. The wording is guarded, not trusted

`learning_reason_vocabulary.py` is the A1 T5 pattern applied to a different
overclaim. Three prohibited categories:

| Category | Example | Why |
|---|---|---|
| `knowledge_claim` | "we learned", "AgentIQ understands your priorities" | decisions were counted; nothing was understood |
| `importance_claim` | "more important", "should be prioritised", "we recommend" | the layer changed an ORDER; it did not discover worth |
| `credibility_implication` | "we are more confident", "your decisions corroborate this" | see §5 — the subtle one |

**Deliberately not prohibited**, following A1 T5's rule that a guard which flags
the evidence trains people to ignore it:

* the counts — "4 decisions", "1 measured outcome";
* what the customer did — "your team accepted". An observation about the
  customer, not a claim by the platform;
* what was measured — "delivered measured improvement". A2 measured it;
  reporting a measurement is not overclaiming;
* what was done — "ranked higher", "moved up 2 places", "the cap limited this".

The guard runs at **build time** (`render_reason` checks its own output, so our
templates are clean by construction) and at the **boundary** (contract tests
sweep the served opportunity, explain, preview and roadmap payloads). A template
that is clean today is not a control over what someone adds tomorrow.

---

## 5. The reason explains ordering and nothing else

The subtle boundary, and the one this subtask most needs to hold.

AC3 forbids the adjustment touching a finding's evidence, confidence or
corroboration. Explainability copy that *implied* otherwise would violate the
spirit of that criterion while passing its letter: a reader seeing "your team's
decisions support this finding" next to the confidence badge would reasonably
conclude the learned signal contributed to the finding's credibility. It did not.

Two enforcements:

1. **Placement** — the reason is namespaced under the finding's `_ranking`
   annotation. `reason_placement_violations()` checks the served payload for
   ranking copy that has leaked into `confidence`, `corroboration_*`,
   `evidenceIds`, `aiRationale` or `projection`, and a contract test runs it over
   every served finding.
2. **Wording** — the `credibility_implication` category above, plus a contract
   test asserting no served summary contains "confiden", "corroborat",
   "verifie" or "more reliable".

---

## 6. Honest about thin evidence

An adjustment resting on three decisions and one outcome says so:

> Ranked higher: moved up one place because your team accepted three similar
> findings and one did not move as far as projected after the change was made.
> **Based on three decisions and one measured outcome, which is limited evidence
> and may change as more arrives.**

`evidenceStrength` is `minimal` / `limited` / `moderate` / `substantial`, from a
weighted count in which a measured outcome counts double — for **hedging only**.
It never affects the adjustment; that weighting lives in T1's config, and
duplicating it here would let the two disagree about what an outcome is worth.

Thresholds are set so "three decisions and one measured outcome" (weighted 5)
reads as **limited**. T1's cold start needs 10 weighted signals before learning
activates for an org at all, but a single finding TYPE can rest on far fewer —
and that is exactly the case worth telling the customer about.

`evidenceStrength` is served as structured data so a UI can style the hedge
rather than string-matching the sentence.

---

## 7. API

| Route | Purpose |
|---|---|
| `GET /api/runs/{runId}/opportunities` | each moved finding carries `_ranking.reason` |
| `GET /api/learning/adjustment/explain/{runId}/{oppId}` | one finding's reason + resolvable links |
| `GET /api/learning/adjustment/preview/{runId}` | every adjustment in a run, each with its reason |

`explain` answers **404** for a finding that did not move: there is no ordering
change to explain, and an empty explanation would invite a UI to render "this
was not adjusted because…" on every unadjusted finding.

---

## 8. When changing any of this

* Add copy only through `render_reason`'s templates — it checks its own output.
* A new prohibited phrase belongs in `learning_reason_vocabulary`, with a test
  for both a case it must flag and a legitimate case it must not.
* A new learning module must be added to `LEARNING_LAYER_MODULES` in
  `test_learning_signal_isolation.py`; that list has its own completeness guard.
* Bump `REASON_SCHEMA_VERSION` when the structured shape changes.
