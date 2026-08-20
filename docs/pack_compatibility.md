# Pack Compatibility, Lifecycle State & Activation

**Story:** 2.0-C1 — Pack Compatibility, Safe Disable & Rollback (AT-822)

| Task | Delivers | Criterion |
|------|----------|-----------|
| **T1 — AT-826** | Compatibility declaration + activation gate | **AC1** — a pack declaring an unmet platform range cannot be activated; the refusal names the unmet requirement |
| **T2 — AT-827** | Safe disable state machine (§8) | **AC2** — disabling stops future execution while all historical findings remain retrievable and correctly labelled |
| **T3 — AT-828** | Version rollback (§9) | **AC3** — rollback causes subsequent runs to use the prior version; existing findings retain their original version stamps |
| **T4 — AT-829** | Never delete history (§10) | **AC4** — no path in disable/rollback/remove deletes findings, evidence, or run records — enforced at the data layer |
| **T5 — AT-830** | Surfacing (§11) | **AC5** — run health reflects pack state and version accurately across all transitions |

Packs are versioned and stamped per run (R16-B1 §4), 1.9 added two more packs, and
1.9.1 enabled multi-pack runs. What was missing: what happens when a pack version
is incompatible with the platform version, when a customer wants a pack turned off,
and when a pack upgrade must be reversed — **without destroying run history**.

> **Scope.** §1–§7 cover compatibility (T1); §8 disable (T2); §9 rollback (T3);
> §10 the never-delete-history guarantee (T4); §11 the UI surfacing (T5). With T5 the
> 2.0-C1 story is complete — all five acceptance criteria are discharged.

## The three lifecycle dimensions

A pack has three independent lifecycle facts per organisation, and it is worth being
precise that they do not interact:

| Dimension | Question | Values | Owner |
|-----------|----------|--------|-------|
| **Compatibility** | *Can this pack run here at all?* | derived from the declaration | T1 |
| **State** | *Is it turned on?* | `active` / `disabled` | T2 |
| **Version** | *Which version does it run?* | current, or a pinned prior version | T3 |

Disabling a rolled-back pack does not clear its pin; re-enabling does not lose it.
They share one row and one audit trail (§9.5) but never overwrite each other.

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
| [`tests/unit/test_pack_version_rollback.py`](../backend/tests/unit/test_pack_version_rollback.py) | Rollback: the archive's integrity, pin transitions, resolution of detectors/config/stamp, refusal of unservable targets, fail-soft. DB-free. |
| [`tests/contract/test_pack_disable_lifecycle.py`](../backend/tests/contract/test_pack_disable_lifecycle.py) | Disable over HTTP: the state endpoints, RBAC, launch exclusion, historical findings retrievable + labelled, run-health state across transitions. Needs the contract DB. |
| [`tests/contract/test_pack_version_rollback_lifecycle.py`](../backend/tests/contract/test_pack_version_rollback_lifecycle.py) | Rollback over HTTP: the version endpoint, RBAC, runs after rollback using the prior version, findings keeping original stamps, run-health across transitions. Needs the contract DB. |

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

---

# 9. Version rollback (T3 / AT-828)

**Criterion discharged:** AC3 — *rollback causes subsequent runs to use the prior
version; existing findings retain their original version stamps*, and nothing is
rewritten retroactively.

## 9.1 The problem with "just pin the version number"

A pack version is a *stamp* (`packVersion`, R16-B1 §4) whose whole purpose is to let
governance tell a data change from a pack-logic change. So the naive rollback —
store `1.1.0` and keep running the current code — would be **actively harmful**: it
produces runs stamped `1.1.0` that behaved like `1.2.0`, corrupting the one signal
the stamp exists to carry.

A rollback is therefore only honest if the platform can genuinely *serve* the prior
version's behaviour. Two things carry that behaviour:

| Carries behaviour | Where it lives | How it is served |
|-------------------|----------------|------------------|
| detector list | `PACK_REGISTRY[...]["detectors"]` | declared per version in `versionHistory` |
| thresholds / calibration / terminology | external config JSON | the **archived artifact** in `versions/` |

