# Pack Deprecation & Migration — 2.0-C4

Read this before deprecating a pack, changing a deprecation declaration, or writing
any code that reads a pack's deprecation state.

This document currently covers **T1 (AT-842) — deprecation metadata**,
**T2 (AT-843) — notice surfacing**, and **T3 (AT-844) — migration assist**. Grace
behaviour (AT-845) and the audit events (AT-846) are separate sub-tasks that layer on
these; each adds its own section here as it lands.

---

## 1. What a deprecation is, and why it is not a delete

With an ecosystem, packs get superseded. The failure mode this feature exists to
prevent is a customer discovering that a pack their discovery configuration depends on
has changed behaviour, or stopped existing, without warning.

So a superseded pack goes through three states, in this order:

| Phase | Meaning | Does the pack run? |
|---|---|---|
| `active` | No deprecation applies. | Yes |
| `grace` | Deprecated. Notice is showing, with reason, dates, and replacement. | **Yes — normally** |
| `grace_expired` | The announced grace period has passed. | No — AT-845 safe-disables it |

The end state is *safe-disabled*, not deleted: it reuses 2.0-C1's disable semantics, so
every historical finding, its evidence, and its run records stay intact and viewable
(2.0-C1 AC2 / AC4). Deprecation removes a pack from *future* runs and never touches
what it already produced.

The phase is **derived on every read** from the declared dates rather than stored, so a
grace period expires on its own. Nothing has to run for it to lapse, and nothing can
forget to.

## 2. What is declared

Each pack declares a `deprecation` block in `PACK_REGISTRY`
(`discovery/packs/pack_config.py`), read through `get_pack_deprecation_declaration()`,
which always returns a complete normalised block:

```python
"deprecation": {
    "status":          "deprecated",        # "active" (default) | "deprecated"
    "versions":        ["1.1.0"],           # [] / omitted ⇒ EVERY version
    "reason":          "Superseded by the Cloud Operations pack.",
    "deprecatedOn":    "2026-08-01",        # ISO date the notice starts
    "gracePeriodDays": 90,                  # derives graceEndsOn from deprecatedOn
    "graceEndsOn":     "2026-10-30",        # authoritative end date, when known
    "replacement": {                        # optional — the migration path
        "packId":     "cloud_ops",
        "minVersion": "1.2.0",
        "notes":      "Reconnect the AWS and Azure event sources before migrating.",
    },
}
```

Declaring nothing is the normal case: an undeclared pack is `active`, and no pack ships
a deprecation today.

Deprecation lives beside `compatibility` (AT-826) and `certification` (AT-831) so that
everything a pack states about itself is read from one place. It deliberately does **not**
live in per-org `pack_states`: that table is the *customer's* dimension (disable,
rollback), and merging the two would let a customer "undeprecate" a superseded pack, or
make a vendor notice look like a customer decision.

### No signature

Unlike certification, a deprecation carries no signature. A certification is a claim
about a *third party* — the pack author would otherwise be vouching for themselves — so
the signature is what makes the badge worth anything. A deprecation is the registry
shipper stating that its **own** pack is superseded. There is no claim to protect.

## 3. Version scoping

A deprecation names the versions it applies to. An empty `versions` list means every
version — the "this pack is superseded" case, which is the common one.

Naming versions covers the narrower "this release line is superseded, the current one is
fine" case. That case is real because 2.0-C1 T3 rollback lets an org pin an archived
version, so an archived version can outlive its usefulness independently of the pack.

`get_pack_deprecation(pack_id, version=...)` defaults to the pack's *current*
`packVersion`; pass a version to ask about a pin.

A scoped version that the pack does not declare — neither its current version nor an
archived one — is flagged `unknown_version_scope`. That defect matters more than it
looks: a typo'd scope deprecates **nothing**, so without the flag it would fail silently
and the notice would simply never appear.

## 4. The grace period

Two ways to declare the end of grace, and one way to declare that there isn't one:

* `graceEndsOn` — the authoritative last day the pack runs normally;
* `gracePeriodDays` — derives that date from `deprecatedOn`;
* **neither** — the grace is *open-ended*.

Open-ended grace is a first-class state, not an omission: "deprecated, no removal date
announced yet" is a real and common position to take. It surfaces the notice and can
**never** reach `grace_expired`, so it can never trigger a safe-disable. Silently
defaulting to some grace length would take a customer's pack offline on a date nobody
declared.

Boundary: `graceEndsOn` is the **last day the pack runs normally**. Expiry is strictly
after it, matching the certification review-age rule.

When both `graceEndsOn` and `gracePeriodDays` are declared and disagree, the conflict is
named (`conflicting_grace_period`) and **the later date wins**. A declaration mistake
must never shorten a customer's grace.

## 5. Failure posture: notice loudly, never auto-disable on bad data

