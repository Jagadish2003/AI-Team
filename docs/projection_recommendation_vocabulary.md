# Recommendation copy and the projection vocabulary guard

**Story:** 2.0-A1 (Intervention Modelling), task 5 — *"Recommendation text in intervention language, never in guaranteed-savings language."*
**Implementation:** `backend/discovery/projection/recommendation.py` (what we say) and `backend/discovery/projection/vocabulary.py` (what we must never say).
**Acceptance criterion:** AC3 — *"No projection output — API, UI, report, or export — contains a point-estimate savings claim or guarantee language; template-level check over the projection vocabulary."*

---

## 1. The rule

AgentIQ does not say:

> This agent will reduce cost by 40%.

It says:

> Agent handles the 240 recurring reassignment cases; the residual requires judgement (cases whose correct owner is genuinely ambiguous).

A projection is a **direction and a band on measured signals**. A savings figure is a different kind of claim — one that gets quoted in a board paper and measured against reality in ninety days. The platform makes the first claim and never the second.

---

## 2. What a recommendation must say

Every recommendation states five things, and the payload names each one so a missing part is visible rather than silently absent:

| Part id | Says |
|---------|------|
| `agent_handles` | What the agent takes over — the manual step |
| `cases_in_scope` | Which recurring cases, as a measured count over the observed window |
| `remains_manual` | What still requires human judgement |
| `signal_expected_to_move` | Which **real measured field** should move, and in which direction |
| `band_and_horizon` | The magnitude band and the observation horizon — never a point |

Plus a `headline` in the story's canonical construction and `nextSteps` that are **actions, not outcomes** (confirm the pattern, agree the scope boundary, record the baseline for re-measurement).

Two per-detector fields in `signal_registry.py` supply the domain wording:

* `case_noun` — what the affected instances *are* ("pending approvals waiting on a chase", not "cases");
* `residual` — what stays with a person. **Every detector must declare one.** An agent that leaves nothing to judgement is a claim this platform does not make, and a test fails the build if a profile omits it.

The headline names the residual parenthetically (`…; the residual requires judgement (approval decisions themselves)`) rather than inlining it as the subject. That is deliberate: inlining needs singular/plural agreement guessed from an arbitrary noun phrase, which reads wrong whenever the guess loses.

---

## 3. What is prohibited

Two families, both in `vocabulary.py`.

### Guarantee language

| Rule | Examples |
|------|----------|
| future-tense promise | "will reduce", "will save", "is going to cut" |
| guarantee wording | "guarantees", "guaranteed" |
| absolute-outcome claim | "eliminates", "would eliminate", "eradicating" |
| totality claim | "will completely remove" |
| assertive assurance | "ensures" |
| savings framing | "savings", "cost savings" |
| return-on-investment framing | "ROI", "payback period" |
| risk-free framing | "risk-free", "proven to" |

The `eliminat*` family is banned outright rather than only under a future auxiliary. "Eliminate" claims **totality** — all of the thing, gone — which no magnitude band supports, and coordinated forms ("would reduce X and eliminate Y") put arbitrary distance between the modal and the verb. The guard only ever runs over payload strings and this repo's own template constants, never over source comments, so the ban costs nothing outside customer-facing copy.

### Point-estimate savings claims

Two directions, deliberately asymmetric:

* **claim then figure**, within a short window — "reduce cost by 40%", "saves 12 hours", "savings of $120,000";
* **figure then benefit noun**, with at most one intervening word — "40% reduction", "12 hours saved", "30% faster".

The asymmetry is the whole trick. A looser figure→term rule flags a *measured* value sitting in the same sentence as the word "improvement" ("currently 120 days, and lower is the improvement"), which is a description of direction, not a claimed benefit.

---

## 4. What is explicitly NOT prohibited

A guard that flags the evidence is worse than no guard — people learn to ignore it. These all pass:

