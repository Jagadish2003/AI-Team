# Pack Compatibility, Lifecycle State & Activation

**Story:** 2.0-C1 — Pack Compatibility, Safe Disable & Rollback (AT-822)

| Task | Delivers | Criterion |
|------|----------|-----------|
| **T1 — AT-826** | Compatibility declaration + activation gate | **AC1** — a pack declaring an unmet platform range cannot be activated; the refusal names the unmet requirement |
| **T2 — AT-827** | Safe disable state machine (§8 below) | **AC2** — disabling stops future execution while all historical findings remain retrievable and correctly labelled |

Packs are versioned and stamped per run (R16-B1 §4), 1.9 added two more packs, and
1.9.1 enabled multi-pack runs. What was missing: what happens when a pack version
is incompatible with the platform version, and when a customer wants a pack turned
off — **without destroying run history**.

> **Scope.** Rollback (AT-828), the exhaustive never-delete-history data-layer
> sweep (AT-829), and UI surfacing (AT-830) are separate tasks layered on top of
> what is described here. §1–§7 cover compatibility (T1); §8 covers disable (T2).

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
| [`tests/unit/test_pack_state_machine.py`](../backend/tests/unit/test_pack_state_machine.py) | The disable state machine: transitions, idempotence, org isolation, append-only history, exclusion, labelling, fail-soft reads. DB-free. |
| [`tests/contract/test_pack_compatibility_activation.py`](../backend/tests/contract/test_pack_compatibility_activation.py) | The HTTP contract: 409 + named reason on both edges, no half-created run, no queued background task, run-record persistence. Needs the contract DB. |
| [`tests/contract/test_pack_disable_lifecycle.py`](../backend/tests/contract/test_pack_disable_lifecycle.py) | Disable over HTTP: the state endpoints, RBAC, launch exclusion, historical findings retrievable + labelled, run-health state across transitions. Needs the contract DB. |

---

# 8. Safe disable state machine (T2 / AT-827)

**Criterion discharged:** AC2 — *disabling a pack stops future execution while all
historical findings remain retrievable and correctly labelled.* Re-enable is
supported.

## 8.1 The state machine

Two states, two transitions, per **(org, pack)**:

```
active  --disable-->  disabled
disabled --enable-->  active
```

`active` is the DEFAULT and is represented by the **absence of a row**. Provisioning
the tables therefore changes no behaviour until a customer disables something —
there is no seed step and no backfill. Both transitions are **idempotent**:
re-disabling an already-disabled pack returns `changed: false` and writes no history
row.

Implementation: [`app/pack_state.py`](../backend/app/pack_state.py). Storage:
`pack_states` + `pack_state_history` (alembic `0031`, mirrored into
`provision.sql`), modelled in
[`database/models/pack_states.py`](../backend/database/models/pack_states.py).

Pack id validation is deliberately **stricter than `get_pack()`**: an unknown id
raises `PackNotFound` rather than resolving to the default pack. Silently disabling
`service_cloud` because an operator typo'd a pack id would be a serious foot-gun.

## 8.2 Disable EXCLUDES, it does not refuse

This is the key design decision, and it differs from T1's compatibility gate on
purpose:

| | Meaning | Behaviour |
|---|---------|-----------|
| **incompatible** (T1) | "this pack CANNOT work on this platform" — a configuration error | the activation edge **refuses** with 409 naming the unmet requirement |
| **disabled** (T2) | "this pack is intentionally turned off" — a deliberate, ongoing customer state | the pack is **excluded** and the run proceeds with what remains |

Refusing every run after a disable would make disable unusable: the customer would
also have to edit every template and industry default that references the pack.
Excluding matches how this codebase treats a source that is not connected —
degrade, don't crash.

**Disabled is evaluated BEFORE compatibility.** A pack the customer already turned
off must not be able to fail a run on compatibility grounds — it is not going to
execute either way, so refusing the run over it would be noise.

**The exclusion is loud, never silent** — the same discipline as MSP-B7's noise
floors and run budgets. And if the exclusion would leave **nothing** to run, that IS
an error (`AllPacksDisabledError` → 409): a run with zero packs would report success
having produced nothing, and must never quietly fall back to the default pack.