That is why only **config-driven packs** are rollbackable today. `cloud_ops` and
`security_ops` externalised their calibration (MSP-B6 T1 / MSP-B12 T1: "a config
change alters behaviour with no code deploy"), which is exactly what makes a prior
version reproducible. A code-only pack has nothing to archive, so rollback is
**refused** for it — naming that reason.

## 9.2 The version archive

[`discovery/packs/versions/`](../backend/discovery/packs/versions/README.md) holds
the config artifact of each prior runnable version. The files are **verbatim
history recovered from git**, not hand-written:

| File | Version | Recovered from |
|------|---------|----------------|
| `cloud_ops_pack_config.v1.1.0.json` | 1.1.0 | `ae4d6f3e` (MSP-B6 T4) |
| `security_ops_pack_config.v1.1.0.json` | 1.1.0 | `11e4aaf0` (MSP-B12 T2) |

A test asserts each artifact's own `packVersion` matches its filename and differs
from the current config — the guard that the archive is real history rather than a
copy of the current file renamed.

Both packs also had a `1.0.0`, but those were **scaffolds with zero detectors**.
Rolling back to a version that produces no findings would be a footgun dressed up as
a feature, so `1.0.0` is deliberately not offered.

**The discipline: archive on bump.** When you bump a config-driven pack's
`packVersion`, archive the outgoing config and add its `versionHistory` entry *in the
same PR*. That is the only moment the artifact is trivially available.

## 9.3 What a version declares

```python
"versionHistory": [                      # PRIOR versions only, newest first
    {
        "version":    "1.1.0",
        "configPath": ".../versions/cloud_ops_pack_config.v1.1.0.json",
        "detectors":  [...],             # that version's detector list
        "note":       "MSP-B6 T4 — before the MSP-B5 documentation-gap detector",
    },
],
```

`pack_config.resolve_pack_at_version(pack_id, version)` returns a **copy** of the
pack with that version's `packVersion`, `detectors`, and `config_path` substituted,
plus a `pinnedVersion` marker. The registry is never mutated. An unarchived version
raises `PackVersionUnavailable`, whose message names the versions that *are*
available.

## 9.4 How a pinned run actually runs the prior version

Three substitutions, all in `runner._resolve_pack_activation`:

1. **Version stamp** — `_effective_pack_version()` prefers the pin, so every
   opportunity is stamped with the version that actually produced it.
2. **Detectors** — `_detectors_for_pinned_version()` narrows the runner's current
   per-domain import list to the version's declared list, preserving declared order.
   Applied **only when a pin is active**, so an un-pinned run is byte-identical to
   before rollback existed. A declared detector this build no longer provides is
   skipped loudly; if *none* survive it raises rather than running an arbitrary set
   under that version's stamp.
3. **Config** — the archived artifact is published to a per-run
   [`contextvars`](../backend/discovery/packs/pack_version_context.py) mapping.
   Detectors and scorers call `get_detector_thresholds(section, fallback)` with no
   path argument, so threading a path through every signature would touch dozens of
   call sites; instead each loader applies one precedence:

   ```
   explicit path argument  →  this run's pinned path  →  the pack's default path
   ```

   A `ContextVar` (not a module global) for the same reason as
   `discovery/ingest/__init__.py`'s live-connector context: runs execute
   concurrently in background threads under `copy_context().run(...)`, and a global
   would leak one tenant's rolled-back config into another tenant's run.

## 9.5 Nothing is rewritten retroactively

A pin is a **forward-looking configuration row**. There is no backfill step of any
kind, and the rollback path never reads or writes findings, evidence, or run records:

- an existing finding keeps the `packVersion` it was produced with, forever;
- a run launched before the rollback still records the version it ran;
- restoring afterwards does not rewrite the run that *did* use the pin;
- the rollback stays on the append-only history after a restore.

Run health reads each run's **historical** pin (`run["pinnedPackVersions"]`, written
by the runner), never the org's current pin — so rolling back today cannot backdate
what yesterday's run reports.

