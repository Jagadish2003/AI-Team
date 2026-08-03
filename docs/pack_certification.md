# Pack Certification Levels — 2.0-C2

Read this before adding a pack, changing certification metadata, rotating the signing
key, or writing any code that reads a pack's certification level.

This document currently covers **T1 (AT-831) — certification metadata**,
**T2 (AT-832) — the internal review workflow**, and **T3 (AT-833) — surfacing**.
Org policy control (AT-834) and date-based expiry (AT-835) are separate tasks that
build on what is described here; sections will be added as they land.

---

## 1. What a certification is

Three levels, ordered:

| Level | Label | Meaning |
|---|---|---|
| `certified` | **CloudFulcrum Certified** | Authored and reviewed by CloudFulcrum. |
| `partner` | **Partner** | Authored by a partner, reviewed and signed by CloudFulcrum. |
| `community` | **Community** | Self-declared. Nobody has vouched for it. |

`community` is deliberately the **un-signed** level. It is the honest label for "nobody
has reviewed this", so requiring proof to claim it would be backwards — a community
pack self-declares, and that *is* the badge.

## 2. Why a signature

Certification is a claim about *who reviewed this pack*. With partner-authored packs
coming (2.0-C3), the manifest is supplied by the party being vouched for — so an
unsigned `"level": "certified"` string is worth exactly nothing, because the author
would be certifying themselves.

The metadata is therefore signed with an Ed25519 key whose **private half never
ships**, and the platform verifies against the **public half** that does. That is the
whole mechanism behind 2.0-C2 AC1:

> Certification metadata is signature-verified; a pack claiming Certified without a
> valid signature is treated as Community.

Note what a failed verification does *not* do: it does not erase the claim. The
declared level is preserved and reported alongside the effective one, because
"claims Certified, could not be verified" is far more useful to a reviewer — and to a
security team reading an export — than a silently rewritten field.

## 3. What is declared

Each pack declares a `certification` block in `PACK_REGISTRY`
(`discovery/packs/pack_config.py`), read through
`get_pack_certification_declaration()`, which always returns a complete normalised
block:

```python
"certification": {
    "level":                          "certified",
    "certifyingEntity":               "CloudFulcrum",
    "reviewDate":                     "2026-07-31",
    "reviewedAgainstPlatformVersion": "2.0.0",
    "scope": {
        "summary":  "Cloud-operations detectors, the MSP-B6 four-part finding "
                    "contract and causal gate, NOC terminology, and the "
                    "config-driven ops-impact scorer calibration.",
        "criteria": ["declarative_manifest_review", "evidence_discipline",
                     "terminology", "calibration_sanity", "aggregation_floor"],
    },
    "signature": {
        "keyId":     "cloudfulcrum-pack-signing-2026",
        "algorithm": "ed25519",
        "value":     "<base64 signature over the canonical payload>",
    },
}
```

A pack that declares **nothing** reads as `community` with empty metadata — the
absence of a declaration is never an error, it is an accurate statement.

Normalisation is conservative on purpose, because its output is the input to the
signed payload: strings are stripped, the level is lower-cased, `scope.criteria` is
de-duplicated order-preservingly, and **nothing is invented or defaulted into a signed
field**. A signature can never cover something the pack did not actually declare.

## 4. What is signed — and what deliberately is not

The signature covers the canonical JSON (sorted keys, no insignificant whitespace,
UTF-8) of `certification_payload()`:

```
payloadVersion, packId, level, certifyingEntity, reviewDate,
reviewedAgainstPlatformVersion, scope{summary, criteria}
```

Every reader-facing field is inside it, so none can be edited after issuance without
invalidating the badge — including `packId`, so a valid signature cannot be copied
from one pack onto another.

**`packVersion` is deliberately NOT signed.** A certification is a statement about a
reviewed pack and its criteria; binding it to a version string would invalidate every
signature on a patch bump, turning routine maintenance into a re-issuance ceremony —
which in practice trains people to disable the check. Version-scoped review lands
where it belongs, on the review date and the reviewed-against platform version
(§6).

