# The assembly policy — declared, not coded (2.0-B3 T1)

Retrieval proposes; assembly decides. This document is the decision half: what
context a finding is composed from, and how a deployment changes it.

`backend/app/config/assembly_policy.json` is the whole policy. Editing it changes
composition with no code deploy — that is 2.0-B3 AC1, and
`tests/unit/test_r2_0_b3_t1_assembly_policy.py` proves it by reordering the
declaration rather than patching the assembler.

## What changed, and why it needed changing

R16-B2 built the assembler with the right discipline: a fixed, documented,
deterministic sequence of rules, tested. But the *precedence* was in code —

```python
return (-_confidence(candidate), -_freshness_score(...), candidate.candidate_id)
```

— and "observed beats inferred" was a boolean selecting between two hardcoded
orderings. Two consequences:

* **Precedence was not tunable.** Changing it meant editing `context_assembly.py`
  and shipping a release, so no deployment could decide for itself whether recency
  outranks strength.
* **Source type did not exist as a dimension at all.** A Slack thread and a
  ServiceNow incident competed on confidence alone, and the higher-confidence chat
  won. "Structured records outrank conversational content" was a principle with
  nothing enforcing it.

## The declaration

```jsonc
{
  "budget_partitions": ["origin"],
  "ranking": ["source_type", "confidence", "freshness", "candidate_id"],
  "origin_ranks":      { "observed": 0, "inferred": 1 },
  "source_type_ranks": { "structured": 0, "prose": 1, "code": 2, "conversation": 3 },
  "freshness":  { "halflife_days": 30.0, "exclude_stale": true },
  "confidence_floor": 0.0,
  "caps": { "entities": 15, "relationships": 20, "evidence_chunks": 10 }
}
```

**To change precedence, reorder `ranking`.** It is applied left to right as a
lexicographic sort key, so the first entry dominates. Move `freshness` ahead of
`confidence` and recency outranks strength. Remove `source_type` and a conversation
competes with a structured record on confidence alone.

### Hard tiers versus soft preferences

The distinction is the subtle part, and collapsing it would quietly weaken R16-B2
AC3.

| List | Meaning |
|---|---|
| `budget_partitions` | **Hard tier.** Everything in a better tier fills the budget before anything in a worse tier is considered, so a worse-tier item can never *displace* a better-tier item that fit. |
| `ranking` | **Soft preference** applied within a tier. |

`origin` is hard by default: observed data genuinely must not be displaced by a
guess, which is the assembly-layer restatement of the Evidence & Identity Spine's
rule. `source_type` is soft by default, so a highly-relevant conversation is not
shut out entirely by the mere existence of one structured record — but a deployment
that wants structured records undisplaceable simply moves `source_type` into
`budget_partitions`.

### Unknown values sort last, never first

A value absent from a rank table gets a rank worse than every declared value. An
item earns precedence by declaring what it is — the same fail-safe rule the module
already applies to provenance (only an explicit `observed` earns observed
precedence) and to freshness (undated context scores least-fresh). The alternative
would let unclassified content outrank every declared structured record.

The rank is derived from the table (`max + 1`) rather than being a large constant,
so it stays correct when the table grows.

## What a malformed declaration does

A missing or invalid declaration **raises** at load — it never silently substitutes
defaults, because a deployment that believes it configured precedence and did not
would compose findings differently from what its operators think. The loader refuses:

* an unknown dimension name (a typo would otherwise drop a precedence rule silently);
* a `ranking` that does not end in `candidate_id` (without the stable tiebreaker the
  key is not a total order, and two equal candidates could swap between runs —
  losing the determinism the module exists for);
* a dimension in both lists (ambiguous about whether it may displace);
* a declared dimension whose rank table is missing (its precedence would be undefined);
* a repeated dimension, or a `confidence_floor` outside 0.0–1.0.

The one place that degrades instead of raising is `graph_context.py`, because graph
context is a non-blocking enrichment step: a failure there must never cost a run its
findings. It falls back to the R16-B2 in-code precedence and logs at **error** level
naming the consequence — a silent fallback would mean composing against precedence
nobody chose.

## Reading a decision after the fact

Two additions make a stored decision interpretable:

* every `selection_log` entry now carries `source_type`. A log showing confidence and
  freshness but not source type could not explain why a high-confidence conversation
  ranked below a weaker structured record — the decision would look arbitrary.
* `ContextPackage.policy_declaration` records the declaration that produced the
  package. Since precedence is now editable, the log alone is no longer
  self-explaining; a log read six months later has to be interpretable against the
  rules in force when it was written.

Bump `version` in the declaration when a change would alter composition for
unchanged inputs.

## Budgeted composition (2.0-B3 T2 / AC2)

