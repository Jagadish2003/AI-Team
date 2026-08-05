# Discipline rules — what your pack is held to

These are requirements, not style guidance. Each one is enforced somewhere in the
pipeline, and the table says where: **schema** (validation refuses the document),
**admission** (the signal layer refuses the record), **lint** (the authoring pass
refuses the pack), **harness** (your fixtures fail), or **boundary** (the platform
refuses the finding at run time, failing the run).

A rule enforced only by review is a rule that eventually ships broken. Every rule
below fails a build.

---

## R1 — A pack MUST NOT contain executable code

*Enforced by: schema.*

No module paths, no scripts, no expressions, no entry points, no shell. The
manifest schema is **closed**: an unrecognised key anywhere is an error rather
than an ignored extra, detectors name a primitive and there is no field in which
a code reference could be written, and a document-wide sweep refuses code-shaped
keys (`module`, `script`, `eval`, `entrypoint`, …) and code-shaped values
(imports, lambdas, dunders, shebangs, executable file suffixes, dotted module
paths) at any depth.

This is the constraint the whole SDK is built around. AgentIQ is deployed inside
regulated boundaries on the strength of the claim that nothing third-party
executes there.

## R2 — A pack MUST NOT reference an individual

*Enforced by: admission, lint, boundary.*

Packs describe **groups, queues, services, and entities**. Not people.

* At admission, a record carrying an individual-person field or an email-shaped
  value is refused before any primitive sees it.
* Lint refuses a manifest whose labels, glossary, LLM context, or focus text names
  a person, and refuses an emitted finding that leaks one.
* The pack boundary refuses a finding that references an individual, failing the
  run.

Lint matches **phrases**, not bare words — "end users report…" is legitimate
prose and is not flagged. What is flagged is language that means *this pack is
about a named person*: `assignee`, `assigned to`, `who resolved`, `per analyst`,
an email address, and their relatives.

## R3 — A pack MUST NOT assert causation

*Enforced by: lint, boundary.*

State what is **observed** — recurrence, concentration, ageing, co-occurrence.
"Caused by", "due to", "root cause" belong to the causal engine, which reasons
about causality with machinery a detector does not have.

Write *"incidents across three services concentrate on a shared dependency"*, not
*"a shared dependency is causing incidents across three services"*. The first
survives a customer disagreeing with it; the second does not.

One deliberate exemption: accountability language ("humans remain responsible for
every action") is not a causal claim about a finding, and is the sentence we most
want in your `llmContext`. A genuine causal claim elsewhere in the same text
still fails.

## R4 — Every detector MUST have an aggregation floor above one record

*Enforced by: lint.*

One record is a record. A finding is a pattern. A detector that can fire on a
single row will fire constantly, and a customer will learn to ignore the pack.

<!-- generated:aggregation_floors — regenerate with `python scripts/pack_sdk.py docs --write`; do not edit by hand -->
| Primitive | Floor parameter | Minimum |
|---|---|---|
| `ageing` | `min_items` | 2 |
| `co_occurrence_window` | `min_pairs` | 2 |
| `concentration_traversal` | `min_dependents` | 2 |
| `oscillation` | `min_distinct_participants` | 2 |
| `recurrence` | `min_occurrences` | 2 |
| `threshold_vs_baseline` | `min_baseline_runs` | 2 |
<!-- /generated:aggregation_floors -->

These minimums are **stricter than the schema's bounds**, on purpose: the schema
says what is structurally sane, this says what is honest. Set them higher than
the minimum where your domain warrants it — the example pack does.

## R5 — Every finding MUST carry all four parts

*Enforced by: harness, lint, boundary.*

Evidence, confidence, corroboration status, source trace. Every case in your
fixtures checks this on every finding **whether or not the case asks for it**,
because a pack whose fixtures pass while emitting a contract-incomplete finding
would sail through authoring and fail at the pack boundary in a customer's run —
the worst possible place to find out.

A finding must also carry **numeric evidence** and a source trace that resolves to
real records. Composing from primitives gives you all of this; the rule exists so
that stays true if the platform's contract widens.

## R6 — A pack MUST NOT assert confidence

*Enforced by: schema.*

There is no confidence field in a detector declaration. Confidence is derived from
how many independent sources agree, and the derivation is the platform's. You may
**lower** the caps in `scorerCalibration.confidence`; the schema refuses an
attempt to raise one above the platform ceiling.

## R7 — Conversational sources MUST NOT lift a finding above MEDIUM

*Enforced by: platform (derivation).*

Slack and Teams content corroborating only itself stays MEDIUM however much of it
agrees. This is not configurable upward.

## R8 — Detectors MUST bind normalised concepts, never connector shapes

*Enforced by: schema.*

Every concept you bind must exist in the [platform
vocabulary](concept_vocabulary.md) **and** be declared in your own
`compatibility` block. A detector that quietly required a concept the pack never
declared would make the compatibility gate a lie: it would pass activation and
then find nothing.

## R9 — Every parameter MUST be bounded

*Enforced by: schema.*

Parameters are typed and bounded — you will meet this most visibly at
`concentration_traversal.max_depth`, which caps at 3. Bounds are part of the
security posture, not ergonomics: an unbounded traversal is an unbounded graph
walk regardless of whether it arrived as code or as configuration.

## R10 — A pack MUST NOT claim a certification level

*Enforced by: schema, installation.*

A manifest declares `certification.requestedLevel`. The fields that would grant
one (`level`, `signature`, `certifyingEntity`, `reviewDate`, …) are refused **by
name** in a partner manifest, and an installed authored pack is registered as
Community regardless of what it requested. A level a pack could self-apply would
make the signature decorative.

## R11 — Fixtures MUST be deterministic

*Enforced by: harness.*

Nothing in the evaluation path reads the wall clock. Pin `asOf` in a case, or let
it come from the latest record in that case's own signal, and use fixed
timestamps. A suite that starts failing on a date nobody chose teaches an author
to distrust the harness.

## R12 — Negative cases are expected

*Enforced by: review, and by good sense.*

Nothing rejects a positive-only fixture suite today, but a detector that fires on
everything passes one forever. Ship at least one case where your detectors stay
quiet — the scaffold writes one for you, and the [worked
example](worked_example.md) ships two.

---

## The lint pass, as it states these

<!-- generated:lint_rules — regenerate with `python scripts/pack_sdk.py docs --write`; do not edit by hand -->
| Rule | The requirement |
|---|---|
| `causal_wording` | A pack states what is observed (recurrence, concentration, ageing). Causation is the causal engine's to assert, never a pack's. |
| `incomplete_evidence` | Every finding carries all four parts, with numeric evidence and a source trace that resolves to real records. |
| `individual_naming` | Findings reference groups, queues, services, and entities — never an individual person. |
| `missing_aggregation_floor` | A detector must require more than a single record before it fires — one record is a record, not a finding. |
<!-- /generated:lint_rules -->

```bash
python scripts/pack_sdk.py rules          # these rules and the floors, from the platform
python scripts/pack_sdk.py check ./my_pack   # everything above, as installation runs it
```