`payloadVersion` (`agentiq-pack-certification-v1`) is inside the signature so a future
payload shape cannot be replayed against this one. Bump it alongside any payload-shape
change, and re-issue.

## 5. Verification — fail closed, every time

`get_pack_certification(pack_id)` returns a `PackCertification` and **never raises**;
the verdict is the return value. Every failure path downgrades to `community` with a
distinct, named reason:

| Reason | When |
|---|---|
| `invalid_level` | Declared level is not one of the three. |
| `missing_certification_metadata` | A signature-required level omitted entity / review date / reviewed-against version — or claimed `certified` without naming CloudFulcrum as the certifying entity. |
| `signature_missing` | Certified/Partner claimed with no signature at all — the self-applied case. |
| `signature_malformed` | Signature or key material is not valid base64 / not a usable key. |
| `signature_unknown_key` | Signed by a key this platform does not trust. |
| `signature_unsupported_algorithm` | Anything other than `ed25519`. |
| `signature_invalid` | Trusted key, right algorithm, and the signature does not match the metadata. |
| `signature_backend_unavailable` | No crypto backend importable. |

That last one matters: an environment that *cannot* verify has not verified. There is
no path where an unverifiable claim keeps its badge.

## 6. Review due

A certification records the platform capability version it was reviewed against.
`PackCertification.review_due` reports when the running platform has moved past it at
**MAJOR.MINOR** granularity.

The badge is **flagged, not revoked** — the signature is still valid and the level is
still reported; the pack is additionally marked as due for review, rather than
silently retaining a badge earned against a platform that no longer exists (2.0-C2's
fifth deliverable). Patch-level platform movement does not trigger it: a patch does
not change the capability surface a pack was reviewed against, and flagging it would
make the signal noise, which trains reviewers to ignore it.

A `community` pack is never "review due" — it was never reviewed, and saying otherwise
would imply a badge it does not have. A pack whose claim was *downgraded* is community
for the same reason.

Date-based expiry is 2.0-C2 T5's concern; this layer supplies the `reviewDate` it will
read.

## 7. Trust anchors and rotation

`CLOUDFULCRUM_SIGNING_KEYS` in `discovery/packs/pack_certification.py` holds the
trusted **public** keys, `{keyId: base64 raw ed25519 public key}`. A deployment may
add anchors via `PACK_CERTIFICATION_TRUSTED_KEYS` (a JSON object of the same shape) —
public keys only, never a credential. A built-in key id present in that map is
**ignored with a warning**: an operator may add trust, never silently substitute
CloudFulcrum's, because that would make the badge meaningless.

Rotation is additive:

1. `python scripts/sign_pack_certifications.py --generate-key` → store the private
   seed in secrets management; add the public key under a **new** key id.
2. Re-issue every signature with the new key
   (`PACK_CERTIFICATION_SIGNING_KEY=<seed> ... --sign`) and paste the values into the
   registry.
3. Retire the old key id in a later release, once nothing references it.

**The private key is never in this repository, in a `.env`, or in a deployment.**
Signing lives in `backend/scripts/sign_pack_certifications.py` — release tooling, not
part of the running application. The application imports only the verification path.

## 8. Changing certification metadata

Editing any signed field invalidates the signature, and the pack immediately reads as
Community — loudly, with a `WARNING` and a named reason, and the structural test in
§9 fails the build. That is the intended behaviour, not an obstacle: metadata that
changed after review has, by definition, not been reviewed.

So the workflow for a metadata change is: change it, have it reviewed, re-issue the
signature with the release key, and commit both in one diff.

```bash
cd backend
python scripts/sign_pack_certifications.py --show cloud_ops   # what would be signed
PACK_CERTIFICATION_SIGNING_KEY=<base64 seed> \
    python scripts/sign_pack_certifications.py --sign         # packId -> signature
python scripts/sign_pack_certifications.py --check            # verify the result
```