R16-B2 already selected deterministically under the per-kind caps and logged a reason
per candidate. What was missing was the ability to *answer* the question the log
technically contained: **did this finding lose context, and to which budget?**
Answering it meant parsing every entry, so in practice nobody asked.

### The report

`ContextPackage.budget_report` (surfaced onward as `GraphContext.budget_report`) is
that answer in one JSON-serialisable object, deliberately mirroring MSP-B7's
`BudgetReport` shape — that module established this repo's loud-degradation vocabulary
(budget / processed / deferred / breached / reason) and a reader who has seen one
should recognise the other.

Per kind: `budget`, `offered`, `considered`, `selected`, and the drops split by cause —
`dropped_by_budget`, `dropped_by_total_budget`, `dropped_below_floor`, `dropped_stale`.
The split matters because the remedies differ: widen a budget, lower a floor, or
refresh a stale artifact. One aggregate "dropped" number would send a reader to the
wrong lever.

**`breached` means a BUDGET cost context** — not merely that something was dropped. A
below-floor or stale exclusion would have happened with unlimited budget, so counting
those as a breach would send an operator to widen a budget that was never the
constraint.

**The report is derived from the selection log**, not counted alongside it, so the two
cannot disagree. `offered == selected + dropped` for every kind, and a test asserts
it: an early version subtracted one count twice and reported 2 drops where 5 had
happened. A report that does not add up is worse than none, because it will be quoted.

### The per-finding total budget

`caps.total_items` bounds a finding across *all* kinds — which is what actually bounds
an LLM prompt, since the per-kind caps sum to 45 and a prompt should rarely carry that
much.

It ships as **`null` (disabled)**, and that is deliberate rather than unfinished: no
measurement of real prompt size against narrative quality exists yet, and a hand-set
number here would silently trim every finding on the strength of a guess — the same
objection this codebase raises to any un-calibrated threshold. The per-kind caps remain
in force, so over-budget selection and its drop record are exercised in production
regardless. Setting an integer enables it; `0` is refused, because an empty context for
every finding is a mistake rather than a policy.

When it binds, the trim is deterministic in both dimensions:

* **which kind yields** — kinds give up items in reverse `kind_precedence` order, so
  the most substitutable kind (declared last) shrinks first. Graph structure is
  declared first because a finding stripped of its entities loses the subject its
  evidence is about;
* **which item yields** — the already-ranked *tail* of that kind, so the item lost is
  always the weakest, never a mid-list item chosen by accident of iteration.

Both are declared, so changing either is a config edit — the T1 discipline carried into
T2.

### Nothing is dropped silently

A trimmed candidate's existing log entry is **re-labelled** to `total_budget` rather
than having a second entry appended. Two entries for one candidate would make the log
self-contradictory — "included" *and* "excluded" — and every reader would then need to
know which wins. `total_budget` is a distinct reason from `budget_exhausted` because it
says something different: the finding as a whole was too big, not that one kind was
oversubscribed.

A breach logs at `info` naming the reason, so a thin narrative can be explained without
log-level archaeology.

### Not yet wired: B1's trace

The ticket notes the drop record "feeds B1's trace". **2.0-B1 is not on this branch** —
`app/trace_graph.py` and `app/retrieval_trace.py` live on the unmerged `R2.0_B1`. The
report is therefore built as a first-class serialisable artifact shaped for that trace
to render, and the wiring is left to whichever merges second rather than half-built
here against a module that is absent.

## T3 — contradiction handling (AC3)

> "Seeded contradictory sources produce a finding that names the disagreement rather
> than silently resolving it."

### The problem

The CMDB says the payments service is owned by `Platform Engineering`. The runbook says
`L2 Support`. Before T3 the assembler ranked one above the other, the loser never
reached the prompt, and the narrative asserted **one** owner with total confidence. The
disagreement — often the actual root of the friction being reported — disappeared
without trace, and it disappeared *most* reliably in exactly the estates where it
mattered most.

`app/context_contradictions.py` detects these disagreements. It does not settle them.

### Surfaced, never resolved

Detection **appends a record**. It never drops, reorders, re-ranks or re-weights a
candidate, and there is no return path meaning "prefer this side". Both positions travel
into the finding with their sources named. This is the 2.0-A2 T4 confounder discipline
applied here, and it is enforced the same way: a structural test walks the module's AST
and fails the build if it ever assigns to selection state.

The record carries **no severity, no score and no preferred side**. There is nothing here
to rank the sources by, and inventing a scale would be winner-picking restated as a
number. The rendered copy is additionally checked against a `RESOLUTION_LANGUAGE` list
("the correct owner is", "should be", "supersedes") at build time — the same guard shape
`discovery/projection/vocabulary.py` uses, and like that one it deliberately does **not**
flag the evidence it is reporting.