Version and state transitions share one `pack_states` row and one
`pack_state_history` trail, with `revision` counting every change of either kind.
That is deliberate: AT-830 has to surface "what has this org done to this pack", and
one trail means one answer. The columns are additive (alembic `0032`,
`ADD COLUMN IF NOT EXISTS`), so a pack with no pin reads `NULL` and behaves exactly
as before.

## 9.6 Fail-soft, in the safe direction

An unreadable state store, or a pin whose artifact was dropped in a later release,
degrades to the **current** version rather than failing the run. Note *why* that is
the safe direction: the run then executes and is stamped with the same version, so it
stays self-consistent — it simply does not honour the rollback. An honest
current-version run beats a run stamped one version and behaving as another. A
dropped artifact also emits `pack.version_pin_unservable`, so a stale pin is never
silent.

## 9.7 API

| Endpoint | Role | Purpose |
|----------|------|---------|
| `PUT /api/packs/{pack_id}/version` | **owner** | `{"version": "1.1.0"}` rolls back; `{"version": null}` restores the current version. Idempotent. **409** when the version is not archived, naming what is. |
| `GET /api/packs/state` | viewer | Adds `pinnedVersion`, `effectiveVersion`, and `availableVersions` per pack |
| `GET /api/packs/{pack_id}/state/history` | analyst | Rollback/restore transitions carry `previous_version` / `resulting_version` |

`effectiveVersion` is the number an operator actually cares about — what a run
started *now* would execute and stamp — while `packVersion` stays what the registry
ships.

## 9.8 What gets recorded

| Location | Content |
|----------|---------|
| Run record | `pinnedPackVersions`; `packVersions` records the **pinned** version for a rolled-back pack, because that is what the run executes |
| Run-scoped KV | `pinned_pack_versions:{run_id}` (launch edge *and* runner) |
| Runner payload | `pinnedPackVersions` |
| Run health | `pinned_version` + `rolled_back` per pack row, plus a top-level `pinned_pack_versions` |
| Audit | `pack_state_changed` with the version fields |
| Telemetry | `pack.version_pinned`, `pack.version_pin_unservable` |
| Domain audit trail | `pack_state_history` (`rollback` / `restore` transitions) |

---

# 10. Never delete history (T4 / AT-829)

**Criterion discharged:** AC4 — *no path in disable / rollback / remove deletes
findings, evidence, or run records — enforced at the data layer, tested.*

## 10.1 What was actually true before this task

Worth stating plainly, because it shaped the design. `app/db.py` carried a comment
claiming *"the app DB role has UPDATE but not DELETE"*. That was **not true** of this
provisioning path: `provision.sql` grants each app role `ALL PRIVILEGES ON ALL
TABLES`, which includes `DELETE` and `TRUNCATE`. There was no data-layer enforcement —
only the incidental fact that no code happened to delete run history. AC4 asks for
enforcement, so this task built it and corrected the comment.

## 10.2 The protected set

[`app/history_retention.py`](../backend/app/history_retention.py) is the **single
declaration** of which tables hold run history. Three enforcement layers read it, so
the guarantee never rests on one mechanism.

| Table | Why protected |
|-------|---------------|
| `runs` | run records — pack ids, pack versions, the configuration executed |
| `run_events` | the run event log (soft-deleted, never removed) |
| `kv` | run-scoped artifacts: findings (`opps:{run_id}`), evidence, clusters, roadmap, report |
| `opportunity_instances` | per-instance pack id + pack version stamps (R16-B1 §4) |
| `pack_state_history` | the append-only pack lifecycle trail (T2/T3) |

**Deliberately NOT protected**, each with a justification in the same module — listed
explicitly so a reviewer sees these were decisions, not omissions:

| Table | Why deletion is correct |
|-------|-------------------------|
| `retrieval_chunks` | derived vector index; R18-B2 freshness purges chunks when a source artifact changes. Re-embeddable, loses no history — the finding, its evidence, and the evidence pointer all survive. |
| `retrieval_refresh_queue` | transient worker queue, not a record |
| `entity_relationships` | cross-run graph state that `relationship_mapper` prunes when a relationship no longer holds; a current view, not history |

