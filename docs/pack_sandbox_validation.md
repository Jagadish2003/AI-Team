# Sandbox validation (2.0-C3 T6 / AT-841)

What runs before an authored pack is allowed to execute, and why it runs twice.
Read this before changing a sandbox limit, the activation gates, or anything that
writes `installed_packs.fixtures` / `.validation`.

Implementation: `backend/app/pack_sandbox.py` (limits, stages, the report),
`backend/app/pack_installation.py` (the gates), migration `0037`. Tests:
`backend/tests/unit/test_pack_sandbox_validation.py`,
`backend/tests/contract/test_pack_install_api.py`.

---

## 1. The requirement, and the three words that matter

> Installing a pack runs its manifest through validation and its fixtures through
> the harness **before activation**; failures block activation with specific
> reasons.

**"its fixtures."** Validation is not only a schema check. A manifest can be
perfectly well-formed and still declare detectors that find nothing, or emit
findings missing a contract part. Running the author's own cases is what turns
"this document parses" into "this pack does what its author claims" — and it is
the only evidence the platform has about a pack it did not write.

**"before activation."** AT-839 already validated at install. But installing is
not activating, and the two can be months apart, during which the platform moves:
a concept can be withdrawn, a primitive's contract can change, a lint floor can be
raised. So activation re-runs the whole check rather than trusting the install-time
verdict. That is why this task persists the fixtures at all — **a pack you cannot
re-validate is a pack you have to take on trust at exactly the moment it starts
executing.**

**"sandbox."** The harness runs partner-supplied *data* through platform code,
inside the API process, on a request.

## 2. What the sandbox actually is

It is **not** an OS-level sandbox, and it does not need to be: no partner code
executes (2.0-C3's governing constraint), so there is nothing to isolate *from*.
What there is, is untrusted input sizing a trusted computation. A fixture with
fifty thousand records over a depth-3 traversal is a CPU and memory spike however
declarative the manifest is.

So the run is bounded, and a breach is a **named refusal** rather than a slow
request nobody can explain:

| Limit | Default | Env |
|---|---|---|
| cases per pack | 50 | `PACK_SANDBOX_MAX_CASES` |
| records per case | 2,000 | `PACK_SANDBOX_MAX_RECORDS_PER_CASE` |
| records per suite | 20,000 | `PACK_SANDBOX_MAX_TOTAL_RECORDS` |
| fixture bytes | 4 MiB | `PACK_SANDBOX_MAX_FIXTURE_BYTES` |
| time budget | 30s | `PACK_SANDBOX_TIMEOUT_SECONDS` |

Two deliberate choices:

* **A bad value falls back to the default and logs; it never removes the bound.**
  `0` does not mean "unlimited" here, unlike some env flags elsewhere in this
  repo — a bound an operator can delete with a typo is not a bound. Removing one
  is a code change.
* **The time budget is checked between cases**, not enforced by a kill. Python
  does not offer one, and claiming otherwise would be worse than saying so. Work
  *inside* a case is bounded by the per-case record cap; the budget bounds the
  suite. The check lives in `harness.run_cases` because that loop is the only
  place that can make it — a bounded caller re-implementing the loop is exactly
  the drift the toolkit exists to prevent.

## 3. Stages, and why the refusal names one

`SandboxReport.stage` is `admission` → `validation` → `fixtures` → `lint` →
`passed`. It maps to two different refusal reasons, and the split is the point:

* `admission` → **`sandbox_limit_exceeded`** — *too expensive to judge.*
* anything else → **`validation_failed`** — *judged, and found wrong.*

Those need different actions from an author: shrink the suite, versus fix the
pack. Collapsing them into one reason would send half of them down the wrong
path. Every failure is reported, not the first — an author who shrinks their
suite once per rejection submits five times.

The verdict is **data, never control flow**: `run_sandbox_validation` does not
raise, in the posture of `PackCheckReport` and `BundleVerification`. The caller
decides what a failure means, and at install and activation it means a refusal.

## 4. Where it runs

Install (`install_pack_bundle`), as gate 2, over the extracted bundle — and the
fixtures are read out before the temporary directory goes.

Activation (`set_installed_pack_activation(active=True)`), before compatibility
and the certification floor, from the **stored** manifest and the **stored**
fixtures. The verdict is persisted whether it passed or failed, because an
operator looking at a pack that will not activate needs the reasons without
re-uploading the bundle (`GET /api/packs/installed/{packId}/validation`).

Withdrawal (`active: false`) runs **no** gates. Taking a pack out of service must
never be blocked by the pack's own condition.

A persistence failure on the verdict is logged and swallowed: the verdict is what
the caller acts on, and losing its audit copy must not turn a passing pack into a
refused one.

## 5. The pre-AT-841 row

A record written before migration 0037 has no stored fixtures. Re-validation then
runs the manifest and lint alone and **records a note saying so** — it neither
fails the pack (which would break existing installs for no security gain; those
fixtures did pass at install) nor reports a full pass it did not perform. The
next re-install repopulates them.

## 6. Scope

AT-841 owns the sandbox limits, the stage model, the persisted fixtures and
verdict, the activation-time re-run, and the read endpoint. It changes no
detector, primitive, lint, or bundle behaviour; the one change outside this
module is an optional `deadline` on `harness.run_cases` and its `toolkit`
passthrough.
