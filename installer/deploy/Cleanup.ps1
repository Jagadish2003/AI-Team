# -----------------------------------------------------------------------------
# AgentIQ - Cleanup.ps1
# Stops ALL AgentIQ services and removes old installation files, so the server
# is a clean slate before running Prerequisites.ps1 + deploying the app.
# Run as Administrator. Every action is non-fatal - the script always finishes.
#
# Removes:
#   - AgentIQ-Backend service (NSSM)
#   - AgentIQ-PostgreSQL service (ZIP-based: pg_ctl register / NSSM / sc)
#   - orphaned postgres.exe / uvicorn / python processes holding files open
#   - IIS site "AgentIQ"
#   - scheduled task "AgentIQ-PostInstall"
#   - firewall rules (AgentIQ HTTP / HTTPS / Backend)
#   - registry key HKLM:\SOFTWARE\AgentIQ
#   - the installed MSI product (if any)
#   - files under C:\AgentIQ and C:\Windows\Temp\AgentIQ-*
#
# Optional:
#   -IncludeOfficialPostgres   also uninstall an official EDB PostgreSQL 16
#                              (service postgresql-x64-16) - OFF by default
#   -KeepFiles                 stop/remove services but do NOT delete C:\AgentIQ
#
# Usage (as Administrator):
#   powershell -ExecutionPolicy Bypass -File Cleanup.ps1
#   powershell -ExecutionPolicy Bypass -File Cleanup.ps1 -IncludeOfficialPostgres
# -----------------------------------------------------------------------------
param(
    [string]$BaseDir = "C:\AgentIQ",
    [switch]$IncludeOfficialPostgres,
    [switch]$KeepFiles
)

$ErrorActionPreference = "Continue"
$BaseDir = $BaseDir.TrimEnd('\')

$LogFile = "C:\Windows\Temp\AgentIQ-cleanup-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
function Log { param([string]$M,[string]$L="INFO")
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$L] $M"
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
    Write-Host $line
}

$NSSM  = "$BaseDir\tools\nssm.exe"
$PGCtl = "$BaseDir\postgres\pgsql\bin\pg_ctl.exe"

# Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Log "Must be run as Administrator." "ERROR"; exit 1 }

Log "AgentIQ cleanup started. BaseDir=$BaseDir"
Log "Log: $LogFile"

# ---------------------------------------------------------------------------
# Helper: fully remove a service by name, trying every removal mechanism
# ---------------------------------------------------------------------------
function Remove-ServiceFully {
    param([string]$Name)
    $svc = Get-Service $Name -ErrorAction SilentlyContinue
    if ($null -eq $svc) { Log "  Service '$Name' not present"; return }
    Log "  Stopping service '$Name' (status $($svc.Status)) ..."
    Stop-Service $Name -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    # NSSM stop/remove (no-op if not an NSSM service)
    if (Test-Path $NSSM) {
        & $NSSM stop   $Name 2>$null | Out-Null
        & $NSSM remove $Name confirm 2>$null | Out-Null
    }
    # pg_ctl unregister (no-op if not a pg_ctl service)
    if (Test-Path $PGCtl) { & $PGCtl unregister -N $Name 2>$null | Out-Null }
    # sc delete as the catch-all
    & sc.exe delete $Name 2>$null | Out-Null
    Start-Sleep -Seconds 1
    if (Get-Service $Name -ErrorAction SilentlyContinue) {
        Log "  WARNING: service '$Name' still present (may need a reboot)" "WARN"
    } else {
        Log "  Service '$Name' removed"
    }
}

# ---------------------------------------------------------------------------
# 1. Stop and remove AgentIQ services
# ---------------------------------------------------------------------------
Log ""
Log "=== Services ==="
Remove-ServiceFully "AgentIQ-Backend"
Remove-ServiceFully "AgentIQ-PostgreSQL"