### Material, not merely different

Four rules keep the report worth reading, because a detector that cries wolf gets
switched off:

* **normalisation** — case, whitespace and `-`/`_` separators are formatting, not
  disagreement. Declared `equivalences` go further and state that two spellings are one
  value. They are declared, never inferred: a fuzzy similarity rule would manufacture
  *agreement*, suppressing a real finding — the mirror image of the failure above.
* **numeric tolerance** — `4.0` and `4.01` hours agree.
* **a missing value is not a position** — absence of information is not disagreement,
  the same rule `outcome_confounders` applies to an absent pack version.
* **two systems, not two records** — one system holding two conflicting rows is a
  data-quality problem inside that system. By default every position must also be
  *observed*: the platform disagreeing with a source is not two sources disagreeing.

**A position is only ever taken from a structured field.** This module never parses
narrative text looking for claims, because "the runbook says X" derived by reading a
paragraph is an inference presented as an observation. A document contradicts a record
only when its producer indexed a structured claim.

### Detected over the eligible set

Detection runs over the candidates that cleared the stale and confidence gates — **not**
over the ones the budget kept. Were it to see only the selected set, a budget that
trimmed one side would silently resolve the disagreement: T3's failure mode reintroduced
one layer down, by T2. Each position therefore carries `in_context`, and a contradiction
whose sides did not all fit says so in its own summary.

### Configuration

The `contradictions` block in `config/assembly_policy.json` declares the comparable
attributes (with their real per-connector field spellings as aliases), the numeric
tolerance, `require_observed`, `min_distinct_sources` and `max_reported`. An **absent**
block falls back to documented defaults, so a config file written before T3 still detects
disagreements rather than shipping the feature quietly switched off; a block that is
**present and invalid** raises, because an operator who configured this and got it wrong
must be told. Whatever `max_reported` omits is counted and reported — never silently
dropped.

## T5 — the conversation MEDIUM ceiling (AC5)

> "Conversation-derived content never lifts a finding above MEDIUM on its own."

The ceiling is not new. COR-05 has held it since R16-A2 (`corroboration_rules.py`), and
the R16-C1 T3 clamp in `apply_corroboration_confidence` guards it a second time.
`app/conversation_ceiling.py` neither replaces nor duplicates either — it imports the
confidence vocabulary from the same registry — because **2.0-B3 opened two routes around
the ceiling that did not exist when it was written**:

* **the assembly route.** COR-05 governs the *detector signal* path. It knows nothing
  about the retrieval substrate, which since R18-A4 indexes Slack and Teams threads as
  `conversation` chunks reaching a finding through `context_assembly`.
* **the configuration route.** T1 made precedence editable, so a deployment can reorder
  `source_type_ranks` to rank conversation first. That is a legitimate composition
  choice; it must not be able to disable a safety rule.

So the ceiling is computed where the evidence is actually composed, and **derived from
the evidence itself rather than from any policy a deployment can edit** — which is what
makes "a config edit cannot defeat the ceiling" true by construction rather than by
convention. `test_reordered_precedence_cannot_defeat_the_ceiling` is the case that pins
it.

**"On its own" is defined precisely.** The ceiling looks at *evidence* and applies when
there is at least one conversation-derived chunk and no evidence of any other source
type. Graph entities and relationships are deliberately not counted as the other source:
they are the finding's subject, not an independent source agreeing with it, and treating
the mere presence of the entity a thread mentions as corroboration would let any
conversation-only finding clear the ceiling by naming something — COR-08's
no-self-corroboration rule, at the assembly layer.

Two asymmetries are deliberate. **Untyped evidence counts as "other", not as
conversation**: the ceiling fires on positive knowledge that the support is chat, never
on a producer's silence, because capping a finding for a missing metadata field would be
the platform being arbitrary. And the ceiling **caps but never lowers or promotes**: a
LOW finding stays LOW; applying it at or below MEDIUM is a no-op. It only ever removes an
elevation the evidence does not support.

`enforcement_points()` names all three places the rule is enforced, so the regression
suite asserts the list is complete rather than trusting a comment.

## Backwards compatibility

Additive. `AssemblyPolicy()` with no declaration behaves exactly as it did before
T1 — confidence, then freshness, then id, with observed as a hard tier — so any
caller that has not opted in is unaffected. `AssemblyPolicy.declared()` is the
opt-in, and `graph_context` (the production assembly path) uses it.

T3 and T5 are additive in the same sense: `select_candidates` keeps its R16-B2 signature
(`run_selection` is the new call for anyone needing the eligible set), the new
`ContextPackage` fields default to empty, and a finding whose sources agree and whose
evidence is not conversation-only produces a byte-identical prompt to the one it produced
before this change.
