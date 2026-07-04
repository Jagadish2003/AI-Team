# -----------------------------------------------------------------------------
# AgentIQ - Restart-Services.ps1
# Restarts AgentIQ services so configuration changes (e.g. edited backend\.env)
# take effect. Run as Administrator.
#
# Default: restarts the backend service, then IIS, then health-checks.
# The backend reads .env only at startup, so restart it after editing .env.
#
# Options:
#   -IncludePostgres   also restart PostgreSQL (rarely needed for .env changes;
#                      drops open DB connections briefly)
#   -SkipIIS           do not run iisreset
#
# Usage (as Administrator):
#   powershell -ExecutionPolicy Bypass -File Restart-Services.ps1
#   powershell -ExecutionPolicy Bypass -File Restart-Services.ps1 -IncludePostgres
# -----------------------------------------------------------------------------
param(
    [string]$BaseDir = "C:\AgentIQ",
    [switch]$IncludePostgres,
    [switch]$SkipIIS
)

$ErrorActionPreference = "Continue"
$BaseDir = $BaseDir.TrimEnd('\')

function Log { param([string]$M,[string]$L="INFO")
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')][$L] $M"
}

# Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Log "Must be run as Administrator." "ERROR"; exit 1 }

# Resolve PostgreSQL service name (official installer = postgresql-x64-16,
# older MSI build = AgentIQ-PostgreSQL). Prefer the manifest if present.
$pgService = $null
$manifest = "$BaseDir\prereqs.json"
if (Test-Path $manifest) {
    try { $pgService = (Get-Content $manifest -Raw | ConvertFrom-Json).pgService } catch {}
}
if (-not $pgService) {
    foreach ($cand in @("postgresql-x64-16","AgentIQ-PostgreSQL")) {
        if (Get-Service $cand -ErrorAction SilentlyContinue) { $pgService = $cand; break }
    }
}

Log "AgentIQ service restart started."

# ---------------------------------------------------------------------------
# 1. PostgreSQL (optional)
# ---------------------------------------------------------------------------
if ($IncludePostgres) {
    if ($pgService -and (Get-Service $pgService -ErrorAction SilentlyContinue)) {
        Log "Restarting PostgreSQL service '$pgService' ..."
        Restart-Service $pgService -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 4
        $s = Get-Service $pgService -ErrorAction SilentlyContinue
        Log "  PostgreSQL status: $($s.Status)"
    } else {
        Log "  PostgreSQL service not found - skipping" "WARN"
    }
}

# ---------------------------------------------------------------------------
# 2. Backend service (reads .env at startup)
# ---------------------------------------------------------------------------
$svc = "AgentIQ-Backend"
if (Get-Service $svc -ErrorAction SilentlyContinue) {
    Log "Restarting backend service '$svc' ..."
    Restart-Service $svc -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 4
    $s = Get-Service $svc -ErrorAction SilentlyContinue
    Log "  Backend status: $($s.Status)"
    if ($s.Status -ne "Running") {
        Log "  Backend not running - check $BaseDir\logs\backend-stderr.log" "WARN"
        if (Test-Path "$BaseDir\logs\backend-stderr.log") {
            Get-Content "$BaseDir\logs\backend-stderr.log" -Tail 15 | ForEach-Object { Log "    $_" "WARN" }
        }
    }
} else {
    Log "  Backend service '$svc' not found - is the app deployed?" "WARN"
}

# ---------------------------------------------------------------------------
# 3. IIS
# ---------------------------------------------------------------------------
if (-not $SkipIIS) {
    Log "Restarting IIS ..."
    & iisreset /restart /noforce 2>&1 | ForEach-Object { Log "  $_" }
}

# ---------------------------------------------------------------------------
# 4. Health check
# ---------------------------------------------------------------------------
Log "Health check ..."
Start-Sleep -Seconds 4
$ok = $false
for ($i=0; $i -lt 10; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Start-Sleep -Seconds 3
}
if ($ok) { Log "  Backend /api/health OK" }
else     { Log "  Backend /api/health not responding yet" "WARN" }

try {
    $r2 = Invoke-WebRequest -Uri "http://localhost/api/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    Log "  IIS proxy /api/health -> HTTP $($r2.StatusCode)"
} catch { Log "  IIS proxy /api/health not responding: $_" "WARN" }

Log ""
Log "Restart complete. Current status:"
Get-Service $svc, $pgService -ErrorAction SilentlyContinue |
    Format-Table Name, Status, StartType -AutoSize | Out-String | ForEach-Object { Write-Host $_ }
