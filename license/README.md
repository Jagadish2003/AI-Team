# LIC-1 — CloudFulcrum-internal licensing tooling

> **Internal only. Not shipped to customers.** This directory lives at the repo
> root, outside the backend Docker build context (`backend/`), so it is never
> copied into the customer image (AC10). Do not move it under `backend/`.

This is the **issuing** side of the LIC-1 Offline License Key System. The
*shipped* side (offline verification) is `backend/app/licensing.py`.

## How issuing works (API-based)

CloudFulcrum runs a remote signing API that holds the **private** Ed25519 key.
This tooling never holds or loads a private key — it calls the API and prints
the signed key string the customer pastes into AgentIQ.

```
inputs ──► generate_license.py ──► POST {customer, license_id, term_months,
                                         grace_days, limits} ──► issuing API
                                  ◄── { "license_key": "<payload>.<sig>" }
```

### Issue a key

```powershell
# Confidential values — set in license/.env or backend/.env (both untracked),
# or export in your shell. NEVER commit them.
#   LICENSE_API_URL=...
#   LICENSE_API_TOKEN=...

python license/generate_license.py `
  --customer 'City National Bank' `
  --license-id cnb-2026-001 `
  --term-months 12 `
  --grace-days 14
# prints the signed license key to stdout
```

`--term-months` must be `3`, `6`, or `12`. The API computes `issued_at` (today)
and `expires_at` (today + term×30 days) and signs the canonical payload.

### Verify a key offline (AC1 / AC2)

Confirm a key the API returned actually validates against the public key shipped
in the app, and that tampering is rejected:

```powershell
python license/verify_license.py --key '<payload_b64>.<sig_b64>'
```

If this prints `INVALID` for a freshly issued key, the public key in
`backend/app/licensing.py` does **not** match the private key the API signs
with — stop and reconcile before shipping.

## Key custody (T2 / AT-343)

| Item | Where it lives | Who can access |
|------|----------------|----------------|
| **Private signing key** | CloudFulcrum issuing API / secrets manager (server-side, never on a laptop or in this repo) | _<fill in: team + secrets-manager path>_ |
| **Public key** | `CLOUDFULCRUM_PUBLIC_KEY` constant in `backend/app/licensing.py` (safe to ship) | Public — published in the binary by design |

> Action for the ticket: record the exact secrets-manager location and the
> access list above (a reference/screenshot in the Jira ticket — **never** the
> key material itself).

## Key rotation runbook

The private key is the only genuinely sensitive secret in the system. If it is
ever compromised, **every issued key must be treated as forgeable.**

1. Generate a new Ed25519 keypair on the secured signing host.
2. Store the new private key in the secrets manager; revoke/retire the old one.
3. Replace `CLOUDFULCRUM_PUBLIC_KEY` in `backend/app/licensing.py` with the new
   public key.
4. Cut a new app release so customers receive the new public key.
5. Re-issue active customer licenses with the new key (old keys stop verifying
   once the new public key ships).
6. Record the rotation (date, reason, who) in the ticket / security log.

## Security rules

- Never commit a private key, `*.pem`, or `*.key` (enforced by `.gitignore`).
- `LICENSE_API_URL` / `LICENSE_API_TOKEN` are confidential — untracked env only.
- The shipped app must never call the issuing API; in-app validation is offline.
- Run `git diff --staged` before every commit on licensing changes and confirm
  only the public constant / tooling is present.