## 10.3 Three enforcement layers

**1. Database privileges — the real data-layer enforcement.** `provision.sql` and
migration `0033` `REVOKE DELETE, TRUNCATE` on every protected table from each app
login role, *after* the `GRANT ALL PRIVILEGES` block (ordering is load-bearing — a
REVOKE before the GRANT would be undone; a test pins the order). A bug, a rogue query,
or a future code path physically cannot remove a finding.

**2. A build-breaking static sweep.**
[`tests/unit/test_never_delete_history.py`](../backend/tests/unit/test_never_delete_history.py)
walks the tree at test time and fails CI on any `DELETE`/`TRUNCATE` against a
protected table, so the problem surfaces in review rather than as a privilege error in
production. Two details make it trustworthy:

- it scans **non-docstring string literals via AST**, because prose *about* SQL
  produces nonsense matches (`"a DELETE or TRUNCATE against any of these"` parses
  "against" as a table name);
- it asserts the sweep **finds** known files and a known legitimate delete, so a
  broken matcher fails loudly instead of passing vacuously.

It also fails on a delete against an *unclassified* table — forcing the
protected-or-deletable decision to be made and justified rather than skipped.

**3. A runtime guard.** `guard_delete(table)` / `assert_no_history_deletion(sql)` are
the seam any code that must touch history-adjacent SQL calls. `db.delete_run_events`
self-checks through it, so if that function ever became a hard delete it fails with the
named retention reason rather than an opaque permission error.

## 10.4 Soft delete is not deletion

`db.delete_run_events` is named for what it means, not what it does: it is an `UPDATE`
setting `is_deleted`. `insert_run_events` re-activates a rewritten `(run_id, seq)` and
`get_run_events` filters the flag, so rewriting a shrunk event list drops stale rows
from **reads** while the rows remain. That shape is compatible with the REVOKE and is
the pattern any future "removal" should take. A test pins the function body.

## 10.5 The third verb: "remove"

Disable and rollback are features (T2/T3). **Remove** is not a runtime API — there is
deliberately no "delete pack" endpoint. Removal means a pack leaving the registry, a
deploy-time change. What must hold is that its history stays present **and reachable**.

Testing this surfaced a real defect in the T2/T3 work: a removed pack's rows survived
in the database, but `GET /api/packs/{id}/state/history` 404'd on a registry lookup and
`GET /api/packs/state` dropped the row entirely. **History you cannot reach is
functionally deleted**, so both were fixed:

- `pack_state_view` now includes **orphaned rows** — state for a pack no longer in the
  registry — flagged `registered: false`, with version fields `null` because the
  registry no longer declares them (the platform reports what it still knows and does
  not invent a version for a pack it no longer ships);
- the history endpoint serves a removed pack's retained trail, gated on
  `has_pack_lifecycle_record` rather than registry membership.

The read/write asymmetry is intentional: **reads stay open** so history is reachable,
while **writes 404** — a pack that is gone has nothing to disable. A genuinely unknown
id (a typo, no lifecycle record) is still a 404, so the allowance cannot turn every bad
id into a 200.

## 10.6 Tests

| Suite | Covers |
|-------|--------|
| [`tests/unit/test_never_delete_history.py`](../backend/tests/unit/test_never_delete_history.py) | The static sweep + provision/migration/protected-set coherence. DB-free. |
| [`tests/unit/test_never_delete_history_data_layer.py`](../backend/tests/unit/test_never_delete_history_data_layer.py) | **The AC4 data-layer test.** Runs the production `PostgresPackStateStore` against a fake connection that RECORDS every statement, attempts disable / rollback / remove, and asserts no statement deletes a protected table and the seeded findings/evidence/runs are byte-identical. Plus a direct delete attempt on every protected table. DB-free. |
| [`tests/contract/test_pack_lifecycle_retention.py`](../backend/tests/contract/test_pack_lifecycle_retention.py) | End-to-end through the real database: all three verbs in sequence with findings, evidence, and run records re-read over the API after each. Needs the contract DB. |