`--sign` prints signatures rather than rewriting source: a signature landing in the
tree should be a reviewed diff, not a side effect of running a script.

## 9. Tests

[`backend/discovery/tests/test_pack_certification.py`](../backend/discovery/tests/test_pack_certification.py)
(43 tests), pure-Python and offline. Two halves:

* **Structural** — every registered pack declares a complete certification block,
  every shipped signature verifies against the shipped anchor, every certified pack
  names CloudFulcrum, and no shipped pack is review-due on the current
  `PLATFORM_VERSION`. These fail the build if a future pack ships an unsigned or
  edited claim, instead of letting the downgrade surface in front of a customer.
* **Behavioural** — AC1 walked from both ends: a genuine badge survives, and each of
  the failure modes in §5 downgrades to Community with its named reason. Editing any
  signed field (including the scope, and including copying a valid signature onto
  another pack) invalidates it. The signing tests mint an **ephemeral** key pair, so
  CI never needs the release private key.

---

# 10. The review workflow (T2 / AT-832)

## 10.1 What a review is, and what it is not

A review is the **record of a human decision**: who reviewed which pack version,
against which criteria, how each criterion came out, on what date, against which
platform version, and whether the outcome was approve or reject. That record is what
2.0-C2 AC5 requires — *every certification decision is recorded with reviewer,
criteria, and date, and is auditable*.

A review does **not** grant a badge, and this is the single most important thing to
understand about the two tasks together. If recording a review changed a pack's
level, the reviewer's database row would become the trust root instead of the signing
key — which is exactly the self-application hole §2 exists to close. So:

```
review (in-app, this workflow)   →  decision + criteria verdicts, recorded
        ↓  approved_declaration()
canonical payload                →  signed OFFLINE with the release key
        ↓
certification metadata (§3)      →  verified at runtime (§5)
```

`PackCertificationReview.approved_declaration()` emits the exact declaration block —
level, certifying entity, review date, reviewed-against platform version, and a scope
whose `criteria` are precisely the criteria that **passed** — and `canonical_payload()`
emits the exact bytes to sign. What gets signed is therefore provably what was
approved, rather than something retyped afterwards.

A rejected review has no declaration at all: asking for one raises, and the serialised
record omits both signing fields, so a consumer cannot accidentally build a payload
out of a rejection.

## 10.2 The checklist

`discovery/packs/certification_criteria.py` holds one vocabulary read from both ends:
a review records a verdict per criterion, and an approved pack's signed
`scope.criteria` (§3) lists the ones that passed. One vocabulary means the two cannot
drift, and a structural test pins that every id a shipped pack claims really exists.

The four the story names are **required** — `declarative_manifest_review`,
`evidence_discipline`, `terminology`, `calibration_sanity`. Two more ship as optional
because they are genuinely pack-specific: `compliance_guardrails` (nCino/STRS/GitHub)
and `aggregation_floor` (security_ops). A pack with no security-derived content should
not have to fake a verdict on an aggregation floor.

Mark a new criterion `required=True` only if EVERY pack must be judged on it — a
required criterion with no verdict blocks approval.

## 10.3 The gate

A review cannot be recorded as `approved` unless every required criterion carries a
**passing** verdict. Missing and failed are reported separately, because they are
different mistakes: one is an incomplete review, the other is a review that found a
problem. An approval that skipped an item is not a lighter-weight approval — it is an
unreviewed pack wearing a badge.

`not_applicable` is allowed but must carry a note, and never satisfies a *required*
criterion. A rejection must carry notes: "rejected, no reason recorded" is not
auditable and leaves the pack author nothing to act on.

## 10.4 Append-only, and protected

There is no update and no delete path in `app/pack_certification_review.py`.
Re-reviewing writes the NEXT revision; a superseded review stays on the trail. That is
what makes it an audit trail rather than a current-state mirror.

