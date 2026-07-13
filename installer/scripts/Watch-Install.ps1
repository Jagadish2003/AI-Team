# -----------------------------------------------------------------------------
# AgentIQ - Watch-Install.ps1
# Launched by the setup wizard's Finish dialog (and runnable manually). Shows
# the background installation progress live, confirms completion to the end
# user, and opens the app in the browser when it is actually ready.
#
# The MSI itself only lays down files and starts the AgentIQ-PostInstall
# scheduled task; the real installation (PostgreSQL, Python, IIS, services)
# runs in that task and takes 10-25 minutes. This window is the user's view
# into that process.
# -----------------------------------------------------------------------------
param(
    [string]$InstallDir = ""
)

if (-not $InstallDir) { $InstallDir = Split-Path -Parent $PSScriptRoot }
$InstallDir = $InstallDir.TrimEnd('\') + '\'
$LogDir     = "${InstallDir}logs"
$Marker     = "$LogDir\install-complete.txt"

$Host.UI.RawUI.WindowTitle = "AgentIQ Setup - Installation Progress"

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host "   AgentIQ - Installation in progress"                          -ForegroundColor Cyan
Write-Host "  ============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "   The wizard has finished copying files. AgentIQ is now being"
Write-Host "   installed in the background (PostgreSQL, Python, IIS, services)."
Write-Host "   This typically takes 10-25 minutes. Progress appears below."
Write-Host ""
Write-Host "   You can close this window at any time - the installation"
Write-Host "   continues regardless. It is finished when this file appears:"
Write-Host "     $Marker"
Write-Host ""

# Wait for the install log to be created by the post-install task
$log = $null
$deadline = (Get-Date).AddMinutes(5)
while (-not $log -and (Get-Date) -lt $deadline) {
    $log = Get-ChildItem "$LogDir\install-*.log" -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $log) { Start-Sleep -Seconds 3 }
}
if (-not $log) {
    Write-Host "   WARNING: no installation log appeared within 5 minutes." -ForegroundColor Yellow
    Write-Host "   Check that scheduled task 'AgentIQ-PostInstall' exists and is running:" -ForegroundColor Yellow
    Write-Host "     schtasks /Query /TN AgentIQ-PostInstall" -ForegroundColor Yellow
    Read-Host  "   Press Enter to close"
    exit 1
}

# Tail the log, showing step lines, until the completion marker appears.
# The step marker is U+2500 box-drawing chars; build it from the code point so
# this file stays pure ASCII (PS 5.1 misdecodes BOM-less non-ASCII sources).
$bar = [string][char]0x2500 * 3
$stepPattern = [regex]::Escape($bar) + "\s*(.+?)\s*" + [regex]::Escape($bar)
$pos = 0
$failed = $false
while ($true) {
    $lines = Get-Content $log.FullName -ErrorAction SilentlyContinue
    if ($lines -and $lines.Count -gt $pos) {
        foreach ($line in $lines[$pos..($lines.Count - 1)]) {
            if ($line -match $stepPattern)          { Write-Host ("   [STEP] " + $Matches[1]) -ForegroundColor Cyan }
            elseif ($line -match "OK:\s+(.*)")      { Write-Host ("     OK   " + $Matches[1]) -ForegroundColor Green }
            elseif ($line -match "SKIP:\s+(.*)")    { Write-Host ("     --   " + $Matches[1]) -ForegroundColor DarkGray }
            elseif ($line -match "\[WARN\]\s*(.*)") { Write-Host ("     WARN " + $Matches[1]) -ForegroundColor Yellow }
            elseif ($line -match "FATAL|\[ERROR\]") { Write-Host ("     !!   " + $line)       -ForegroundColor Red; $failed = $true }
        }
        $pos = $lines.Count
    }
    if (Test-Path $Marker) { break }
    # If the task is gone and no marker was written, the install died
    schtasks /Query /TN AgentIQ-PostInstall 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0 -and -not (Test-Path $Marker)) { $failed = $true; break }
    Start-Sleep -Seconds 5
}

Write-Host ""
if ($failed -and -not (Test-Path $Marker)) {
    Write-Host "  ============================================================" -ForegroundColor Red
    Write-Host "   Installation FAILED - see the log for details:"              -ForegroundColor Red
    Write-Host "     $($log.FullName)"                                           -ForegroundColor Red
    Write-Host "  ============================================================" -ForegroundColor Red
    Read-Host  "   Press Enter to close"
    exit 1
}

# Final health confirmation
Write-Host "   Verifying application health..."
$healthy = $false
for ($i = 0; $i -lt 12; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost/api/health" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { Start-Sleep -Seconds 5 }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   AgentIQ installation COMPLETE."                              -ForegroundColor Green
if ($healthy) {
    Write-Host "   Application verified healthy - opening in your browser."  -ForegroundColor Green
} else {
    Write-Host "   Services are still warming up; if the page does not load," -ForegroundColor Yellow
    Write-Host "   wait a minute and refresh."                                -ForegroundColor Yellow
}
Write-Host "   URL: http://localhost/"                                       -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Start-Process "http://localhost/"
Write-Host ""
Read-Host "   Press Enter to close this window"