The data-layer suite asserts against the SQL the production code path **actually
emits**, not against a re-implementation of it — which is what makes it a data-layer
test rather than a mock of one.

---

# 11. Surfacing (T5 / AT-830)

**Criterion discharged:** AC5 — *run health reflects pack state and version accurately
across all transitions.* Two surfaces: the Run Health packs panel, and the finding
itself.

Everything here reads fields the backend already produced in T1–T4; no new backend
behaviour. The work is making them legible.

## 11.1 Run health: two pills, not one word

The packs panel shows **state** and **version** as separate pills, because they are
two orthogonal facts and a pack can be both disabled and rolled back at once:

| Pill | Values | Source |
|------|--------|--------|
| State | `Active` / `Disabled` | `pack_state` — read LIVE (T2) |
| Version | `Version 1.2.0` / `Rolled back to 1.1.0` / `Version unavailable` | `pack_version` + `rolled_back` (T3) |

`packLifecycleLabel()` (exported from `RunHealthDashboardPage.tsx` and unit-tested
directly) is the single place that mapping lives. Collapsing them into one word —
"rolled back" as if it were a state — would have made the both-at-once case
unrepresentable.

Two explanatory notes render beneath the pills when relevant:

- **disabled** — "will not run again … everything it produced below is kept exactly as
  it executed", so a reader does not read a disable as data loss;
- **rolled back** — "pinned to version X — a deliberate rollback, not the version
  currently shipped", so a lower version number is not mistaken for drift.

**A disabled pack does not make the panel look unhealthy.** Disabling is intentional
configuration, so the panel's `data-state` stays `healthy` — a test pins this. Tone
follows the same logic: `warn` (informational, visible) and never `bad`.

## 11.2 Run health: packs that did not run

`excluded_packs` is rendered as a *Selected but not run* block naming each pack and
stating the reason, so an analyst seeing one pack where two were selected is never left
to infer why. It also notes that earlier runs' findings are unaffected.

The empty state is branched on the same data: when **every** selected pack is
disabled, the panel says *"No pack executed for this run — every selected pack is
disabled…"* instead of the generic *"No pack executions yet"*, which would be actively
misleading in that case.

## 11.3 Findings: the version that produced them

`PackProvenanceRow` adds a **Produced by** row to the finding detail showing the pack
id and `v{packVersion}`, following the existing "Identifier" row pattern.

The version stamp is R16-B1 §4 provenance — what lets a reader tell a DATA change from
a PACK LOGIC change — so it belongs on the finding, not only in run health. It is the
version that produced **this** finding and never moves: rolling the pack back or
disabling it afterwards leaves it alone (AC3).

When the producing pack is disabled today, the backend's `packStateLabel` (T2) renders
alongside it — the label sits *next to* the finding, it never replaces or suppresses
it, which is the reader-facing half of AC2. A finding with no pack stamp (runs
materialised before R191-P1 T3) renders nothing rather than an invented value.

## 11.4 Contract

`contracts/API_CONTRACT.md` **v1.15 → v1.16**, per the repo rule that a
`frontend/src/types/*.ts` change requires a contract bump. Everything is additive and
optional, so pre-v1.16 consumers are unaffected:

- `runHealth.ts` — `pack_state`, `pinned_version`, `rolled_back` per pack row;
  `excluded_packs`, `pinned_pack_versions` on the response;
- `analystReview.ts` — `packState`, `packStateLabel` on `OpportunityCandidate`
  (`packId`/`packVersion` were already documented at v1.11).

v1.16 also documents the four pack-lifecycle routes from T2/T3 and the new 409s on
launch/compute, which had shipped but were not yet in the contract.

## 11.5 Tests

[`frontend/src/__tests__/PackLifecycleSurfacing.test.tsx`](../frontend/src/__tests__/PackLifecycleSurfacing.test.tsx)
(26 tests) walks the transitions rather than checking one static shape: active →
disabled → rolled back → both, plus multi-pack independence, the excluded block, the
branched empty state, the healthy-not-broken panel state, and a pre-2.0-C1 response
with none of the lifecycle fields present.
