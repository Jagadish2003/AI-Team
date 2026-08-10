# Primitive reference

A detector is a **primitive**, bound to one or more [normalised
concepts](concept_vocabulary.md), with parameters. That is the entire
composition model. There is no module path, no expression, and no callback —
see [discipline rules](discipline_rules.md) for why.

---

## 1. Declaring a detector

```json
{
  "detectorId": "repeated_manual_resolution",
  "title": "Repeated manual resolution of the same incident shape",
  "primitive": "recurrence",
  "concepts": ["resolution_signature"],
  "parameters": {
    "min_occurrences": 4,
    "window_days": 30,
    "group_by": "signature"
  },
  "labels": {
    "summary": "The same resolution is applied repeatedly by hand.",
    "whyItMatters": "Repeated identical resolutions are the clearest candidate for an assisting agent.",
    "recommendation": "An agent handles the recurring cases; the residual requires judgment."
  }
}
```

| Field | Required | Notes |
|---|---|---|
| `detectorId` | yes | Stable and unique within the pack; it is what fixtures assert against |
| `title` | yes | Shown wherever the finding is |
| `primitive` | yes | Exactly one, from the library below |
| `concepts` | yes | Must satisfy the primitive's concept arity, and every concept must also appear in your `compatibility` block |
| `parameters` | — | Typed and bounded; an unknown parameter is an error, not an ignored extra |
| `labels` | — | `summary`, `whyItMatters`, `recommendation`, `evidenceHint` |
| `enabledByDefault` | — | Defaults to `true`; a disabled detector is *reported as skipped*, never silently omitted |

Give every detector a `summary`. Without one the finding surfaces numbers with
no claim a reader can interrogate, and lint will say so.

## 2. What every primitive gives you

The reason to compose rather than implement: each primitive emits the four-part
contract for you, and there is no way to opt out of any part of it.

* **Evidence** — the contributing records, the counts, the window, and the
  measured values.
* **Confidence** — *derived*, never declared. One source caps at MEDIUM; two or
  more independent sources are eligible for HIGH; conversational sources never
  lift a finding above MEDIUM on their own. No manifest field sets a confidence
  level. You may lower the caps in `scorerCalibration.confidence`; you cannot
  raise them.
* **Corroboration status** — which systems agreed, whether a window gated the
  agreement, and the cap reason when one applied.
* **Source trace** — a pointer to every originating record, so any claim opens to
  the rows behind it.

## 3. The library

<!-- generated:primitives — regenerate with `python scripts/pack_sdk.py docs --write`; do not edit by hand -->
Primitive library **1.0.0** — 6 primitives. A detector names exactly one.

#### `ageing` — Ageing

Work items sitting in a state longer than a threshold — queue ageing, stalled approvals, deferral drift.

**Concepts:** exactly 1.

| Parameter | Type | Required | Default | Bounds / values | Meaning |
|---|---|---|---|---|---|
| `min_age_days` | integer | yes | — | 1 to 3650 | Age threshold, in days, before an item counts as aged. |
| `min_items` | integer | no | `3` | 1 to 10000 | Aged items required before the detector fires (an aggregation floor — one aged item is a record, not a finding). |
| `age_from` | enum | no | `opened_at` | opened_at \\| last_state_change_at \\| due_at | Which timestamp the age is measured from. |
| `state_scope` | enum | no | `open` | open \\| unresolved \\| any | Which items are in scope for ageing. |

*Evidence:* Emits the four-part finding contract: the contributing records as evidence, a confidence level derived from source count and agreement, an explicit corroboration status, and a source trace to every originating record.

*Corroboration:* Ageing is observed directly from records, so corroboration status reports the source systems that agree on the aged population.

#### `co_occurrence_window` — Co-occurrence within window

Two normalised concepts occurring together inside a bounded correlation window — the only honest form of a cross-stream join.

**Concepts:** exactly 2.

| Parameter | Type | Required | Default | Bounds / values | Meaning |
|---|---|---|---|---|---|
| `window_minutes` | integer | yes | — | 1 to 10080 | Correlation window. A join outside it is a coincidence and is recorded as rejected, never as agreement. |
| `min_pairs` | integer | no | `2` | 1 to 10000 | Co-occurring pairs required before the detector fires. |
| `ordering` | enum | no | `either` | either \\| first_before_second | Whether ordering within the window matters. |

*Evidence:* Emits the four-part finding contract: the contributing records as evidence, a confidence level derived from source count and agreement, an explicit corroboration status, and a source trace to every originating record.

*Corroboration:* The join type and the window used are recorded on the claim, on success and on rejection, so a coincidence never inflates confidence.

#### `concentration_traversal` — Concentration / traversal (depth-bounded)

Work concentrating on a shared entity reached by bounded traversal of the entity graph — stated as concentration, never as causation.

**Concepts:** 1 or more.

| Parameter | Type | Required | Default | Bounds / values | Meaning |
|---|---|---|---|---|---|
| `max_depth` | integer | yes | — | 1 to 3 | Traversal depth bound. Hard-capped: an unbounded traversal is a full graph walk, not a detector. |
| `min_dependents` | integer | yes | — | 2 to 1000 | Distinct dependents that must concentrate on the anchor. |
| `anchor` | enum | no | `entity_reference` | entity_reference \\| artifact \\| actor_group | What the concentration is measured against. |
| `window_days` | integer | no | `30` | 1 to 365 | Observation window, in days. |
| `require_corroboration` | boolean | no | `False` | true / false | Require a second source to agree before emitting. |