Evaluation never raises — the verdict is the return value, as with certification. But the
*direction* of the degradation is different, because the risks are different. A
certification that cannot be verified fails **closed** (the badge is withdrawn) because
the danger there is overclaiming. A deprecation's danger is the opposite: the two ways to
harm a customer are to hide a real notice, and to disable a working pack on the strength
of a malformed date. So:

| Defect | What happens |
|---|---|
| `missing_reason` | Still reports deprecated — suppressing a real notice is worse than an incomplete one. |
| `missing_deprecated_on` | Still reports deprecated; nothing to derive an end from, so grace is open-ended. |
| `unreadable_date` | Grace becomes **open-ended**. A typo must never expire a grace period. |
| `invalid_grace_period` | Grace becomes **open-ended**. Same reason. |
| `conflicting_grace_period` | The **later** date wins (§4). |
| `unknown_replacement_pack` | The replacement is **dropped**. A path to a pack that does not exist is worse than no path — AT-844 would offer a migration that cannot be applied. |
| `self_replacement` | Dropped, same reasoning. |
| `unknown_version_scope` | Reported; the deprecation applies to nothing (§3). |
| `invalid_status` | Flagged, and a populated block is still read as a notice. |

Every one of these is reported in `PackDeprecation.issues`, and a **shipped** declaration
carrying any of them fails the build (§7). The tolerant runtime behaviour is a safety
net, not the contract.

One inference is deliberate: a block that carries a reason and a date but forgets
`status` is read as **deprecated**. Reading it as active would suppress the notice, which
is the single failure this feature exists to prevent. That is also why
`get_pack_deprecation_declaration()` reports an undeclared `status` as empty rather than
`"active"` — the evaluator has to be able to tell "said nothing" from "said not
deprecated".

## 6. What T1 provides to the rest of the story

`discovery/packs/pack_deprecation.py` is dependency-free of `app` (the same posture as
`platform_capabilities.py` / `pack_compatibility.py` / `pack_certification.py`), so both
the API activation edges and the discovery runner can consult it without the runner
taking an `app` dependency.

| Surface | Consumer |
|---|---|
| `get_pack_deprecation(pack_id, version=…, as_of=…)` → `PackDeprecation` | the full verdict; `.to_dict()` is the audit shape |
| `deprecation_notice(...)` / `deprecation_notices(...)` | AT-843 — the compact projection a surface renders |
| `PackDeprecation.summary` | AT-843 — one sentence with reason, dates, and replacement, composed once so run configuration, run health, and findings cannot word it differently |
| `replacement_pack_id(...)` | AT-844 — the migration target, or `None` for "no path yet" |
| `is_grace_expired(...)` | AT-845 — the safe-disable trigger |
| `deprecation_summary(...)` | the run-record snapshot: what the position was *when the run executed*, including proof that non-deprecated packs were evaluated |

`deprecation_notice()` returns `None` — rather than an object saying "not deprecated" —
because a renderer shows a notice or shows nothing, and a falsy-but-present object
invites an empty banner on every healthy pack.

## 7. Tests

`backend/discovery/tests/test_pack_deprecation.py` (pure-Python, offline, no DB).

Structural tests keep the shipped registry honest: every shipped declaration must be
defect-free, an undeclared pack must not be deprecated by accident, and a shipped
replacement must be a registered pack.

Every date-dependent assertion injects `as_of`, exactly as the certification expiry tests
do. A grace-period test that reads the clock passes today and fails on some future
Tuesday when its fixture dates age out.

## 8. Deprecating a pack — the checklist

1. Add the `deprecation` block to the pack's `PACK_REGISTRY` entry, with a reason, a
   `deprecatedOn`, a grace period (or deliberately none), and a replacement if one exists.
2. Scope it with `versions` only if the deprecation is version-specific; leave it empty
   to supersede the whole pack.
3. Run `python -m pytest discovery/tests/test_pack_deprecation.py` — the structural tests
   reject an incomplete declaration, an unregistered replacement, and a typo'd version
   scope.
4. Remember what the grace period commits you to: on the day after `graceEndsOn`, every
   org still selecting that pack loses it from future runs (AT-845). Pick a date the
   replacement is actually ready for.
5. Nothing else is needed to make the notice appear: run configuration, run health,
   and the pack's findings all read the declaration (§9), so the block IS the release
   step.

---

# 9. Notice surfacing (T2 / AT-843)

## 9.1 The one rule

Every surface renders the SAME notice, built once by
`pack_deprecation.deprecation_notice()`. Nothing composes its own wording from the
declared fields.

That is not tidiness. A customer who reads "runs until 2026-09-29, replaced by Cloud
Operations" on the pack picker has to meet the identical sentence on the finding that
pack produced and in run health when the output changes. Three near-miss phrasings of
the same deprecation is how a customer concludes the platform is confused about its
own state, and it is exactly what one shared builder makes impossible. A test asserts
the three surfaces return character-identical `summary`, `statusLabel`, `graceEndsOn`,
and `replacementPackId`.

