# 2.0-B3 — Context Assembly Maturity: T2, T3, T5

Three tasks from the 2.0-B3 story, all in the context assembler: the per-finding budget
and its drop record (T2/AC2), contradiction handling (T3/AC3), and the conversation
MEDIUM ceiling carried into assembly (T5/AC5).

Each is small in code and specific in intent. What they share is a single discipline:
**when assembly loses something or has to choose, it says so** — a truncated context, a
disagreement between sources, and an unsupported confidence elevation are all facts the
finding must carry rather than quietly absorb.

---

## T2 — Budgeted composition (AC2)

> "Over-budget candidate sets select deterministically and record what was dropped and
> why."

R16-B2 already selected deterministically under the per-kind caps and logged a reason per
candidate. What was missing was the ability to *answer* the question the log technically
contained: **"did this finding lose context, and to which budget?"** Answering it meant
parsing every entry, so in practice nobody asked.

### The report

`ContextPackage.budget_report` (surfaced onward as `GraphContext.budget_report`) is that
answer in one JSON-serialisable object. Its shape deliberately mirrors MSP-B7's
`BudgetReport` — budget / offered / selected / dropped / breached / reason — so the repo
has **one** loud-degradation vocabulary and a reader who has seen one recognises the
other.

Drops are split by **cause**: `dropped_by_budget`, `dropped_by_total_budget`,
`dropped_below_floor`, `dropped_stale`. The split matters because the remedies differ —
widen a budget, lower a floor, refresh a stale artifact. One aggregate number would send
a reader to the wrong lever.

`breached` means **a budget cost this finding context**, not merely that something was
dropped. A below-floor or stale exclusion would have happened with unlimited budget;
counting it as a breach would send an operator to widen a budget that was never the
constraint.

**The report is derived from the selection log, not counted alongside it**, so the two
cannot disagree. `offered == selected + dropped` per kind is asserted — an early version
subtracted one count twice and reported 2 drops where 5 had happened. A report that does
not add up is worse than none, because it will be quoted.

### The per-finding total budget

`caps.total_items` bounds a finding across *all* kinds — what actually bounds a prompt,
since the per-kind caps sum to 45.

It ships **`null` (disabled)**, deliberately rather than unfinished: no calibration of
prompt size against narrative quality exists, and a hand-set number here would silently
trim every finding on the strength of a guess. The per-kind caps remain in force, so
over-budget selection and its drop record are exercised in production regardless. `0` is
refused — an empty context for every finding is a mistake, not a policy.

When it binds, both dimensions are declared and deterministic: kinds yield in reverse
`kind_precedence` (graph structure first, because a finding stripped of its entities
loses the subject its evidence is about; evidence chunks are most substitutable and yield
first), and within a kind the already-ranked *tail* yields.

A trimmed candidate's log entry is **re-labelled** to `total_budget` rather than gaining a
second entry — two entries would make the log self-contradictory ("included" *and*
"excluded") and every reader would need to know which wins.

### Not wired: B1's trace

The ticket notes the drop record "feeds B1's trace". **2.0-B1 is not on this branch** —
`trace_graph.py` / `retrieval_trace.py` live on the unmerged `R2.0_B1`. The report is
built as a first-class artifact shaped for that trace to render, and the wiring left to
whichever merges second rather than half-built here against an absent module.

---

## T3 — Contradiction handling (AC3)

> "Seeded contradictory sources produce a finding that names the disagreement rather than
> silently resolving it."

**The problem.** The CMDB says the payments service is owned by `Platform Engineering`.
The runbook says `L2 Support`. Before this change the assembler ranked one above the
other, the loser never reached the prompt, and the narrative asserted **one** owner with
total confidence. The disagreement — often the actual root of the friction being
reported — disappeared without trace.

New module: `backend/app/context_contradictions.py`.

### Surfaced, never resolved

Detection **appends a record**. It never drops, reorders, re-ranks or re-weights a
candidate, and there is no return path meaning "prefer this side". This is 2.0-A2 T4's
confounder discipline, enforced the same way — a structural test walks the module's AST
and fails the build if it ever assigns to selection state.