# ---------------------------------------------------------------------------
# 2. Kill orphaned processes that hold OUR install files open.
#    Scoped strictly to $BaseDir so an unrelated / pre-existing PostgreSQL
#    (e.g. the official one under C:\Program Files\PostgreSQL) is NOT disturbed.
# ---------------------------------------------------------------------------
Log ""
Log "=== Orphaned processes (only those running from $BaseDir) ==="
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath -like "$BaseDir\*") -or
        ($_.CommandLine    -and $_.CommandLine    -like "*$BaseDir\*")
    } |
    Where-Object { $_.Name -in @("postgres.exe","pg_ctl.exe","python.exe","uvicorn.exe") } |
    ForEach-Object {
        Log "  Killing PID $($_.ProcessId) ($($_.Name)) from $BaseDir"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Log "  Orphan process sweep complete (unrelated services untouched)"

# ---------------------------------------------------------------------------
# 3. IIS site
# ---------------------------------------------------------------------------
Log ""
Log "=== IIS site ==="
Import-Module WebAdministration -ErrorAction SilentlyContinue
if (Get-Website -Name "AgentIQ" -ErrorAction SilentlyContinue) {
    Stop-Website  -Name "AgentIQ" -ErrorAction SilentlyContinue
    Remove-Website -Name "AgentIQ" -ErrorAction SilentlyContinue
    Log "  IIS site 'AgentIQ' removed"
} else {
    Log "  IIS site 'AgentIQ' not present"
}
# Remove the AgentIQ app pool if it exists
if (Test-Path "IIS:\AppPools\AgentIQ") {
    Remove-WebAppPool -Name "AgentIQ" -ErrorAction SilentlyContinue
    Log "  App pool 'AgentIQ' removed"
}

# ---------------------------------------------------------------------------
# 4. Scheduled task
# ---------------------------------------------------------------------------
Log ""
Log "=== Scheduled task ==="
if (Get-ScheduledTask -TaskName "AgentIQ-PostInstall" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "AgentIQ-PostInstall" -Confirm:$false -ErrorAction SilentlyContinue
    Log "  Scheduled task 'AgentIQ-PostInstall' removed"
} else {
    Log "  Scheduled task not present"
}

# ---------------------------------------------------------------------------
# 5. Firewall rules
# ---------------------------------------------------------------------------
Log ""
Log "=== Firewall rules ==="
foreach ($rule in @("AgentIQ HTTP","AgentIQ HTTPS","AgentIQ Backend")) {
    if (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
        Log "  Removed firewall rule '$rule'"
    }
}
Log "  Firewall cleanup complete"

# ---------------------------------------------------------------------------
# 6. Uninstall the MSI product (if registered)
# ---------------------------------------------------------------------------
Log ""
Log "=== MSI product ==="
try {
    $app = Get-WmiObject Win32_Product -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -like "AgentIQ*" }
    if ($app) {
        foreach ($a in $app) {
            Log "  Uninstalling MSI product '$($a.Name)' ..."
            $a.Uninstall() | Out-Null
        }
        Log "  MSI product uninstalled"
    } else {
        Log "  No registered AgentIQ MSI product"
    }
} catch { Log "  MSI uninstall skipped: $_" "WARN" }

# ---------------------------------------------------------------------------
# 7. Registry
# ---------------------------------------------------------------------------
Log ""
Log "=== Registry ==="
if (Test-Path "HKLM:\SOFTWARE\AgentIQ") {
    Remove-Item "HKLM:\SOFTWARE\AgentIQ" -Recurse -Force -ErrorAction SilentlyContinue
    Log "  Removed HKLM:\SOFTWARE\AgentIQ"
} else {
    Log "  No AgentIQ registry key"
}

# ---------------------------------------------------------------------------
# 8. (Optional) uninstall official EDB PostgreSQL 16
# ---------------------------------------------------------------------------
if ($IncludeOfficialPostgres) {
    Log ""
    Log "=== Official PostgreSQL 16 (EDB) ==="
    Remove-ServiceFully "postgresql-x64-16"
    $pgUninstall = "C:\Program Files\PostgreSQL\16\uninstall-postgresql.exe"
    if (Test-Path $pgUninstall) {
        Log "  Running EDB uninstaller (unattended) ..."
        $p = Start-Process $pgUninstall -ArgumentList "--mode unattended" -Wait -PassThru -NoNewWindow
        Log "  EDB uninstaller exit $($p.ExitCode)"
        Remove-Item "C:\Program Files\PostgreSQL" -Recurse -Force -ErrorAction SilentlyContinue
        Log "  Removed C:\Program Files\PostgreSQL"
    } else {
        Log "  EDB uninstaller not found (PostgreSQL may not be installed)"
    }
}

# ---------------------------------------------------------------------------
# 9. Delete files
# ---------------------------------------------------------------------------
Log ""
Log "=== Files ==="
if ($KeepFiles) {
    Log "  -KeepFiles set: leaving $BaseDir in place"
} else {
    if (Test-Path $BaseDir) {
        Remove-Item $BaseDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $BaseDir) {
            Log "  WARNING: some files under $BaseDir could not be deleted (in use?)" "WARN"
        } else {
            Log "  Removed $BaseDir"
        }
    } else {
        Log "  $BaseDir not present"
    }
}
Remove-Item "C:\Windows\Temp\AgentIQ-*" -Force -ErrorAction SilentlyContinue
Log "  Removed old temp logs (C:\Windows\Temp\AgentIQ-*)"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Log ""
Log "==========================================================="
Log "  AgentIQ cleanup complete."
if (Get-Service "AgentIQ-*" -ErrorAction SilentlyContinue) {
    Log "  NOTE: a service is still listed - a reboot may be needed." "WARN"
}
Log "  The server is ready for a fresh Prerequisites.ps1 run."
Log "  Log: $LogFile"
Log "==========================================================="
