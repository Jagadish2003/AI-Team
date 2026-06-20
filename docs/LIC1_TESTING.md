# LIC-1 — Offline License Key System: Test Guide

How to manually verify the offline licensing feature (LIC-1). The app validates a
**signed, offline** license key against a baked-in public key, degrades
gracefully through grace → read-only, and exposes an Owner-only admin page.

No network call is ever made to validate a license.

---

## 1. Setup

```powershell
# backend
cd backend
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# frontend (second terminal)
cd frontend
npm run dev
```

Log in as an **Owner** (the dev user is seeded as owner). Open
`http://localhost:5173/license`.

---

## 2. Get a license key

A valid key must be signed by CloudFulcrum's **private** key (not in the repo).
Pick one path:

### Path A — real key from the issuing API (happy-path only)
Ask the owner of `LICENSE_API_URL` / `LICENSE_API_TOKEN` for a key, or POST to
the issuing API yourself (Postman). Issued keys are always **valid** (expiry =
today + term), so this path tests valid / update / gate-unblock / owner-only —
but not grace / read-only.

### Path B — self-service dev keys for EVERY state (recommended)
No secrets needed. Generates a throwaway keypair and one key per state.

```powershell
# run from the repo root
python backend/license/dev_mint_test_keys.py
```

This prints a **DEV PUBLIC KEY** and writes keys to `backend/license/test_keys/`
(git-ignored). To make the app trust them:

1. Copy the printed DEV PUBLIC KEY into `CLOUDFULCRUM_PUBLIC_KEY` in
   `backend/app/licensing.py` — **temporary, do not commit**.
2. Restart the backend.
3. Paste the keys from `backend/license/test_keys/` (below) into the License page.
4. When done: `git checkout backend/app/licensing.py` to restore the real key.

| File | Tests state |
|------|-------------|
| `key_valid.txt` | Valid (within term) |
| `key_grace.txt` | Grace (expired, within 14-day grace) |
| `key_readonly.txt` | Read-only (past grace) |
| `key_tampered.txt` | Invalid (forged payload) |

---

## 3. Acceptance-criteria checklist

| AC | Steps | Expected |
|----|-------|----------|
| **AC3** Valid | Paste `key_valid.txt` → Update key | Badge **Valid** (green); Issued to / Term / Expiry / Days remaining shown; no banner; success toast |
| **AC7** Renew, no restart | While on grace/read-only, paste a valid key | Status flips to Valid **and the banner clears immediately** — no reload/restart |
| **AC4** Grace | Paste `key_grace.txt` | Badge **Grace** (amber); **amber banner** "Your AgentIQ license expired on … Contact CloudFulcrum to renew." on every page; discovery runs still work |
| **AC5** Read-only | Paste `key_readonly.txt` | Badge **Read-only** (red); **red banner** "License expired. Renew to resume discovery runs."; **Discovery Run / Stack Builder are blocked** (402); Source Intelligence / Opportunity Review / reports / graph still load; login still works |
| **AC2** Tampered | Paste `key_tampered.txt`, or edit one char of a valid key | "This key is not valid" error toast; **stored key unchanged** |
| **AC6** No license | Clear the key (see §5), reload | Badge **Read-only**; banner **"No valid license installed. Paste a valid license key to activate AgentIQ."**; login + paste still work |
| **AC1** Offline | Disconnect from the network, then validate any key | Still validates — no outbound call |
| **AC8** Clock rollback | With a valid key installed, set the OS clock back >2 days, wait for the periodic re-check or restart | Drops to Read-only; banner shows clock-inconsistency message; `license.clock_anomaly` telemetry emitted |

> Banner appears for **every role** (owner, analyst, viewer) — an analyst whose
> run is blocked sees why.

---

## 4. Role-based access (AC9)

Log in (or switch the dev role via `DEV_JWT_ROLE`) as each role:

| Role | License nav entry | `/license` page | Discovery run in read-only |
|------|-------------------|-----------------|----------------------------|
| Owner | visible | full access | blocked (402) |
| Analyst | hidden | redirected away | blocked (402) |
| Viewer | hidden | redirected away | n/a (cannot start runs) |

The status read endpoints are also server-gated: `GET /api/license` and
`POST /api/license/update-key` return **403** for non-owners.

---

## 5. Useful commands

Clear the installed license (to test the fresh / AC6 state):

```powershell
cd backend
.\.venv\Scripts\python.exe -c "from dotenv import load_dotenv; load_dotenv('.env'); from app.db import kv_set; from app.license_runtime import LICENSE_KEY_KV; kv_set(LICENSE_KEY_KV, None); print('cleared')"
```

Inspect license state in Postgres:

```sql
SELECT key, payload FROM kv WHERE key LIKE 'license:%' ORDER BY key;
```

Verify a key offline against the shipped public key:

```powershell
python backend/license/verify_license.py --key "<payload_b64>.<sig_b64>"
```

---

## 6. Automated tests

```powershell
# backend (contract + unit)
cd backend
python -m pytest tests/contract/test_license_lifecycle.py tests/contract/test_license_routes.py tests/contract/test_license_gate.py tests/contract/test_license_packaging.py tests/unit/test_licensing.py tests/unit/test_license_runtime.py tests/unit/test_license_telemetry.py

# signing CLI
python -m pytest license/tests

# frontend
cd ../frontend
npx vitest run src/__tests__/LicensePage.test.tsx src/__tests__/LicenseBanner.test.tsx
```

---

## 7. Cleanup

- Restore the real public key if you used Path B: `git checkout backend/app/licensing.py`
- `backend/license/test_keys/` and any `*.pem` are git-ignored — never commit them.
- Reset the OS clock if you tested AC8.
