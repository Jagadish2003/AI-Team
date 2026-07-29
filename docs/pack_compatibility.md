# Pack Compatibility Declaration & Activation Gate

**Story:** 2.0-C1 — Pack Compatibility, Safe Disable & Rollback (AT-822)
**Task:** T1 — Compatibility declaration (AT-826)
**Criterion discharged:** AC1 — *a pack declaring an unmet platform range cannot be
activated; the refusal names the unmet requirement.*

Packs are versioned and stamped per run (R16-B1 §4), 1.9 added two more packs, and
1.9.1 enabled multi-pack runs. What was missing: what happens when a pack version
is incompatible with the platform version. This is that gate.

> **Scope.** This document covers COMPATIBILITY only. Safe disable (AT-827),
> rollback (AT-828), never-delete-history (AT-829), and surfacing (AT-830) are
> separate tasks layered on top of this gate. Nothing described here reads or
> writes pack enable/disable state.

## 1. What a pack declares

Every pack in [`pack_config.py`](../backend/discovery/packs/pack_config.py)
`PACK_REGISTRY` declares a `compatibility` block:

```python
"compatibility": {
    "minPlatformVersion": "1.9.0",   # INCLUSIVE floor;   None ⇒ no floor
    "maxPlatformVersion": None,      # INCLUSIVE ceiling; None ⇒ open-ended
    "requiredConcepts":   [...],     # gating — an unmet concept refuses activation
    "optionalConcepts":   [...],     # advisory — the pack degrades honestly without these
},
```

Read it with `get_pack_compatibility_declaration(pack_id)`, which always returns a
complete, cleaned block. `DEFAULT_PACK_COMPATIBILITY` is deliberately **permissive**
so a pack that declares nothing behaves exactly as it did before this gate — the
declaration requirement is enforced by a structural test over `PACK_REGISTRY`, not
by silently refusing to run an undeclared pack.

### Required vs. optional — declare soft dependencies as OPTIONAL

`cloud_ops` requires the MSP-B4 signatures and the MSP-B0/B7 operational-event
stream, but declares MSP-B3 (`cmdb_dependency`) and MSP-B5 (`runbook_match`) as
**optional**, because the pack degrades honestly without them *by design*:

- without B3 a recurrence stays *unlocated* and still emits (MSP-B4 AC5);
- without B5 the runbook leg downgrades to an explicit "runbook match
  unavailable" label (MSP-B6 T6).

Declaring those as required would misreport a designed graceful degradation as an
incompatibility. **If the pack still produces honest findings without it, it is
optional.**

## 2. What the platform provides

[`platform_capabilities.py`](../backend/discovery/packs/platform_capabilities.py)
is the single source of truth for both halves of the comparison:

| Surface | Meaning |
|---------|---------|
| `PLATFORM_VERSION` | The discovery platform's capability version (currently `2.0.0`). |
| `NORMALISED_CONCEPTS` | Every normalised signal concept the platform provides, each stamped with the platform version (`since`) that introduced it. |

The module is **dependency-free** (no `app` import, no I/O) so the gate can run in
*both* the API layer and the discovery runner without the runner taking an `app`
dependency — the same posture as `app/connector_roadmap.py`.

### Concepts are platform capabilities, not per-run data availability

A concept is listed when the platform can normalise it **at all** — i.e. the
ingestion + normalisation code ships. Whether a *given run* has that data is a
different question, answered by connector selection and the existing per-source
degradation rules. A pack whose source is not connected degrades to
partial/unavailable findings; it is **not** "incompatible". Conflating the two
would turn a disconnected connector into a refused pack.

### The MSP-B4 concepts (the sub-task's named dependency)

| Concept | `since` |
|---------|---------|
| `incident_workflow` | `1.0.0` |
| `resolution_signature` | `1.9.0` |
| `incident_identity_signature` | `1.9.0` |
| `assignment_group_routing` | `1.9.0` |

Note the deliberate asymmetry: MSP-B4 added the deterministic **signatures** and
group-routing history, *not* ServiceNow incident normalisation itself, which
`enterprise_ops` has consumed since 1.0. Stamping `incident_workflow` at `1.9.0`
would wrongly refuse `enterprise_ops` on the platform it has always run on — the
structural test `test_declared_floor_covers_the_concepts_the_pack_requires` caught
exactly that during implementation.

## 3. The gate

[`pack_compatibility.py`](../backend/discovery/packs/pack_compatibility.py)
compares the two and returns a `PackCompatibility` report.

`assert_selection_activatable(pack_ids)` is the one call an activation edge makes.
It raises `PackIncompatibleError` naming **every** incompatible pack in a
multi-pack selection — not just the first — so a user fixing a selection sees all
of it at once.

### Unmet-requirement kinds

| Kind | Cause |
|------|-------|
| `platform_version_below_minimum` | Platform is below the declared inclusive floor. |
| `platform_version_above_maximum` | Platform is above the declared inclusive ceiling. |
| `required_concept_unavailable` | Concept exists but was introduced in a later platform version. |
| `required_concept_unknown` | Concept the platform does not provide at **any** version (unshipped or misspelled). |
| `invalid_compatibility_declaration` | A declared bound (or the platform version) is unparseable. |