The corollary: **a pack that is not deprecated surfaces nothing.** No pill, no banner,
no "not deprecated" object to render. Every surface's map/field is absent or null.

## 9.2 The three surfaces

| Surface | Where | Shape |
|---|---|---|
| **Run configuration** | `GET /api/packs/state` → Discovery Plan | `deprecation` on each pack row (`PackDeprecationNotice \| null`) |
| **Run health** | `GET /api/run-health/packs` → packs panel | `deprecated` + `deprecation_*` fields on each pack row |
| **Findings** | every opportunity serve site, via `with_display_title` | `packDeprecated` + `packDeprecation*` fields on the finding |

Run configuration matters most: it is the moment someone is about to build a run on a
pack that is going away, and the only moment the warning can still change what they
do. Run health is where they look when the output surprises them. The finding is
where a reader who never saw either still needs the context.

All three read the **live** position, for the same reason `packState` and the
certification badge do: "is this pack still supported, and until when" is a question
about now, not about the run. The run record's `packDeprecations` snapshot (written at
launch) is the audit record beside them — and it lists every pack it *evaluated*, so a
clean run can prove it checked rather than merely not mentioning it.

## 9.3 Date and replacement, stated even when absent

AC1 names two things the notice must carry: the date it stops being supported, and
what replaces it. Both are surfaced explicitly — including when they do not exist:

* no end date → "No removal date has been announced." (never "Supported until "
  trailing into nothing);
* no replacement → "No replacement pack has been named."

On findings the two fields are **omitted rather than empty** when undeclared, so a
consumer cannot render a half-sentence from a blank string.

## 9.4 Amber, never red — and never "unhealthy"

A deprecated pack in grace *works*. It runs exactly as it did before the notice, which
is the entire point of a grace period. So the notice is amber (advance warning), never
red (fault), and a deprecated pack does **not** make the run-health packs panel read
unhealthy — the same rule 2.0-C1 T5 established for a disabled pack, for the same
reason: this is intentional lifecycle, not a failure.

Deprecation is also a fourth **orthogonal** fact beside pack state, version, and
certification. A pack can be active, current, certified, and deprecated at once, so it
gets its own pill rather than being folded into the state word. Tests pin that
`packLifecycleLabel` is unchanged by any deprecation field.

## 9.5 Fail-soft, in one direction

Every surface degrades to **no notice**, never to an invented one — a resolution
failure must not tell a customer their pack is being retired when it is not. A pack
the registry no longer declares reports nothing either: `get_pack()` resolves an
unknown id to the default pack, which would attribute *its* notice to a pack that is
gone (the same trap `_pack_certification` documents).

The launch snapshot is fail-soft too: a deprecation notice is a label, and failing to
resolve one must never fail a launch.

## 9.6 Contract

Contract **v1.21**. All additive; no pack ships a deprecation today, so every new
field is absent or null on current responses.

## 9.7 Tests

* `backend/tests/unit/test_pack_deprecation_surfacing.py` — DB-free; each surface
  separately, plus the one-notice-three-surfaces assertion and the fail-soft paths.
* `frontend/src/__tests__/PackDeprecationSurfacing.test.tsx` — the badge and detail
  components, the findings provenance row, the run-health lifecycle pills, and the
  API mapping.

---

# 10. Migration assist (T3 / AT-844)

## 10.1 Why notice is not enough

T1 lets a pack say it is superseded and name what replaces it. T2 shows that
everywhere the customer looks. Both are *information*, and the parent story is
explicit that a superseded pack must leave the customer with **a path** — "not a
broken configuration". Telling someone their configuration will stop working and
leaving them to rebuild it by hand is a slower version of the failure this whole
story exists to prevent.

So where a replacement is declared, the platform offers to make the change: rewrite
this org's saved run configuration so its pack and template selections point at the
replacement.

## 10.2 Three properties, in this order

| Step | What it guarantees |
|---|---|
| **Preview** (`preview_migration`) | The exact field-level change set, computed with **no writes**. |
| **Apply on confirmation** (`apply_migration`) | Explicit `confirm`, plus the plan's `fingerprint` — so what is applied is provably what was displayed. |
| **Revert** (`revert_migration`) | Every change records its **previous value**, so the configuration is restored verbatim. |

The fingerprint is what turns "previewed before applying" from a convention into a
property. It is a digest of the change set; if the configuration or the declaration
moved between preview and confirmation, the apply is **refused (409)** instead of
quietly applying a different change set. A caller that omits it is trusted, which is
deliberate — a CLI operator scripting a bulk migration has no screen to have seen.

