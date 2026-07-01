# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ — Uninstall.ps1
# Called by the MSI on uninstall. Stops and removes services/sites.
# Files are removed by the MSI itself after this script completes.
# ─────────────────────────────────────────────────────────────────────────────
param([string]$InstallDir = "C:\AgentIQ\")

$LogDir  = "$($InstallDir.TrimEnd('\'))\logs"
$LogFile = "$LogDir\uninstall-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-Log { param([string]$M) Add-Content $LogFile "$(Get-Date -F 'HH:mm:ss') $M"; Write-Host $M }

Write-Log "AgentIQ uninstall started."

# Stop and remove backend service
foreach ($svc in @("AgentIQ-Backend")) {
    $s = Get-Service $svc -ErrorAction SilentlyContinue
    if ($s) {
        Write-Log "  Stopping service: $svc"
        Stop-Service $svc -Force -ErrorAction SilentlyContinue
        $NSSMExe = "$($InstallDir.TrimEnd('\'))\tools\nssm.exe"
        if (Test-Path $NSSMExe) {
            & $NSSMExe remove $svc confirm 2>&1 | Add-Content $LogFile
        } else {
            & sc.exe delete $svc | Out-Null
        }
        Write-Log "  Service $svc removed"
    }
}

# Stop and remove PostgreSQL service
$pgSvc = Get-Service "AgentIQ-PostgreSQL" -ErrorAction SilentlyContinue
if ($pgSvc) {
    Write-Log "  Stopping PostgreSQL..."
    Stop-Service "AgentIQ-PostgreSQL" -Force -ErrorAction SilentlyContinue
    & sc.exe delete "AgentIQ-PostgreSQL" | Out-Null
    Write-Log "  PostgreSQL service removed"
}

# Remove IIS site
Import-Module WebAdministration -ErrorAction SilentlyContinue
if (Get-Website -Name "AgentIQ" -ErrorAction SilentlyContinue) {
    Write-Log "  Removing IIS site AgentIQ..."
    Stop-Website  -Name "AgentIQ" -ErrorAction SilentlyContinue
    Remove-Website -Name "AgentIQ" -ErrorAction SilentlyContinue
    Write-Log "  IIS site removed"
}

# Remove firewall rules
foreach ($rule in @("AgentIQ HTTP","AgentIQ HTTPS","AgentIQ Backend")) {
    Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
}
Write-Log "  Firewall rules removed"

# Remove registry keys
Remove-Item "HKLM:\SOFTWARE\AgentIQ" -Recurse -Force -ErrorAction SilentlyContinue
Write-Log "  Registry entries removed"

Write-Log "AgentIQ uninstall complete. Data in $InstallDir\backend\database\ was preserved."
