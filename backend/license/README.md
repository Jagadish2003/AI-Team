# LIC-1 — CloudFulcrum-internal licensing tooling

> **Internal only. Not shipped to customers.** This directory lives at
> `backend/license/`. It sits inside the Docker build context (`backend/`) but is
> excluded from the customer image by `backend/.dockerignore` (`license/`), so it
> is never copied into the image (AC10). Do not remove that .dockerignore entry.

This is the **issuing** side of the LIC-1 Offline License Key System. The
*shipped* side (offline verification) is `backend/app/licensing.py`.

## How issuing works (local signing)

CloudFulcrum holds the **private** Ed25519 key on a secured signing host. The
CLI loads that private key, signs the canonical payload, and prints the key
string the customer pastes into AgentIQ. Issuing is fully offline — no network
call, no online activation.

```
generate_keypair.py ──► cloudfulcrum_private.pem (secrets manager; git-ignored)
                   └──► public key  ──► CLOUDFULCRUM_PUBLIC_KEY in licensing.py (shipped)

inputs ──► generate_license.py --private-key … ──► sign(base64(payload))
                                              ──► "<payload_b64>.<sig_b64>"
```

### One-time: generate the keypair (T2)

```powershell
# run from the repo root
python backend/license/generate_keypair.py
# → writes the PRIVATE key to backend/license/cloudfulcrum_private.pem (git-ignored)
#   move it into the secrets manager; NEVER commit it
# → prints the PUBLIC key — paste it into CLOUDFULCRUM_PUBLIC_KEY in
#   backend/app/licensing.py and cut a release
```

### Issue a key

Issuance is **gated and logged** (R-1.9.1-L3): `--contract-ref`, `--org-id`, and
`--issued-by` are required, and every issue writes a row to the license registry
plus an entry to the append-only audit ledger. There is no ungated path — a key
cannot be minted without a registry record.

```powershell
# run from the repo root
python backend/license/generate_license.py `
  --customer 'City National Bank' `
  --org-id cnb `
  --contract-ref CTR-4471 `
  --issued-by ganesh `
  --license-id cnb-2026-001 `
  --term-months 12 `
  --grace-days 14 `
  --private-key backend/license/cloudfulcrum_private.pem
# prints the signed license key to stdout; "issued … (audit …)" to stderr
```

`--term-months` must be `3`, `6`, or `12`. The CLI computes `issued_at` (today)
and `expires_at` (today + term×30 days) and signs the canonical payload. The same
issue is available via the ops CLI: `license_ops.py issue …` (below).

## Vendor-side license operations (R-1.9.1-L3)

The license **registry** and **issuance service** are CloudFulcrum-internal
software that runs in the ops environment. They are the authoritative record of
every key we have minted — distinct from the customer-side installed-key store
(`app/license_runtime.py` → `org_licenses`), which is one downstream copy of a
single installed key at one customer.

- **Storage:** the ops PostgreSQL database, via the standard `DATABASE_URL`
  (the connection string in `backend/.env`). The schema — `license_registry` +
  the append-only `issuance_audit` ledger — is defined in
  `database/models/license_registry.py`, applied by alembic migration `0026`, and
  captured in `database/provision/provision.sql` (the psql-only Path B). Run
  `provision.sql` (or `alembic upgrade head`) before first use.
- **Issuance service** (`issuance.py`): the single path every write goes through.
  Refuses without `contract_ref`/`org_id`/`issued_by`; emits payload-v2 keys only;
  writes the registry row and the audit entry in one transaction.
- **Audit ledger** (`issuance_audit`): append-only, enforced at the schema level
  by Postgres rewrite rules (`ON UPDATE/DELETE DO INSTEAD NOTHING`) — no service
  path can alter or delete an entry.

### Ops CLI (`license_ops.py`)

```powershell
# renew (links via supersedes, inherits customer/org, flags term changes)
python backend/license/license_ops.py renew --supersedes cnb-2026-001 `
  --license-id cnb-2027-001 --issued-by ganesh --term-months 12

# proactive-renewal list: active licenses expiring within N days
python backend/license/license_ops.py expiring --days 30

# renewal lineage (the supersedes chain, oldest first)
python backend/license/license_ops.py lineage --license-id cnb-2027-001

# record the deployment fee for a license
python backend/license/license_ops.py fee --license-id cnb-2026-001

