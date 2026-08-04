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

## Backwards compatibility

Additive. `AssemblyPolicy()` with no declaration behaves exactly as it did before
T1 — confidence, then freshness, then id, with observed as a hard tier — so any
caller that has not opted in is unaffected. `AssemblyPolicy.declared()` is the
opt-in, and `graph_context` (the production assembly path) uses it.