### Fail-closed, and never over-refuse

- **Fail closed:** an unparseable declared bound is an unmet requirement, *not* an
  ignored bound. A typo must never silently widen a range.
- **Never over-refuse:** an **unknown pack id** is not a compatibility failure.
  `get_pack()` resolves an unknown id to the default pack (warning, not raising),
  and the gate checks the *resolved* pack — so unknown-id behaviour is
  byte-identical to before this gate existed.

### The refusal reason (AC1)

Version bounds are stated individually; unmet concepts are collapsed into one
clause naming each concept, because a version-floor failure normally drags every
concept with it and five repetitions of the same sentence bury the actual cause.
Each concept still gets its own `UnmetRequirement` in `unmet`, so nothing is lost
structurally.

```
Pack 'cloud_ops' (version 1.2.0) cannot be activated on platform version 1.5.0:
requires platform version >= 1.9.0 (this platform is 1.5.0); requires normalised
concepts this platform version does not provide: resolution_signature (introduced
in platform version 1.9.0), incident_identity_signature (introduced in platform
version 1.9.0), assignment_group_routing (introduced in platform version 1.9.0),
operational_event (introduced in platform version 1.9.0).
```

## 4. Where the gate runs

[`app/pack_activation.py`](../backend/app/pack_activation.py) is the ONE place the
API layer refuses an incompatible pack, so the two edges cannot drift. It delegates
the verdict to the discovery layer (a structural test pins that the app layer never
re-implements the rule) and adds the two app-layer concerns:

- the `pack.activation_refused` telemetry event — pack ids, the **named** unmet
  requirements, and the platform version; no credentials, no PII, and a telemetry
  failure never masks the refusal;
- `compatibility_snapshot()` — the run-scoped verdict persisted at launch.

| Edge | Behaviour |
|------|-----------|
| `POST /api/stack-builder/launch` | **409** with the reason as `detail`. Runs *before* the run id is generated and before any persistence — a refused launch leaves no half-created run. |
| `POST /api/runs/{run_id}/compute` | **409** with the reason as `detail`. Runs *after* the 404 existence check (a nonexistent run stays a 404) but *before* `_set_status("running")` and before the background task is queued. Gates the same effective selection the background task will execute — the launch record's `packIds` plus the request's. |
| `discovery/runner.py` `run()` | Re-asserts the gate at the top of the function, deliberately **not** wrapped in a try/except — the same posture as the `cloud_ops` four-part-contract violation. A CLI/direct caller that never touches an API edge cannot execute an incompatible pack. |

409 (not 422) mirrors the roadmap-connector connect guard in `main.py`, which
refuses a non-connectable connector the same way.

## 5. What gets recorded (AC5 groundwork)

So run health reports a pack's version and state **as evaluated then**, rather than
re-deriving it from a registry that may have moved on:

| Location | Content |
|----------|---------|
| Run record | `platformVersion`, `packCompatibility` (keyed by pack id) |
| Run-scoped KV | `pack_compatibility:{run_id}` |
| Runner payload | `platformVersion`, `packCompatibility` |
| Telemetry | `pack.activation_refused` |

## 6. Changing things

- **Adding a pack:** declare a `compatibility` block. A pack without one fails
  `test_every_registered_pack_declares_compatibility`.
- **Adding a normalised concept:** add a `ConceptSpec` to `NORMALISED_CONCEPTS`
  stamped with the release that ships it.
- **Bumping `PLATFORM_VERSION`:** when the platform's capability surface changes in
  a way a pack could depend on.
- **Note:** adding or editing a `compatibility` block is *declarative* and does not
  change detector or scorer logic, so it does **not** require a `packVersion` bump
  (see the pack-versioning rule in `CLAUDE.md`). Bumping would change run stamps
  for no behavioural reason.

Structural tests keep the declarations honest and fail the build rather than
letting a bad declaration surface as a runtime refusal in front of a customer:

- every registered pack declares a compatibility block;
- every declared concept exists in the vocabulary;
- every declared bound is parseable;
- a pack's declared floor covers the `since` of every concept it requires;
- required and optional concept lists do not overlap;
- **every shipped pack is activatable on the current `PLATFORM_VERSION`** — the
  regression bar: adding this gate must not refuse a pack that runs today.

## 7. Tests

| Suite | Covers |
|-------|--------|
| [`discovery/tests/test_pack_compatibility.py`](../backend/discovery/tests/test_pack_compatibility.py) | The rule: version parsing/ranges, concept availability, declaration integrity, refusal reasons, the runner gate. DB-free. |
| [`tests/unit/test_pack_activation_gate.py`](../backend/tests/unit/test_pack_activation_gate.py) | The shared app-layer gate + refusal telemetry + the compute edge's selection resolution. DB-free. |
| [`tests/contract/test_pack_compatibility_activation.py`](../backend/tests/contract/test_pack_compatibility_activation.py) | The HTTP contract: 409 + named reason on both edges, no half-created run, no queued background task, run-record persistence. Needs the contract DB. |