# list a customer's licenses
python backend/license/license_ops.py list --customer 'City National Bank'
```

### Deployment fee tracking

Each registry row carries `deployment_type` (from the signed payload) and a
`deployment_fee_collected` flag + date. Use `license_ops.py fee` to record
collection; `--uncollected` clears it.

### Verify a key offline (AC1 / AC2)

Confirm a key the API returned actually validates against the public key shipped
in the app, and that tampering is rejected:

```powershell
python backend/license/verify_license.py --key '<payload_b64>.<sig_b64>'
```

If this prints `INVALID` for a freshly issued key, the public key in
`backend/app/licensing.py` does **not** match the private key the API signs
with — stop and reconcile before shipping.

## Key custody (T2 / AT-343)

| Item | Where it lives | Who can access |
|------|----------------|----------------|
| **Private signing key** | CloudFulcrum secrets manager (generated by `generate_keypair.py`; git-ignored locally, never committed) | _<fill in: team + secrets-manager path>_ |
| **Public key** | `CLOUDFULCRUM_PUBLIC_KEY` constant in `backend/app/licensing.py` (safe to ship) | Public — published in the binary by design |

> Action for the ticket: record the exact secrets-manager location and the
> access list above (a reference/screenshot in the Jira ticket — **never** the
> key material itself).

## Key rotation runbook

The private key is the only genuinely sensitive secret in the system. If it is
ever compromised, **every issued key must be treated as forgeable.**

### Preferred: key-set (kid) rotation — config only, no release (R-1.9.1-L1 / T3)

The trusted public keys are a **keyed set**: a payload v2 license carries a `kid`
(key identifier) and verification selects the trusted public key by it. Rotating
the signing key is a config change on the deployment, not a binary release:

1. Generate a new Ed25519 keypair on the secured signing host, under a NEW kid
   (e.g. `cf-2027-2`).
2. Store the new private key in the secrets manager (keep the old one until every
   license issued under its kid is re-issued).
3. Add the new kid's **public** key to the deployment's `LICENSE_TRUSTED_KEYS`
   JSON (`{"cf-2026-1": "...", "cf-2027-2": "..."}`) — both kids are now trusted,
   so in-field licenses keep verifying.
4. Issue new licenses under the new kid (`generate_license.py --kid cf-2027-2`).
5. Once no active license references the old kid, drop it from
   `LICENSE_TRUSTED_KEYS`. A license under a retired/unknown kid then fails as
   `invalid: unknown_key`.
6. Record the rotation (date, reason, who) in the ticket / security log.

A license under a kid not in the trusted set is `invalid: unknown_key` (distinct
from `signature_or_format`), so an operator can tell "trust/rotate this signing
key" from "this key is corrupt or forged".

> **Honest limitation — no remote revocation (by design).** There is no
> phone-home and no online activation, so CloudFulcrum cannot remotely revoke a
> key that is already in the field. Retiring a `kid` is the mechanism: once a
> customer's `LICENSE_TRUSTED_KEYS` config drops the old `kid`, licenses signed by
> it stop verifying (`invalid: unknown_key`). That requires a customer-side config
> update. The registry marks a license `revoked_at_next_rotation` to record intent,
> but enforcement happens only when the customer's trusted set no longer includes
> the `kid`.

> **Parallel kids during a rotation window (AC6).** Because `kid` is a per-issue
> parameter and `LICENSE_TRUSTED_KEYS` holds a set, two signing keys can be active
> at once: keep issuing under the old `kid` while you start issuing under the new
> one, and the registry records which `kid` signed each license.

### Signing-key custody at issuance (AC5)

The issuance service reads the private key from a **filesystem path only** — never
from the repository and never as key material in an environment variable:

1. an explicit `--private-key <path>`, else
2. `LICENSE_SIGNING_KEY_PATH` — point this at the key mounted from the managed
   secrets store in the ops environment, else
3. the git-ignored dev default under `backend/license/` (local development only).

A CI/repo scan (`test_r191_l3_acceptance.py`) fails the build if any `*.pem` /
`*.key` private-key material is committed.

### Last resort: replace the baked-in root of trust (needs a release)

Use only if the config path is unavailable (e.g. the baked-in default key itself
is compromised and no `LICENSE_TRUSTED_KEYS` override is deployed):

1. Generate a new Ed25519 keypair on the secured signing host.
2. Store the new private key in the secrets manager; revoke/retire the old one.
3. Replace `CLOUDFULCRUM_PUBLIC_KEY` in `backend/app/licensing.py` (registered
   under `DEFAULT_KID`) with the new public key.
4. Cut a new app release so customers receive the new public key.
5. Re-issue active customer licenses with the new key.
6. Record the rotation (date, reason, who) in the ticket / security log.

## Security rules

- Never commit a private key, `*.pem`, or `*.key` (enforced by `.gitignore`).
- The private key never leaves the secured signing host / secrets manager.
- The shipped app only ever *verifies* offline against the public key — it never
  signs and never reaches CloudFulcrum.
- Run `git diff --staged` before every commit on licensing changes and confirm
  only the public constant / tooling is present (no `*.pem`).