* **Bands and ranges** — "23–57%", "between 13% and 67%", "25 to 55 percent". Ranges are masked out before the point-estimate rules run, so a band can never be mistaken for a point estimate. This is the single most important false positive to avoid: it is the honest output.
* **Measured observations** — "240 owner changes across 800 Cases in 90 days", "Impact 8/10", "reassignment rate 2.4 against a threshold of 1.5".
* **Machine identifiers** — a detector id like `COST_REDUCTION_GAP` or a field name like `hours_saved_90d` is not customer-facing copy. `scan_payload` skips a fixed set of non-prose keys.
* **Hedged, figureless verbs** — "could reduce handling steps" is weak copy, not a false claim. Blocking a common English verb outright would train people to route around the guard.

---

## 5. Where the guard runs

AC3 says "API, UI, report, or export", so enforcement is at each boundary rather than in one hopeful place.

| Point | What happens |
|-------|--------------|
| **Recommendation build** (`recommendation.py`) | `assert_clean` on every generated string. Our own templates must be clean *by construction* — a drift raises `ProhibitedVocabularyError` at build/test time rather than being quietly sanitized in production. |
| **LLM enrichment** (`llm_enrichment.py`) | `_scrub_enrichment_result` sanitises `aiSummary` sentence-wise and drops offending `aiWhyBullets` / `aiRisks` / `aiSuggestedNextSteps` bullets whole. Scrubs are logged (counts at WARNING, phrases at DEBUG) so prompt drift is visible. |
| **LLM prompts** | The prompts no longer *ask* for savings language. They previously instructed the model to "include a projected outcome using 'could reduce' / 'estimated' language"; they now forbid savings, percentages, and cost figures explicitly. |
| **Executive report** (`executive_report_engine.py`) | Narrative fields are scrubbed on a **copy** of each opportunity — the report must never rewrite what a run persisted, or a replay would serve different text than the run produced. `scrub_executive_summary` guards the summary paragraph at the report boundary. |
| **Blueprint** (`routes_sprint41_blueprint.py`) | `agentTopic` is sanitised whatever its source (LLM summary or `aiRationale` template), and the recommendation headline leads it. |
| **Static templates** | `track_a_adapter` rationale templates and the blueprint detector metadata are swept by a test; both were fixed in this task. |

**A prompt is a request, not a control.** The prompt changes reduce how often the guard has to fire; they are not the guarantee. This closes a note that had stood in `llm_enrichment.py` since Sprint 5: *"guardrail is prompt-instruction only — not post-validated. Post-generation validation of prohibited phrases is deferred."*

### Sentence-level, not field-level

A generated paragraph is usually three good sentences and one over-claiming one. Sanitising sentence-wise keeps the analysis and drops the claim. Bullets are dropped whole, because a bullet is already a single claim and half of one is a fragment.

When *every* sentence offends, the `REDACTION_NOTICE` is returned rather than an empty string — an explicit "a claim was removed here" always beats a blank field that reads as "the model had nothing to say". The notice is itself worded to survive the guard.

---

## 6. Tests

| Test file | Covers |
|-----------|--------|
| `backend/discovery/tests/test_projection_vocabulary.py` | The guard catches the prohibited shapes and does **not** catch bands, measurements, or identifiers; sanitisation behaviour; the five required parts; every profiled detector produces clean copy and declares a residual; static templates are clean; the prompts do not ask for savings language. |
| `backend/tests/contract/test_a1_recommendation_vocabulary.py` | The whole-payload sweep on the wire, across `/opportunities`, `/enrichment`, `/blueprint`, and `/executive-report`, plus a seeded savings claim proving the scrub is a control rather than a hope. |
| `frontend/src/__tests__/A1_RecommendationCopy.test.tsx` | Opportunity Review, Blueprint, and Top Quick Wins render the recommendation; the rendered DOM text carries no prohibited copy. |

The frontend never composes recommendation copy — it renders what the backend sent. A sentence written in a component would be one more place a savings claim could appear, and the guard only covers what the backend emits.