The record carries **no severity, no score, no preferred side**. There is nothing here to
rank the sources by, and inventing a scale would be winner-picking restated as a number.
The rendered copy is additionally checked at build time against a `RESOLUTION_LANGUAGE`
list ("the correct owner is", "should be", "supersedes") — the guard shape
`projection/vocabulary.py` established, and like that one it deliberately does **not**
flag the evidence it is reporting.

### Material, not merely different

A detector that cries wolf gets switched off, so four rules bound what is reported:

* **normalisation** — case, whitespace and `-`/`_` separators are formatting.
  Declared `equivalences` go further. They are *declared, never inferred*: a fuzzy
  similarity rule would manufacture **agreement** and suppress a real finding — the
  mirror image of the failure this story is about.
* **numeric tolerance** — `4.0` and `4.01` hours agree.
* **a missing value is not a position** — absence of information is not disagreement.
* **two systems, not two records** — one system holding two conflicting rows is a
  data-quality problem inside that system. By default every position must be *observed*:
  the platform disagreeing with a source is not two sources disagreeing.

**A position is only ever taken from a structured field.** Narrative text is never parsed
for claims, because "the runbook says X" derived by reading a paragraph is an inference
presented as an observation.

### Detected over the eligible set, not the selected one

This is the subtle part, and it is where T3 meets T2. Were detection to see only what the
budget kept, **a budget that trimmed one side would silently resolve the disagreement** —
T3's failure mode reintroduced one layer down, by T2. So detection runs over the
candidates that cleared the stale and confidence gates; each position carries
`in_context`, and a contradiction whose sides did not all fit says so in its own summary.

`select_candidates` keeps its R16-B2 signature; `run_selection` is the new call that also
returns the eligible set.

### Where it surfaces

`ContextPackage.contradictions` → `GraphContext.contradictions` +
`contradiction_note` → a `=== SOURCE DISAGREEMENTS ===` prompt section that instructs the
model not to settle it (a model handed two conflicting facts otherwise picks the more
fluent one and states it plainly — the disagreement would survive detection and die in
generation). The section is additive exactly as T3-S16-A's causal section is: with no
disagreement the prompt is byte-for-byte unchanged.

### Configuration

A `contradictions` block in `config/assembly_policy.json` declares the comparable
attributes and their per-connector field spellings, the tolerance, `require_observed`,
`min_distinct_sources` and `max_reported`. An **absent** block falls back to documented
defaults, so a pre-T3 config still detects disagreements rather than shipping the feature
switched off; a **present but invalid** block raises, because an operator who configured
this and got it wrong must be told. Whatever `max_reported` omits is counted and
reported.

---

## T5 — Conversation ceiling enforcement (AC5)

> "Conversation-derived content never lifts a finding above MEDIUM on its own (regression
> against the standing ceiling)."

The ceiling is **not new** — COR-05 has held it since R16-A2 and the R16-C1 T3 clamp
guards it a second time. `backend/app/conversation_ceiling.py` neither replaces nor
duplicates either, and imports the confidence vocabulary from the same registry. It
exists because **2.0-B3 opened two routes around the ceiling that did not exist when it
was written**:

1. **The assembly route.** COR-05 governs the *detector signal* path. It knows nothing
   about the retrieval substrate, which since R18-A4 indexes Slack and Teams threads as
   `conversation` chunks reaching a finding through `context_assembly`.
2. **The configuration route.** T1 made precedence **editable**. A deployment can reorder
   `source_type_ranks` to rank conversation first. That is a legitimate composition
   choice, and it must not be able to switch off a safety rule.

So the ceiling is computed where the evidence is composed, and **derived from the
evidence itself rather than from any policy a deployment can edit** — which makes "a
config edit cannot defeat the ceiling" true by construction rather than by convention.
`test_reordered_precedence_cannot_defeat_the_ceiling` is the case that pins it, and it is
the most load-bearing test in the file.

**"On its own" is defined precisely.** The ceiling looks at *evidence* and applies when
there is ≥1 conversation chunk and no evidence of any other source type. Graph entities
are deliberately **not** counted as the other source: they are the finding's subject, not
an independent source agreeing with it, and counting them would let any conversation-only
finding clear the ceiling by naming something — COR-08's no-self-corroboration rule, one
layer down.

Two asymmetries are deliberate:

* **untyped evidence counts as "other", not conversation.** The ceiling fires on positive
  knowledge that the support is chat, never on a producer's silence. Capping a finding for
  a missing metadata field would be the platform being arbitrary.
