# -----------------------------------------------------------------------------
# AgentIQ - SetupDatabase.ps1
# Installs the application schema and seed data into the PostgreSQL database that
# Prerequisites.ps1 created. Safe to re-run (migrations + seed are idempotent).
# Run as Administrator AFTER Prerequisites.ps1 and after the backend build has
# been deployed (venv + backend files present).
#
# Steps:
#   1  Verify DB reachable + uuid-ossp extension present
#   2  alembic upgrade head        (creates all tables - migrations 0001..NNNN)
#   3  python database\seed_loader.py  (loads connectors, mappings, permissions, ...)
#
# Reads C:\AgentIQ\prereqs.json (written by Prerequisites.ps1) for DB details.
#
# Usage (as Administrator):
#   powershell -ExecutionPolicy Bypass -File SetupDatabase.ps1
# -----------------------------------------------------------------------------
param(
    [string]$BaseDir = "C:\AgentIQ"
)

$ErrorActionPreference = "Stop"
$BaseDir = $BaseDir.TrimEnd('\')

$LogFile = "C:\Windows\Temp\AgentIQ-dbsetup-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
function Log { param([string]$M,[string]$L="INFO")
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$L] $M"
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
    if ($L -eq "ERROR") { Write-Error $M -ErrorAction Continue } else { Write-Host $line }
}

Log "AgentIQ database setup started. BaseDir=$BaseDir"

# ---------------------------------------------------------------------------
# Load manifest from Prerequisites.ps1
# ---------------------------------------------------------------------------
$manifestPath = "$BaseDir\prereqs.json"
if (-not (Test-Path $manifestPath)) {
    Log "Manifest not found at $manifestPath. Run Prerequisites.ps1 first." "ERROR"
    exit 1
}
$m = Get-Content $manifestPath -Raw | ConvertFrom-Json
$PgBin      = $m.pgBin
$PgPort     = $m.pgPort
$PgUser     = $m.pgUser
$PgPassword = $m.pgPassword
$PgDatabase = $m.pgDatabase
Log "PostgreSQL : $PgBin (port $PgPort, db $PgDatabase, user $PgUser)"

$BackendDir = "$BaseDir\backend"
$VenvDir    = "$BackendDir\.venv"
$VenvPy     = "$VenvDir\Scripts\python.exe"
$VenvAlembic= "$VenvDir\Scripts\alembic.exe"
if (-not (Test-Path $VenvPy)) {
    Log "Backend venv not found at $VenvPy. Deploy the app build first (DeployApp.ps1)." "ERROR"
    exit 1
}

$DbUrl = "postgresql://${PgUser}:${PgPassword}@localhost:${PgPort}/${PgDatabase}"

# ---------------------------------------------------------------------------
# 1. Verify DB reachable + uuid-ossp
# ---------------------------------------------------------------------------
Log ""
Log "=== Verify database ==="
$env:PGPASSWORD = $PgPassword
& "$PgBin\pg_isready.exe" -p $PgPort 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Log "PostgreSQL not accepting connections on port $PgPort" "ERROR"; exit 1 }
Log "  PostgreSQL is accepting connections"

$ext = & "$PgBin\psql.exe" -U $PgUser -p $PgPort -d $PgDatabase -tAc `
    "SELECT 1 FROM pg_extension WHERE extname='uuid-ossp';" 2>&1
if ($ext -match "^1") {
    Log "  uuid-ossp extension present"
} else {
    Log "  uuid-ossp extension MISSING - attempting to create (needs superuser)" "WARN"
    & "$PgBin\psql.exe" -U $PgUser -p $PgPort -d $PgDatabase `
        -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>&1 | ForEach-Object { Log "    $_" }
}

# ---------------------------------------------------------------------------
# 2. Alembic migrations
# ---------------------------------------------------------------------------
Log ""
Log "=== Alembic migrations ==="
Push-Location $BackendDir
$env:PYTHONPATH  = $BackendDir
$env:DATABASE_URL = $DbUrl
if (-not (Test-Path "$BackendDir\alembic.ini")) {
    Pop-Location
    Log "alembic.ini not found in $BackendDir" "ERROR"; exit 1
}
& $VenvAlembic upgrade head 2>&1 | ForEach-Object { Log "  $_" }
$algExit = $LASTEXITCODE
Pop-Location
if ($algExit -ne 0) { Log "alembic upgrade head failed (exit $algExit)" "ERROR"; exit 1 }
Log "  Migrations applied (schema up to head)"

# ---------------------------------------------------------------------------
# 3. Seed data
# ---------------------------------------------------------------------------
Log ""
Log "=== Seed data ==="
$SeedScript = "$BackendDir\database\seed_loader.py"
if (-not (Test-Path $SeedScript)) {
    Log "  seed_loader.py not found - skipping seed" "WARN"
} else {
    Push-Location $BackendDir
    $env:PYTHONPATH   = $BackendDir
    $env:DATABASE_URL = $DbUrl
    & $VenvPy "database\seed_loader.py" 2>&1 | ForEach-Object { Log "  $_" }
    $seedExit = $LASTEXITCODE
    Pop-Location
    if ($seedExit -ne 0) { Log "seed_loader failed (exit $seedExit)" "ERROR"; exit 1 }
    Log "  Seed data loaded"
}

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
Log ""
Log "=== Verify tables ==="
$tableCount = & "$PgBin\psql.exe" -U $PgUser -p $PgPort -d $PgDatabase -tAc `
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>&1
Log "  Public tables: $($tableCount.Trim())"

Log ""
Log "==========================================================="
Log "  DATABASE SETUP COMPLETE."
Log "  DB : $PgDatabase on port $PgPort (user $PgUser)"
Log "  Log: $LogFile"
Log "==========================================================="
