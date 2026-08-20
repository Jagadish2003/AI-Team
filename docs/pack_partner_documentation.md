# Partner documentation & the worked example (2.0-C3 T5 / AT-840)

The partner-facing half of the Skills SDK: the documentation a pack author reads,
and the example pack it describes. Read this before editing anything under
`docs/partner/`, moving the worked example, or adding a concept, primitive, or
lint rule that the documentation names.

Published docs: [`docs/partner/`](partner/README.md) — README, concept
vocabulary, primitive reference, discipline rules, worked example.
Implementation: `backend/discovery/packs/sdk/reference_docs.py` (the generators)
and `backend/discovery/packs/sdk/examples/example_service_desk/` (the pack).
CLI: `pack_sdk docs [--check|--write]`. Tests:
`backend/tests/unit/test_partner_documentation.py`.

---

## 1. The problem this task actually solves

Partner documentation names things a partner cannot see the source of: which
normalised concepts exist, which primitives exist, what each parameter's bounds
are, what the aggregation floors are. Every one of those lists lives in code and
moves. A hand-maintained copy of a moving list is a copy that will be wrong,
usually silently, and usually discovered by a partner rather than by us.

So AC6 is read in its strongest available form. It says *"the worked example pack
in the documentation builds and passes end to end in CI, so the docs cannot
rot"* — the example is the part the criterion names, and the reference lists are
the part that rots fastest. Both are covered:

* the **reference blocks are generated** from the platform's own declarations and
  re-rendered in CI;
* the **worked example is a real project** that CI validates, tests, lints,
  packages, verifies, and installs.

## 2. What is generated and what is not

`reference_docs.py` renders five blocks — `concepts`, `primitives`,
`manifest_shape`, `lint_rules`, `aggregation_floors` — from
`platform_capabilities`, `primitives`, `manifest`, and `lint`. A document carries
them between markers:

```markdown
<!-- generated:concepts — regenerate with `python scripts/pack_sdk.py docs --write` ... -->
...table...
<!-- /generated:concepts -->
```

Everything else is written by hand and must stay that way. Generated prose reads
like generated prose, and the parts of partner documentation that actually teach
— why a rule exists, how to think about a primitive, what a real pack looks like
— are the parts a person wrote. The generator handles lists; it does not handle
explanation.

`sync_docs()` is check-by-default: CI runs `docs` (no flag) and fails on a stale
block; an engineer runs `docs --write` after changing a declaration. In write
mode the verdict is what remains stale *after* rewriting, so a document that
could not be repaired — missing, or naming a section that does not exist — still
fails rather than being reported as fixed.

**Adding a concept, primitive, or lint rule is now a two-line change**: the
declaration, and `pack_sdk docs --write` in the same PR. The test suite is what
makes that non-optional.

## 3. The worked example is a project, not a listing

It moved from two loose files (`example_partner_pack.json`,
`example_partner_signal.json`) into a real pack project:

```
examples/example_service_desk/
  pack.json
  fixtures/01_busy_service_desk.json     every detector fires
  fixtures/02_below_the_floor.json       the same shapes, under every floor: silence
  fixtures/03_quiet_estate.json          an ordinary quiet month: silence
  README.md
```

That shape is load-bearing rather than cosmetic: a project directory is what
`check_pack_directory` and `build_bundle` take, so the example can be run through
the *actual* authoring and installation paths instead of a test-only
approximation. The manifest is unchanged — the AT-836/837/838 suites simply point
at the new path.

The seeded signal now lives inside the positive fixture rather than beside it.
One copy: the file CI runs end to end is the file the primitive-library tests
read, so the two cannot drift into disagreeing about what the example contains.

**The second negative fixture is the interesting one.** `02_below_the_floor`
reproduces each detector's shape one step *under* its floor — three identical
resolutions where the detector needs four, two aged incidents where it needs five
— and asserts silence. A quiet estate proves a detector does not fire on nothing;
this proves it does not fire on almost-enough, which is the failure a
positive-only suite never catches and the habit we most want partners to copy.

## 4. What the test suite pins

Beyond "the example passes `check`":

* every relative link in the partner docs resolves to a file;
* every generated section is published somewhere, and every published section
  matches the code;
* every concept and primitive the platform declares appears in the docs;
* the hand-written record-shape table exactly equals `ConceptRecord`'s fields —
  the one table that could not be generated cheaply, guarded instead;
* every `pack_sdk.py <command>` the docs teach is a real subcommand;
* the example produces findings carrying all four parts, naming no individual and
  claiming no causation, at both MEDIUM and HIGH confidence (a partner's first
  instinct is to look for a confidence field, so the example has to show both
  outcomes of a derivation they do not control);
* the example packages, verifies, installs, and activates — and a tampered copy
  of that same bundle is refused, because the walkthrough shows that failure and
  it has to be the real one.

One documentation gap was found by these tests rather than by review: the
walkthrough described the recurrence detector and skipped the other three. The
test that every declared detector appears in the walkthrough is what caught it.

## 5. Scope

AT-840 owns the partner documentation, the generated reference blocks, and the
worked example project. It changes no platform behaviour: no schema, primitive,
lint, packaging, or installation logic moved. The only production-code additions
are the generators and the `docs` CLI command; the only production-code change is
the example's new location.