## 8.3 Where disable is enforced

| Edge | Behaviour |
|------|-----------|
| `POST /api/stack-builder/launch` | Disabled packs dropped from the selection; `packIds` and `packVersions` record only what will run; `excludedPacks` names what was dropped. 409 if every selected pack is disabled. |
| `POST /api/runs/{run_id}/compute` | Same resolution over the same effective selection. 409 if every selected pack is disabled. |
| `discovery/runner.py` `run()` | **The guarantee.** `_resolve_pack_activation` narrows `_pack_configs` before any detector runs, so a disabled pack's detectors never execute even for a CLI/direct caller that never touched an API edge. |

## 8.4 Historical output: kept, and labelled

Disabling **never** removes or rewrites a finding. What changes is that a reader can
tell the finding came from a pack that is no longer running.
`opportunity_display.with_pack_state` stamps two ADDITIVE fields (the
`connector_roadmap.annotate_connector` pattern):

```
packState      : "active" | "disabled"
packStateLabel : "Produced by a now-disabled pack"   (absent when active)
```

Nothing else is touched — not the score, not the evidence ids, and **not the
`packVersion` stamp**, so R16-B1 §4 provenance (and AC3's guarantee) survives.

It is wired into `with_display_title`, the funnel every opportunity serve site
already uses, so the label reaches the list, decision, override, roadmap, executive
report, and blueprint paths alike. `with_display_titles` / `with_display_all` read
the org's pack state **once per list** rather than once per finding.

**Reads are fail-soft.** If the state store cannot be read — including a deployment
that has not yet applied migration `0031` — every pack reads as active and the
finding is served **unlabelled** rather than not at all. "Historical findings remain
retrievable and viewable" outranks the label. The failure is logged. Writes are NOT
fail-soft: a disable that did not persist must never look like it succeeded.

## 8.5 Nothing is ever deleted (AC4 contribution)

There is **no delete path** in `app/pack_state.py` — not for state, not for history,
and emphatically not for findings, evidence, or run records. Re-enabling writes a
*new* state and a *new* history row; it does not remove the disable. That is what
makes the transition history an audit trail. Tests pin that the module contains no
`DELETE`/`DROP`/`TRUNCATE`, that the store contract exposes no delete/remove method,
and that a disable→enable cycle leaves both transitions on the trail. AT-829 owns
the exhaustive data-layer sweep.

## 8.6 API

| Endpoint | Role | Purpose |
|----------|------|---------|
| `GET /api/packs/state` | viewer | Every pack with its state, revision, reason, and current version |
| `PUT /api/packs/{pack_id}/state` | **owner** | `{"state": "disabled"\|"active", "reason": "..."}` — idempotent |
| `GET /api/packs/{pack_id}/state/history` | analyst | Append-only transitions, newest first |

Reading is `viewer` so a viewer who sees a "now-disabled pack" label can confirm it.
Changing is `owner`: turning off a pack alters what every future run for the whole
organisation produces — the same bar as connector connect/disconnect.

## 8.7 What gets recorded (AC5)

| Location | Content |
|----------|---------|
| Run record | `excludedPacks` (+ `packIds` narrowed to what runs) |
| Run-scoped KV | `excluded_packs:{run_id}` — written by the launch edge *and* the runner (the only record for a direct/CLI run) |
| Runner payload | `excludedPacks` |
| Run health `GET /api/run-health/packs` | `pack_state` per pack row + a top-level `excluded_packs` list |
| Audit | `pack_state_changed` (org-wide audit stream) |
| Telemetry | `pack.state_changed`, `pack.execution_skipped` |
| Domain audit trail | `pack_state_history` table |

Note the deliberate split in the run-health packs panel: every other field comes
from **immutable run fields**, precisely so a later pack change cannot rewrite what
the dashboard says executed. `pack_state` is the one field read **live**, because
"is this pack still running?" is a question about *now*, not about the run. So a
pack disabled after a run reads `disabled` while its recorded `pack_version` and
`detectors` stay exactly as executed.
