# Detector primitive library (2.0-C3 T2 / AT-837)

The runnable set of composable primitives an authored pack builds detectors from.
Read this before adding a primitive, changing a parameter contract, or writing
code that executes manifest-declared detectors.

Implementation: `backend/discovery/packs/sdk/primitive_library.py` (the runnable
primitives), `signals.py` (what they read), `contract.py` (what every finding
inherits), `execution.py` (running a manifest). Contracts:
`backend/discovery/packs/sdk/primitives.py` (AT-836). Worked example + seeded
signal: `backend/discovery/packs/sdk/examples/`. Tests:
`backend/tests/unit/test_pack_primitive_library.py`.

---

## 1. One vocabulary, two halves

| Half | Module | Owns |
|------|--------|------|
| Declaration | `primitives.py` (AT-836) | ids, parameter contracts, bounds, concept arity, evidence/corroboration semantics as documentation |
| Implementation | `primitive_library.py` (AT-837) | one runnable function per declared id |

`PRIMITIVE_IMPLEMENTATIONS` is keyed by the ids `PRIMITIVE_LIBRARY` declares, and
a structural test asserts the two key sets are **equal**. A declared-but-unimplemented
primitive is a promise to an author that fails at their customer; an
implemented-but-undeclared one is a capability nobody can author against or
review. Both fail the build instead.

`PRIMITIVE_LIBRARY_VERSION` versions the **contract surface**, not the
implementation. This task changed no contract, so it stays `1.0.0` — bumping it
would invalidate authored manifests for a change they cannot observe.

---

## 2. What an author inherits (the point of the library)

The ticket's requirement is that *the four-part criterion is inherited rather than
re-implemented*. Concretely, a manifest declares a detector and gets all four
parts without writing any of them:

| Part | Where it comes from |
|------|---------------------|
| **evidence** | Built by the primitive from the records that actually contributed — counts, spans, departures, gaps, all numeric and specific. |
| **confidence** | **Derived**, never asserted (`contract.derive_confidence`). |
| **corroboration** | The agreeing sources, or an explicit single-source cap; a windowed join also records the join type and window. |
| **source trace** | A pointer per contributing record (bounded at 25 with an explicit sample note — the count in evidence stays exact). |

The contract vocabulary and builders are **imported from `cloud_ops_finding`**, the
operational pack scaffold, exactly as `security_ops_finding` inherits them. A
partner finding is therefore the same object as a first-party finding — same
parts, same "no individuals" sweep, same causal gate. `contract.py` is the SDK's
one import site for that seam.

### Confidence derivation

An author cannot write a confidence level anywhere in a manifest. The level
follows from the contributing records:

| Contributing sources | Status | Level |
|----------------------|--------|-------|
| One | `single_source` | MEDIUM, capped, labelled |
| Two or more, at least two non-conversational | `corroborated` | HIGH |
| Two or more, but only one non-conversational | `corroborated` | MEDIUM — the standing conversation ceiling |

A manifest's own caps then apply on top, and they may only **lower** (the schema
refuses raising them, AT-836). This is why certification means something: a pack
cannot buy confidence by declaring it.

---

## 3. The primitives

| Primitive | Fires when | Key parameters | Metric |
|-----------|-----------|----------------|--------|
| `recurrence` | The same grouped fact occurs ≥ `min_occurrences` inside `window_days` | `group_by`, `min_distinct_actor_groups` | occurrences |
| `threshold_vs_baseline` | A subject's metric departs from **its own** baseline by ≥ `departure_pct` | `metric`, `direction`, `min_baseline_runs` | departure fraction |
| `ageing` | ≥ `min_items` items have sat ≥ `min_age_days` in scope | `age_from`, `state_scope` | aged item count |
| `oscillation` | Items transition ≥ `min_hops` times across ≥ `min_distinct_participants` groups | `transition_kind`, `window_days` | worst hop count |
| `concentration_traversal` | ≥ `min_dependents` dependents concentrate on one anchor within `max_depth` hops | `anchor`, `require_corroboration` | dependent count |
| `co_occurrence_window` | ≥ `min_pairs` cross-concept pairs fall inside `window_minutes` | `ordering` | pair count |

Three behaviours worth stating explicitly because getting them wrong is subtle:

* **`threshold_vs_baseline` is per-subject, never global.** A subject is judged
  only against the baseline carried on its own records, the per-queue discipline
  the first-party ageing detector established. A subject with no established
  baseline (`baseline_runs` below the floor) does **not** fire — unbaselined is
  not the same as compliant.
* **`co_occurrence_window` gives a join outside its window nothing at all** — not
  a weaker contribution, nothing. That is how coincidence is stopped from
  inflating confidence. Each second-concept record matches at most its *nearest*
  qualifying first-concept record, so a busy window cannot inflate the pair count
  combinatorially.
* **`concentration_traversal` states concentration, never causation.** Its
  statement is generated by a helper that runs the causal gate on its own output,
  so the wording contract cannot regress silently.

---

## 4. What a primitive reads

`ConceptRecord` — one normalised fact, tagged with the normalised concept it
instantiates. A primitive never sees a connector payload; that is what makes an
authored detector portable across source families.

**Individual-free at admission, not at the boundary.** `concept_record()` refuses a
record carrying an individual-person field or an email-shaped value. Checking only
when the finding is built would be too late in one specific way: a pack could
group *by* an individual and emit a "group" whose identity is a person. Refusing
at admission means no primitive can ever see one.

**Deterministic time.** `as_of` comes from the caller or from `SignalSet.default_as_of()`,
which is the latest instant **in the data**. Nothing reads the wall clock — pinned
by a test — because a primitive that did would produce a different finding every
day from the same fixture, making the authoring harness and reproducibility both
impossible.

`SignalSet.dependency_edges` is the graph `concentration_traversal` walks (MSP-B3
dependency edges in the platform, a plain mapping in a fixture). Supplying none
means concentration sees only direct references — an honest degradation, not an
error.

---

## 5. Running a manifest

```python
manifest = parse_manifest(document)                  # AT-836
signals  = signal_set_from_dicts(fixture)            # AT-837
result   = run_manifest(manifest, signals)           # findings, contract-enforced
results  = to_detector_results(result)               # pipeline DetectorResult objects
```

`run_manifest` enforces the four-part contract at the **pack boundary** and
raises `PackContractViolation` on a violation — the same posture
`cloud_ops_finding.enforce_pack_findings` takes for a first-party pack. A partner
pack is held to the identical bar; a certification level earnable by a pack
emitting weaker findings would not be worth printing on a board paper.

A disabled detector is reported as an outcome with a reason rather than omitted,
so an author can tell "did not fire" from "was not run".

`to_detector_results` is the **only** SDK path that touches `discovery.models`
(and through it `app`), imported lazily inside the function. A structural test
pins that importing the SDK does not import `app`, so offline authoring tooling
stays offline.

---

## 6. Scope of this task

AT-837 owns the primitive library, the signal model it reads, the inherited
contract, and manifest execution. The authoring toolkit (scaffold, fixture
harness, lint), packaging and installation, sandbox validation, and partner-facing
documentation are separate 2.0-C3 tasks that drive `run_manifest` rather than
re-deriving detector behaviour. Registering an installed authored pack into the
run pipeline is installation's job (AT-839); this task supplies the adapter it
needs.
