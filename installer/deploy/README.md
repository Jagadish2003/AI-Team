# AgentIQ — Prerequisite-Based Deployment (Windows Server 2025)

Deploy AgentIQ by first installing prerequisites with their official installers,
then deploying the application build. This avoids the all-in-one MSI and is the
most reliable path.

All server scripts run **as Administrator** from a single folder:
`C:\Temp\AgentIQ-Deploy\`. Nothing is written into the app directory except the
application itself.

---

## Scripts (run order)

| # | Script | Where | One-line description |
|---|--------|-------|----------------------|
| 1 | `Package-App.ps1` | Build machine | Builds the frontend and zips frontend+backend into `AgentIQ-app.zip` (the deployable build). |
| 2 | `Cleanup.ps1` | Server | **OPTIONAL (reinstall only)** — removes a previous AgentIQ install; skip on a fresh machine. |
| 3 | `Prerequisites.ps1` | Server | Installs IIS, VC++, URL Rewrite, ARR, ODBC, PostgreSQL 16, Python 3.11, NSSM from the internet; creates the DB + user. |
| 4 | `DeployApp.ps1` | Server | Extracts the build, writes `.env`, creates the venv, runs DB setup, registers the backend service, configures IIS. |
| 5 | `SetupDatabase.ps1` | Server | Applies Alembic migrations and loads seed data (called automatically by `DeployApp.ps1`; can be run standalone). |
| 6 | `Restart-Services.ps1` | Server | Restarts the backend + IIS so edited `.env` values take effect; health-checks the API. |

> `SetupDatabase.ps1` must sit **next to** `DeployApp.ps1` — DeployApp invokes it.

---

## Step 1 — Build the app

Produces `installer\bin\AgentIQ-app.zip`.
> One-liner: builds the frontend with a relative API base and packages the deployable zip.

**Option A — build on a separate build machine, copy the zip to the server:**
```powershell
powershell -ExecutionPolicy Bypass -File installer\deploy\Package-App.ps1 -IncludeWheels
```

**Option B — clone this repo on the server and build there** (needs Node.js + Python 3.11 on the server; Node is only needed to build the frontend):
```powershell
git clone <repo-url> C:\AgentIQ-src
cd C:\AgentIQ-src
powershell -ExecutionPolicy Bypass -File installer\deploy\Package-App.ps1 -IncludeWheels
# -> C:\AgentIQ-src\installer\bin\AgentIQ-app.zip
```
Then run the server steps below directly from the cloned `installer\deploy\` folder,
pointing `-AppZip` at the built zip:
```powershell
cd C:\AgentIQ-src\installer\deploy
powershell -ExecutionPolicy Bypass -File .\Prerequisites.ps1
powershell -ExecutionPolicy Bypass -File .\DeployApp.ps1 -AppZip "..\bin\AgentIQ-app.zip"
```

---

## Prepare the server

Copy these into `C:\Temp\AgentIQ-Deploy\` on the server:

```
Cleanup.ps1
Prerequisites.ps1
SetupDatabase.ps1
DeployApp.ps1
Restart-Services.ps1
AgentIQ-app.zip
```

Create the folder if needed:
```powershell
New-Item -ItemType Directory -Force "C:\Temp\AgentIQ-Deploy" | Out-Null
```

---

## Step 2 — Clean any previous install (server, Administrator) — OPTIONAL

> **Skip this on a fresh machine.** Run it ONLY to remove a previous AgentIQ
> install before redeploying. It removes only `AgentIQ-*` services and the
> `C:\AgentIQ` folder, and it kills processes ONLY if they run from `C:\AgentIQ`
> — a pre-existing/unrelated PostgreSQL or other service is never disturbed.

```powershell
cd C:\Temp\AgentIQ-Deploy
powershell -ExecutionPolicy Bypass -File .\Cleanup.ps1
```
> One-liner: wipes a prior AgentIQ install (services, IIS site, scheduled task, registry, `C:\AgentIQ` files).

Optional flags: `-IncludeOfficialPostgres` (also uninstall EDB PostgreSQL), `-KeepFiles` (leave `C:\AgentIQ`).

**Fresh install order = Step 3 -> Step 4.** Skip Step 2 entirely.

---

## Step 3 — Install prerequisites (server, Administrator)

```powershell
powershell -ExecutionPolicy Bypass -File .\Prerequisites.ps1
```
> One-liner: installs every prerequisite from official sources and creates the `agentiq` database, user, and `uuid-ossp` extension.

Outputs `C:\AgentIQ\prereqs.json` (manifest) and `C:\AgentIQ\db-credentials.txt` (generated DB credentials).

---

## Step 4 — Deploy the application (server, Administrator)

```powershell
powershell -ExecutionPolicy Bypass -File .\DeployApp.ps1 -AppZip ".\AgentIQ-app.zip"
```
> One-liner: extracts the build, writes `.env`, installs Python packages, runs DB migrations + seed, starts the backend service, and configures the IIS site.

Optional: `-AnthropicApiKey "sk-ant-..."` to enable live LLM enrichment (otherwise deterministic fallbacks run).

When it finishes, open **http://localhost/** — the AgentIQ login screen.

---

## Step 5 — Edit config and restart (server, Administrator)

```powershell
notepad C:\AgentIQ\backend\.env
powershell -ExecutionPolicy Bypass -File .\Restart-Services.ps1
```
> One-liner: restarts the backend + IIS to apply edited `.env` values and health-checks the API.

Optional flags: `-IncludePostgres` (also restart PostgreSQL), `-SkipIIS` (backend only).

---

## Where things live

| Location | Contents |
|----------|----------|
| `C:\Temp\AgentIQ-Deploy\` | All deployment scripts + `AgentIQ-app.zip` (run from here) |
| `C:\AgentIQ\` | App only: `frontend\dist`, `backend`, `.venv`, `logs`, `tools\nssm.exe`, `.env`, `prereqs.json`, `db-credentials.txt` |
| `C:\Program Files\PostgreSQL\16\` | PostgreSQL (official installer) |
| `C:\Windows\Temp\AgentIQ-*.log` | Run logs for each script |

---

## Services created

| Service | Purpose |
|---------|---------|
| `postgresql-x64-16` | PostgreSQL 16 database (port 5433) |
| `AgentIQ-Backend` | FastAPI/uvicorn backend (port 8000), managed by NSSM |
| IIS site `AgentIQ` | Serves the frontend on port 80 and reverse-proxies `/api` to the backend |

---

## Health checks

```powershell
Get-Service postgresql-x64-16, AgentIQ-Backend | Format-Table -AutoSize
Invoke-WebRequest http://localhost:8000/api/health -UseBasicParsing   # backend direct
Invoke-WebRequest http://localhost/api/health       -UseBasicParsing   # through IIS proxy
```

---

## Requirements

- Windows Server 2025 (or 2016+), x64
- Administrator login
- ~5 GB free disk on C:
- Internet access (prerequisites are downloaded from official sources)
