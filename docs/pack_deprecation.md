# Pack Deprecation & Migration — 2.0-C4

Read this before deprecating a pack, changing a deprecation declaration, or writing
any code that reads a pack's deprecation state.

This document currently covers **T1 (AT-842) — deprecation metadata**. Notice
surfacing (AT-843), migration assist (AT-844), grace behaviour (AT-845), and the
audit events (AT-846) are separate sub-tasks that layer on what this one reports; each
adds its own section here as it lands.

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