*Evidence:* Emits the four-part finding contract: the contributing records as evidence, a confidence level derived from source count and agreement, an explicit corroboration status, and a source trace to every originating record.

*Corroboration:* Wording is concentration-shaped ('work concentrates on...'). The primitive never asserts causation — causality is the causal engine's.

#### `oscillation` — Oscillation

Repeated back-and-forth transitions — reassignment ping-pong between groups, state flapping, ownership churn.

**Concepts:** exactly 1.

| Parameter | Type | Required | Default | Bounds / values | Meaning |
|---|---|---|---|---|---|
| `min_hops` | integer | yes | — | 2 to 100 | Transitions on one item before it counts as oscillating. |
| `transition_kind` | enum | no | `assignment` | assignment \\| state \\| ownership | Which transition is counted. |
| `window_days` | integer | no | `30` | 1 to 365 | Observation window, in days. |
| `min_distinct_participants` | integer | no | `2` | 2 to 50 | Distinct actor groups the oscillation must span. |

*Evidence:* Emits the four-part finding contract: the contributing records as evidence, a confidence level derived from source count and agreement, an explicit corroboration status, and a source trace to every originating record.

*Corroboration:* Oscillation is described at group level only; participants are actor groups and queues, never individuals.

#### `recurrence` — Recurrence

The same normalised fact recurring above a count within a window — the shape behind 'this is handled manually again and again'.

**Concepts:** exactly 1.

| Parameter | Type | Required | Default | Bounds / values | Meaning |
|---|---|---|---|---|---|
| `min_occurrences` | integer | yes | — | 2 to 10000 | Occurrences within the window before the detector fires. |
| `window_days` | integer | yes | — | 1 to 365 | Rolling observation window, in days. |
| `group_by` | enum | no | `signature` | signature \\| artifact \\| actor_group \\| entity_reference | What counts as 'the same' occurrence. |
| `min_distinct_actor_groups` | integer | no | — | 1 to 50 | Require the recurrence to span at least this many actor groups. |

*Evidence:* Emits the four-part finding contract: the contributing records as evidence, a confidence level derived from source count and agreement, an explicit corroboration status, and a source trace to every originating record.

*Corroboration:* Single-source recurrence is capped at MEDIUM; agreement from a second source system elevates to HIGH.

#### `threshold_vs_baseline` — Threshold vs baseline

A measured quantity departing from its own observed baseline by more than a proportion — never an absolute number picked by hand.

**Concepts:** exactly 1.

| Parameter | Type | Required | Default | Bounds / values | Meaning |
|---|---|---|---|---|---|
| `metric` | enum | yes | — | volume \\| age_days \\| time_to_resolve_minutes \\| reassignment_hops \\| backlog_depth | The normalised measure compared against its baseline. |
| `departure_pct` | number | yes | — | 0.01 to 10.0 | Fractional departure from baseline before firing (0.25 = 25%). |
| `direction` | enum | no | `above` | above \\| below \\| either | Which direction of departure is a finding. |
| `min_baseline_runs` | integer | no | `3` | 1 to 100 | Prior runs required before a baseline is trusted. |
| `window_days` | integer | no | `30` | 1 to 365 | Comparison window, in days. |

*Evidence:* Emits the four-part finding contract: the contributing records as evidence, a confidence level derived from source count and agreement, an explicit corroboration status, and a source trace to every originating record.

*Corroboration:* A departure observed in one source stays MEDIUM until a second source agrees within the correlation window.
<!-- /generated:primitives -->

## 4. Three behaviours worth knowing before you tune parameters

**`threshold_vs_baseline` judges each subject against its own baseline, and does
not fire when there isn't one.** A subject with no `*_baseline` metric produces no
finding rather than being compared against a global average. Unbaselined is not
compliant, and reporting it as compliant would give a customer a clean result for
the part of their estate the platform knows least about.

**A `co_occurrence_window` join outside its window contributes nothing** — not a
weaker signal, nothing. The window is what separates correlation from
coincidence, and a coincidence must never inflate confidence. Each second-concept
record also matches only its *nearest* first-concept record, so a busy window
cannot manufacture pair counts by matching everything to everything.

**`concentration_traversal` describes concentration, never causation.** The
generated statement is concentration-shaped ("work concentrates on a shared
dependency"), and it is checked against the causal gate before it is emitted.
Causality is the causal engine's to assert; a pack that claims it is a pack
whose findings cannot be defended when a customer disagrees. `max_depth` caps at
3 for the same class of reason: an unbounded traversal shipped as "configuration"
is still an unbounded graph walk.

## 5. Scoring

You do not write a scorer. You calibrate the platform's one:

```json
"scorerCalibration": {
  "impactWeights": {
    "effort_concentration": 0.4,
    "breadth": 0.25,
    "recurrence_stability": 0.2,
    "automation_shape": 0.15
  },
  "confidence": {
    "singleSourceCap": "MEDIUM",
    "corroboratedMax": "HIGH",
    "conversationSourceCap": "MEDIUM"
  }
}
```

`impactWeights` change the **order** findings are presented in. They never touch
evidence, confidence, or corroboration — ranking and truthfulness are separate
concerns in this platform, and a weight that could change a confidence level
would collapse that separation.