* **it caps, never lowers or promotes.** LOW stays LOW; at or below MEDIUM it is a no-op.
  It only ever removes an elevation the evidence does not support.

The suite also re-proves the **standing** ceiling has not regressed: Slack-only and
Teams-only stay MEDIUM, the registry still declares COR-05 non-elevating, the clamp still
catches a drifted verdict — and, importantly, conversation *with* a primary corroborator
still reaches HIGH. Without that last case the ceiling could quietly become a suppression
bug and every test here would still pass.

---

## Degradation posture

Both new steps run *after* selection and both **degrade rather than raise**: assembly's
job is to compose a finding, and a fault in a reporting layer must never cost the finding
its context. Each logs at `error` **naming the consequence**, because a silently absent
disagreement report reads exactly like "the sources agree", and a silently absent ceiling
reads like "this evidence supports HIGH".

Note the honest asymmetry: a T5 failure removes a *second* line of defence — COR-05 and
the R16-C1 clamp still stand independently.

---

## Files

**New**
| File | Purpose |
|---|---|
| `backend/app/context_contradictions.py` | T3 — detection, the record, rendering, the copy guard, config parsing |
| `backend/app/conversation_ceiling.py` | T5 — the assembly-layer ceiling and its assessment |
| `backend/tests/unit/test_r2_0_b3_t3_contradiction_handling.py` | 44 tests |
| `backend/tests/unit/test_r2_0_b3_t5_conversation_ceiling.py` | 29 tests |
| `backend/tests/unit/test_r2_0_b3_t2_budgeted_composition.py` | 22 tests (T2) |

**Changed**
| File | Change |
|---|---|
| `backend/app/context_assembly.py` | T2 budget report; `run_selection` exposing the eligible set; contradiction + ceiling wiring on `ContextPackage` |
| `backend/app/assembly_policy_config.py` | `caps.total_items`, `kind_precedence`, `contradictions` block |
| `backend/app/config/assembly_policy.json` | the declared blocks above, with rationale inline |
| `backend/app/graph_context.py` | carries `budget_report`, `contradictions`, `contradiction_note`, `confidence_ceiling` |
| `backend/app/llm_enrichment.py` | `=== SOURCE DISAGREEMENTS ===` prompt section (additive) |
| `docs/assembly_policy.md` | T2 / T3 / T5 sections |
| `CLAUDE.md` | module map entries |

## Verification

```
python -m pytest tests/unit/test_r2_0_b3_t2_budgeted_composition.py \
                 tests/unit/test_r2_0_b3_t3_contradiction_handling.py \
                 tests/unit/test_r2_0_b3_t5_conversation_ceiling.py \
                 tests/unit/test_context_assembly.py \
                 tests/unit/test_context_assembly_selection_log.py \
                 tests/unit/test_r2_0_b3_t1_assembly_policy.py
```

**157 passed** across the six B3 assembly suites (73 of them new: 44 for T3, 29 for T5).

Full unit suite: **1002 passed, 12 failed.** All 12 failures are `psycopg2.OperationalError`
connection timeouts to `192.168.0.181:5432` in `tests/unit/test_run_health_last_ingestion.py`,
a file with no reference to assembly, contradictions or the ceiling — environmental, and
unrelated to this change.

**Contract tests were not run.** `tests/contract/conftest.py` cannot reach a maintenance
database to create `agentiqdev-test` against the same unreachable host, so the whole
contract layer errored at collection. **The contract layer is therefore unverified locally
and needs CI (or a reachable Postgres) before merge.**

## Backwards compatibility

Additive throughout. `select_candidates` keeps its signature, the new `ContextPackage`
and `GraphContext` fields default to empty, `AssemblyPolicy()` with no declaration behaves
exactly as it did before T1, and a finding whose sources agree and whose evidence is not
conversation-only produces a byte-identical prompt to the one it produced before this
change.

## Out of scope

* **B1 trace wiring** — 2.0-B1 is on an unmerged branch (see T2 above).
* **B3 T1 and T4** — T1 is already merged; T4 (narrative claim-to-evidence enforcement)
  depends on B1's trace and is not in this PR.
* **Frontend surfaces** for the disagreement block and the ceiling label — the data is
  contract-shaped and carried through `GraphContext`; rendering is a separate change.