`pack_certification_reviews` is therefore in
`app/history_retention.PROTECTED_TABLES`, so migration `0034` REVOKEs
`DELETE, TRUNCATE` on it exactly as 2.0-C1 T4 does for run history — a certification
decision that can be deleted is not auditable. Note the ordering inside `0034`: the
table is created first, then the REVOKE block is re-applied, because `0033` has
already run on existing deployments and a table created afterwards would otherwise sit
under `GRANT ALL PRIVILEGES` with no data-layer enforcement at all.

## 10.5 Attribution

Reviewer, pack version, platform version, and date are all recorded from the
**server's** view. None is accepted from the request body — a trail the caller can
back-date or attribute to somebody else is not an audit trail. The pack id is
validated strictly (`PackNotFound`) rather than resolved through `get_pack()`'s
default-pack fallback: a typo must never put a real reviewer's name against a pack
they never looked at.

Reviews are org-scoped like every other write here. Certification review is an
internal CloudFulcrum activity performed in CloudFulcrum's own workspace; scoping it
keeps the tenancy invariant and isolates a partner or federal deployment reviewing
packs locally.

## 10.6 API and audit

| Route | Role | Purpose |
|---|---|---|
| `GET /api/packs/certification/criteria` | viewer+ | The checklist. |
| `POST /api/packs/{packId}/certification/reviews` | **owner** | Record a review (201). |
| `GET /api/packs/{packId}/certification/reviews` | analyst+ | Newest-first trail + the live badge. |

Viewer can read the checklist because someone looking at a Certified badge must be
able to see what was checked, otherwise the badge is an unfalsifiable claim. Recording
is owner: it puts a named person's decision on a permanent trail.

The trail endpoint returns the live `certification` block (§5) **alongside** the
reviews, so an approved-but-unsigned pack reads as "approved on 2026-07-31 / effective
level: Community" rather than as Certified.

Every recorded review emits the `pack_certification_reviewed` audit event (actor, org,
pack, version, decision, criteria) and the `pack.certification_reviewed` telemetry
event. Free-text notes stay in the domain record and never reach telemetry.

Contract: `contracts/API_CONTRACT.md` **v1.16 → v1.17**. Entirely new routes; no
existing response shape changes.

## 10.7 Tests

* [`backend/tests/unit/test_pack_certification_review.py`](../backend/tests/unit/test_pack_certification_review.py)
  (43 tests, DB-free) — the checklist vocabulary, the gate from both sides, the
  append-only trail, org scoping, the review→signature bridge (including an
  end-to-end sign-and-verify against §5), and the auditability properties: the table
  is protected, the store contract exposes no delete/update, and the module contains
  no destructive SQL.
* [`backend/tests/contract/test_pack_certification_review_api.py`](../backend/tests/contract/test_pack_certification_review_api.py)
  (24 tests) — the HTTP surface: RBAC, org isolation, server-side attribution,
  400/409/404 boundaries, newest-first append-only ordering, the audit-log entry, and
  the load-bearing negative — recording an approval does **not** change the effective
  level.

---

# 11. Surfacing (T3 / AT-833)

## 11.1 The one rule

2.0-C2 AC2 asks for the level *at selection, at activation, on findings, and in
exports*. Four surfaces, one rule:

> **Every surface displays the EFFECTIVE, signature-verified level.**

A pack claiming Certified whose signature does not verify reads as **Community** at
selection, at activation, on its findings, and in an export — all at once. That is
§2/§5 carried through to the UI, and it is what makes the badge worth anything: if
one surface rendered the *declared* level, a pack author would only need to find
that surface.

So the backend never ships a bare claim to a renderer. `certification_badge()` is a
five-field projection — `level` (effective), `label`, `statusLabel`, `declaredLevel`,
`reviewDue` — and every consumer renders `level`. `declaredLevel` is carried for
diagnosis ("claims Certified, could not be verified"), never for display. The React
component takes `level` and has no code path that could render `declaredLevel`; a
test pins that an unrecognised level renders **nothing** rather than falling back to
something reassuring.

