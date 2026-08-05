# Pack packaging & installation (2.0-C3 T4 / AT-839)

How an authored pack becomes a distributable artifact, and how that artifact gets
into a customer deployment. Read this before changing the bundle format, the
install gates, or anything that writes to `installed_packs`.

Implementation: `backend/discovery/packs/sdk/bundle.py` (format, signing,
verification), `backend/app/pack_installation.py` (the gates + the registry),
`backend/app/routes_pack_install.py` (the API), `database/models/installed_packs.py`
+ migration `0036`. CLI: `pack_sdk package` / `pack_sdk verify`. Tests:
`tests/unit/test_pack_bundle_packaging.py`, `tests/unit/test_pack_installation.py`,
`tests/contract/test_pack_install_api.py`.

---

## 1. The bundle

```
my_pack.aiqpack            (a zip)
  pack.json                the manifest
  fixtures/*.json          the author's test cases
  README.md                docs (CHANGELOG.md / LICENSE also allowed)
  BUNDLE.json              the digest index — every file, its sha256, its size
  BUNDLE.sig               {keyId, algorithm, value} over BUNDLE.json's bytes
```

**Why a digest index and a signature, rather than signing the archive.** Signing
the archive would tie the signature to zip encoding — compression level, entry
order, extra fields — none of which is content. Signing a canonical index means
the signature covers exactly the *content*, so a bundle repacked by a mirror still
verifies while any change to what is inside does not.

**Determinism.** Sorted entries, fixed timestamps, fixed compression, and no build
time in the index. Building the same project twice produces byte-identical bytes,
so a rebuild is itself a verification.

### Tampering, and the four shapes it takes

Each is a separate check, because a format that catches only the obvious one is
not integrity-protected:

| Attack | Caught by |
|--------|-----------|
| Edit a file | digest comparison → `bundle_content_mismatch` |
| Add a file the index does not cover | index-vs-archive set comparison → `bundle_unexpected_file` |
| Remove an indexed file | digest comparison → `bundle_content_mismatch` |
| Re-index and re-sign with another key | trust check → `bundle_signature_untrusted` |
| Strip the signature | → `bundle_signature_missing` |

Plus the archive-level defences that run before any content is read: size and file
count caps checked from the zip header (a decompression bomb must be a refusal,
not a disk-full outage), and a zip-slip guard on member paths. Any read failure at
all — truncated archive, corrupt deflate stream, unreadable path — becomes
`bundle_unreadable` rather than an exception escaping into the caller.

### Trust: the default is to trust nobody

There are **no built-in publisher keys.** A deployment lists the publishers it
trusts in `PACK_BUNDLE_TRUSTED_KEYS` (public halves only, `{"keyId": "<base64>"}`);
with none set, no bundle verifies and no partner pack installs. Installing
third-party content is a deliberate act of trust, and a platform shipping a
convenient default anchor would be making that decision for the customer.

This deliberately uses a **separate** anchor set from `pack_certification`: a
certification key attests *we reviewed this pack*, a publisher key attests *this
artifact is what I built*. One key doing both jobs would let either claim be
mistaken for the other.

`PACK_BUNDLE_SIGNING_KEY` (a base64 32-byte ed25519 seed) is packaging-tooling
input only — never read by the running platform, never in a repo, an `.env`, or a
deployment.

---

## 2. The install pipeline

Five gates, in this order, each returning a **named** refusal:

1. **Signature** — verified before any byte is written anywhere. Extraction is
   what puts partner-supplied content on disk, so nothing precedes verification.
2. **Validation** — manifest schema + the author's fixtures + lint, through the
   *same* `check_pack_directory` the author ran locally. One code path means "it
   passed on my machine" and "it passed on install" cannot diverge. This is also
   the hook sandbox validation builds on.
3. **Compatibility (2.0-C1)** — the manifest's declared range and required
   concepts, judged by the shared `check_declaration_compatibility` rule the
   runner enforces. That function was *extracted* from `check_pack_compatibility`
   for this task rather than copied: an installed pack held to a parallel
   implementation would drift from the one the runner uses.
4. **Certification policy (2.0-C2)** — an authored pack is Community, so an org
   with a Certified-only floor refuses it, naming the floor. **Fail-closed**: a
   policy that cannot be read refuses (503), matching the policy module's own
   posture. A control that fails open lifts the restriction exactly when it
   matters.
5. **Persist** — and only then.

A refused install leaves nothing behind: extraction happens in a temporary
directory removed on the way out, whatever the outcome.

### Installing is not activating

Installation records a pack; activation is a separate decision, and the two gates
that can move underneath an installed pack — the platform version and the org's
certification floor — are **re-checked at activation** rather than trusted from
install time. A pack that was installable last month is not automatically
activatable today; assuming otherwise is how a Certified-only deployment quietly
ends up running a Community pack.

Withdrawal (`active: false`) runs no gates — taking a pack *out* of service must
never be blocked — and never deletes.

---

## 3. The registry

`installed_packs` (migration 0036, mirrored into `provision.sql`): one row per
`(org, pack)`, re-install is an upgrade that bumps `revision`.

**No delete path.** Withdrawal writes `status = 'inactive'`; the manifest and the
bundle provenance (digest + publisher key id) stay, so "which pack produced this
historical finding, and where did that pack come from" survives the pack leaving
service — the 2.0-C1 never-delete discipline applied to the installed registry.

The table is deliberately **not** in `history_retention.PROTECTED_TABLES`: it holds
current configuration, not a record of what the platform found. Findings, evidence,
and run records are what that set protects, and none of them live here.

`installed_pack_config(org, pack_id)` is the seam a runner integration reads — the
manifest projected into the registry-shaped config (`manifest_to_pack_config`), so
an authored pack rides the existing lifecycle paths rather than a parallel one.

---

## 4. API

| Route | Role | Notes |
|-------|------|-------|
| `POST /api/packs/install` | owner | `{bundleBase64, activate?}` → 201 |
| `GET /api/packs/installed` | viewer+ | a viewer seeing a partner-pack finding must be able to see which pack and who published it |
| `PUT /api/packs/installed/{packId}/activation` | owner | `{active}` — the target state, so it is idempotent |

Install and activate are **owner**: an authored pack changes what every future run
for the whole org produces, and it introduces third-party content into the
deployment — the same bar as connecting a connector, higher in consequence.

Refusals are 409 with the gate named, except 503 for an unreadable policy (see
above), 404 for activating a pack that is not installed, 400 for a non-base64
body, and 413 for an oversized one. Contract v1.21 documents the shapes.

Audit: `pack_installed` and `pack_activation_changed`, both carrying the actor and
the bundle provenance. They are deliberately distinct from `pack_state_changed` —
collapsing them would make "a partner pack went live here" and "a first-party pack
was re-enabled" indistinguishable in the trail. Telemetry mirrors them with
`pack.installed`, `pack.activation_changed`, and `pack.install_refused` (which
gate refused, and how many failures — never the failure text, which can quote
partner-supplied content).

---

## 5. Scope of this task

AT-839 owns the bundle format, signing/verification, the install and activation
gates, the registry, and the API. Sandbox validation (AT-841) layers on the same
`check_pack_directory` call this pipeline already makes; wiring an active installed
pack into the discovery runner's execution path is a separate integration that
reads `installed_pack_config`.