**Revert restores, it does not invert.** Mapping the replacement back to the
deprecated pack would also drag back a selection that pointed at the replacement all
along. Restoring the recorded previous value is the only version of "reversible" that
is correct for a customer who was already partly migrated.

## 10.3 What is migrated

One surface: the org's saved Stack Builder setup state (`kv` row
`stack_builder_state:{org_id}`), and within it only the **selection** fields —
`packId`, `packIds`, `templateId`, `templateIds`.

`templateContributions` is deliberately **not** rewritten. It records which systems a
template contributed to *this* configuration; re-keying it onto the replacement
template would attribute one template's choices to another. That is inventing
provenance, not migrating a selection — so a remapped template that had contributions
raises a `template_contributions_need_review` warning and the customer decides.

Nothing else moves either. Run records, findings, and evidence keep the pack they
were produced with (2.0-C1 T4), and per-org lifecycle rows (disable, version pin) are
the *customer's* dimension: a stale pin on a pack that is no longer selected is inert,
and clearing it would erase a decision they made.

## 10.4 Template remapping never guesses

A template is registry-owned and declares its pack, so a template selection can only
move to another **registered** template that declares the replacement. The resolution
is the same discipline as `runtime_structure_resolution.py`:

| Candidates | Outcome |
|---|---|
| exactly one | remap |
| zero | left selected, reported as `no_replacement_template` |
| two or more | left selected, reported as `ambiguous_replacement_template`, naming them |

Force-picking one of several is how a migration quietly changes what a customer's runs
look for. The pack selection still migrates in every case, so the run uses the
replacement either way — what is withheld is only the guess.

Everything left alone is **named** in `unmapped[]`. A customer told "2 changes
applied" who is not told a template still points at the old pack has been given a
false picture of their own configuration.

## 10.5 Warnings, which never block

`warnings[]` states what is true about the destination before the customer commits:
the replacement is disabled for this org, or fails the 2.0-C1 compatibility gate, or
the grace period has already ended, or the declaration itself has defects. All are
advisory and all are fail-soft — a lifecycle or compatibility read that fails omits
its warning rather than refusing the migration. A missing advisory is a smaller harm
than denying a customer the path out of a deprecated pack.

## 10.6 Failure posture

Split the same way as `pack_state`: **reads answer, writes raise.**

"This pack is not deprecated" and "no replacement is declared" are answers a UI has
to explain, so `preview_migration` returns `available: false` with a reason (HTTP
200) rather than raising. An `apply` in that state *is* an error (409) — the caller
asked for something that cannot be done. An apply with nothing to change is a
**no-op**: no write, no ledger row, no audit event, `changed: false`, exactly like a
no-op pack-state transition, which is what makes re-applying safe.

An unknown pack id is a 404 rather than resolving to the default pack — the same
strictness as `pack_state`, and for the same reason: migrating `service_cloud`
because someone typo'd a pack id would be a serious foot-gun.

## 10.7 The ledger

Applies and reverts both **append** to `pack_migrations:{org_id}`. A revert never
edits or removes the row it undoes; "has this been reverted?" is derived from whether
a later revert references it. That is what lets AT-846 read "what did this org do,
and when" off one trail.

It lives in `kv` rather than a new table on purpose. `kv` already holds the setup
state being migrated, and it is in `history_retention.PROTECTED_TABLES` — so the
ledger inherits the never-delete guarantee (2.0-C1 T4) without a schema migration.

## 10.8 Roles, and where the gate sits

| Operation | Role | Why |
|---|---|---|
| Preview | analyst+ | It quotes the org's saved configuration back. The *notice* stays viewer+ — anyone who can select a pack must be able to see it is going away. |
| Apply / revert | **owner** | It rewrites what every future run for the whole organisation is built from — the same bar as disabling a pack or connecting a connector. |
| History | analyst+ | Same reasoning as the preview. |

Both writes emit an audit event (`pack_migration_applied` /
`pack_migration_reverted`) and telemetry (`pack.migration_applied` /
`pack.migration_reverted`), carrying field NAMES and counts only — the values live on
the ledger, which is the domain record. This discharges this transition's share of
AC4; AT-846 owns the consolidated audit view.

## 10.9 Contract

Contract **v1.22**. Four new routes; no existing response shape changes.

## 10.10 Tests

* `backend/tests/unit/test_pack_migration.py` — DB-free: preview/apply/revert,
  idempotence, the fingerprint guard, conservative template resolution, org
  isolation, and the structural no-delete-path check.
* `backend/tests/contract/test_pack_migration_api.py` — over HTTP: the role
  boundary, the status codes a UI branches on, the audit entries, and the end-to-end
  property that the migrated configuration is what the setup-state endpoint serves.
* `frontend/src/__tests__/PackMigrationAssist.test.tsx` — the preview → confirm →
  undo flow, the fingerprint round-trip, and the render-nothing paths.
