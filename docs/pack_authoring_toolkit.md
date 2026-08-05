# Pack authoring toolkit (2.0-C3 T3 / AT-838)

The local loop a pack author works in: scaffold a project, validate the manifest,
run fixtures, lint the non-negotiables. Read this before changing a lint rule,
the case-file format, or what the scaffold emits.

Implementation: `backend/discovery/packs/sdk/scaffold.py`, `harness.py`,
`lint.py`, `toolkit.py`. CLI: `backend/scripts/pack_sdk.py`. Tests:
`backend/tests/unit/test_pack_authoring_toolkit.py`.

---

## 1. The loop

```
python scripts/pack_sdk.py scaffold ./my_pack --pack-id acme_service_desk \
    --name "Acme Service Desk" --author "Acme Ltd" --contact packs@acme.test

python scripts/pack_sdk.py validate ./my_pack   # well-formed and closed? (AT-836)
python scripts/pack_sdk.py lint ./my_pack       # holds the non-negotiables?
python scripts/pack_sdk.py test ./my_pack       # fixtures produce what you expect?
python scripts/pack_sdk.py check ./my_pack      # all three, as installation runs them

python scripts/pack_sdk.py primitives           # the library + parameter contracts
python scripts/pack_sdk.py schema               # the manifest schema reference
python scripts/pack_sdk.py rules                # lint rules and aggregation floors
```

Every command exits non-zero on failure, so the loop drops straight into CI.
`--json` gives a machine-readable report on any of them. Nothing here installs,
signs, or executes partner code — the toolkit only ever reads a pack.

A pack project is a directory:

```
pack.json          the manifest
fixtures/*.json    test cases: seeded signal + expectations
README.md          the loop, written where the author will look
```

---

## 2. Scaffold

`scaffold_pack()` writes a manifest, three fixtures, and a README.

**The property that makes it worth having: its output passes the whole toolkit
immediately**, and a test asserts exactly that
(`test_a_scaffolded_pack_passes_the_whole_toolkit`). A scaffold whose output
fails its own tooling teaches an author that the errors are noise, and from then
on they read past every real one.

Two more deliberate choices:

* **It scaffolds a negative case.** A detector that fires on everything passes a
  positive-only suite forever; the quiet case is what catches it. An author shown
  the pattern once tends to keep writing it.
* **It derives `minPlatformVersion` from the concepts it declares** rather than
  hardcoding one — a scaffold declaring a floor its own concepts do not support
  would fail validation the moment the author ran it.

Fixture timestamps are fixed constants, never clock reads, so a scaffolded suite
cannot start failing on a date nobody chose. Existing files are never overwritten
without `--force`: silently replacing an author's manifest would be the worst bug
this toolkit could have.

---

## 3. Harness

An author writes a case: seeded signal, and what they expect from it.

```jsonc
{
  "name": "recurring work items are detected",
  "asOf": "2026-06-30T00:00:00Z",        // optional; else the latest record in this case
  "signal": { "records": [ ... ], "dependencyEdges": { ... } },
  "expect": {
    "detectors": {
      "repeated_work_item": {
        "fires": true, "findingCount": 1, "subjects": ["repeated_manual_step"],
        "minMetric": 4, "confidence": "MEDIUM", "corroboration": "single_source",
        "statementContains": "recurs"
      },
      "queue_ageing": { "fires": false }
    },
    "findingCount": 1,
    "noOtherDetectorsFire": true
  }
}
```

Four behaviours worth knowing:

* **Every case checks the four-part contract** on every finding, whether or not
  the case mentions it. Evidence completeness is not something an author opts
  into — a pack whose fixtures pass while emitting a contract-incomplete finding
  would sail through authoring and fail at the pack boundary in a customer's run.
* **A typo'd detector id fails the case.** Otherwise the expectation silently
  asserts nothing and passes forever — the classic green-but-empty test.
* **A case reports every failed expectation**, not the first. An author fixing one
  assertion per run stops running the harness.
* **A signal the platform refuses at admission** (an individual field, an unknown
  concept) reads as a case failure with the reason, not a traceback. That is the
  most common early mistake, and it deserves a sentence rather than a stack.

The case-file vocabulary is closed, for the same reason the manifest schema is: an
ignored key is an assertion the author believes they wrote and never ran.

---

## 4. Lint — the non-negotiables

| Rule | What it catches |
|------|-----------------|
| `individual_naming` | A label, glossary entry, LLM context, or emitted finding naming a person |
| `causal_wording` | "caused by" / "due to" / "root cause" in pack text or an emitted statement |
| `missing_aggregation_floor` | A detector that can fire on a single record |
| `incomplete_evidence` | A detector with no summary claim; a finding missing a contract part, numeric evidence, or a source trace |

**Why lint exists on top of schema validation.** Validation answers *is this
document well-formed and closed?* Lint answers *is this pack honest?* — a question
with nothing to do with shape. A manifest can be perfectly valid and still set an
ageing floor of one item or write "caused by" into a label.

**Static and runtime legs.** Most rules check both the manifest and the findings
the harness produced, and neither subsumes the other: a label can name an
individual the fixtures never exercise, and a finding can leak one through a
subject the manifest never mentions.

**Aggregation floors are stricter than the schema's bounds by design.** The schema
says what is structurally sane; lint says what is honest. Where the two coincide
today (e.g. `recurrence.min_occurrences`), the lint rule stays as the regression
guard for the day somebody loosens a bound.

**The individual-naming rule matches phrases, not bare words.** "user" appears in
legitimate pack prose ("end users report…"), and a rule that fires on it is a rule
authors learn to suppress.

**The causal rule exempts accountability language.** "Humans remain responsible
for every action" is the platform's own guardrail sentence — every first-party
pack's LLM context carries it — and it asserts nothing causal about a finding.
Flagging it would make the rule fire on the one sentence we most want authors to
write. A genuine causal claim elsewhere in the same text still fires
(`test_lint_still_fires_on_real_causation_beside_a_guardrail_sentence`).

---

## 5. The combined check

`toolkit.check_pack_directory()` runs the three stages in dependency order:

1. **validate** — a failure stops here rather than producing a cascade of derived
   noise from a document that was never well-formed;
2. **test** — which also produces the findings the next stage needs;
3. **lint** — manifest *and* those findings, so the runtime legs run against real
   output.

That ordering is why the check lives in one function rather than three CLI calls
glued together. It is also the function installation-time sandbox validation is
meant to call, so "what the author ran locally" and "what the platform runs before
activation" are the same code path rather than two implementations that drift.

`PackCheckReport.reasons()` is the specific-failure list an installer surfaces —
"validation failed" tells an author nothing they can act on.

---

## 6. Scope of this task

AT-838 owns the scaffold, harness, lint, the combined check, and the CLI.
Packaging and installation, sandbox validation wiring, and partner-facing
documentation are separate 2.0-C3 tasks. They call `check_pack_directory` rather
than re-deriving these rules — if a later task re-implements a lint rule or a
fixture runner, that is the drift this module exists to prevent.