## 11.2 The four surfaces

| Surface | Where | Shape |
|---|---|---|
| **Selection** | `GET /api/packs/state`; Discovery Plan pack picker | `certification` on each `PackStateItem` |
| **Activation** | run record + `pack_certifications` KV at launch; run-health packs panel | `packCertifications` (snapshot); `certification_level`/`_label`/`_review_due` (live) |
| **Findings** | every opportunity serve site | `packCertificationLevel` / `packCertificationLabel` / `packCertificationReviewDue` |
| **Exports** | executive report artifact + the PDF | `packCertifications[]` |

Findings are stamped in `opportunity_display`'s shared display funnel — the same
funnel `packState` uses — so one wiring covers list, decision, override, roadmap,
executive report, and blueprint. Badges are resolved **once per list** and threaded
down: a 200-finding response costs one verification pass, not 200. A test pins the
call count, because that is the kind of regression that is invisible until a large
run is slow.

## 11.3 Live level, snapshotted record

The two are deliberately different, and the split is the interesting decision:

* **Displayed everywhere: the LIVE level.** A badge is a statement about the pack,
  not a property frozen into a historical finding. If a signature stops verifying —
  metadata edited, key rotated out, trust anchor removed — the pack must stop reading
  as Certified *everywhere at the same moment*. Freezing the level onto findings
  would leave revoked badges scattered across historical output with no way to
  correct them.
* **Recorded at launch: `packCertifications`.** The run record and KV keep what each
  pack held when the run was launched, so an audit of an old run can say what was
  true then. It is an audit record, not a display source.

The executive report sits between the two by design: the level is resolved when the
report is **generated** and frozen into the artifact. That is the honest reading for
an export — a board paper states what was verifiable when it was produced, and a
document already printed cannot be retroactively corrected anyway.

## 11.4 Additive, and orthogonal to lifecycle

Certification is a third fact alongside 2.0-C1's state and version, not a
replacement for either. A pack can be disabled *and* certified — it was certified
when it produced the findings you are reading — so the finding row shows the pack id,
the version, the disabled label, and the certification badge together, and the
run-health panel gets a third pill rather than a merged one. The 2.0-C1 T5
`packLifecycleLabel()` helper is untouched, and a test pins that adding assurance did
not change state or version wording.

Every field is additive and optional. A pre-2.0-C2 response omits them; a finding
with no `packId` (pre-R16-B1) is returned unchanged rather than guessed at; and an
unresolvable badge is **absent**, never defaulted to Community — the backend decides
what Community means, and inventing it in a renderer would be a claim we cannot
support.

## 11.5 Fail-soft, in the safe direction

Every resolution path degrades to *no badge*: the pack picker still lists packs, the
findings still serve, the report still generates. Note the direction — the failure
mode is a missing badge, never an unverified claim rendered as Certified. The
activation snapshot is fail-soft for the same reason: a launch must not fail because
a label could not be resolved.

## 11.6 Contract

`contracts/API_CONTRACT.md` **v1.17 → v1.18**, per the repo rule that a
`frontend/src/types/*.ts` change requires a bump. Everything is additive:
`analystReview.ts`, `runHealth.ts`, `executiveReport.ts`, and the new shared
`packCertification.ts`.

## 11.7 Tests

* [`backend/tests/unit/test_pack_certification_surfacing.py`](../backend/tests/unit/test_pack_certification_surfacing.py)
  (27 tests) — each of the four surfaces, each tested twice: once with a genuine
  badge and once against a seeded **unsigned Certified claim** that must read as
  Community. Plus the additive/fail-soft properties and the resolve-once call count.
* [`frontend/src/__tests__/PackCertificationSurfacing.test.tsx`](../frontend/src/__tests__/PackCertificationSurfacing.test.tsx)
  (13 tests) — the shared badge component (including that an unknown level renders
  nothing), the finding provenance row, the disabled-AND-certified case, the
  run-health lifecycle regression, and the selection mapping.
