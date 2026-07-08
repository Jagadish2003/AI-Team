# -----------------------------------------------------------------------------
# AgentIQ - Package-App.ps1  (run on the BUILD machine, not the server)
# Produces the application build package (AgentIQ-app.zip) that DeployApp.ps1
# extracts on the prepared server. This is the "build files" deliverable.
#
# Contents of the zip:
#   frontend\dist\...            Vite production build (VITE_API_BASE_URL="")
#   backend\app\...              FastAPI app
#   backend\connectors\...       connectors
#   backend\database\...         DB layer + seed\ JSON (11 files)
#   backend\discovery\...        discovery engine
#   backend\migrations\...       Alembic migrations (0001..NNNN)
#   backend\alembic.ini
#   backend\requirements.txt
#   backend\wheels\...           (optional) offline pip wheels if present
#   postgres\init.sql            uuid-ossp bootstrap reference
#
# Usage (on the build machine, from repo root or anywhere):
#   powershell -ExecutionPolicy Bypass -File installer\scripts\Package-App.ps1
#   powershell -ExecutionPolicy Bypass -File installer\scripts\Package-App.ps1 -SkipFrontend
#   powershell -ExecutionPolicy Bypass -File installer\scripts\Package-App.ps1 -IncludeWheels
# -----------------------------------------------------------------------------
param(
    [switch]$SkipFrontend,
    [switch]$IncludeWheels,
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$FrontendDir = Join-Path $RepoRoot "frontend"
$BackendDir  = Join-Path $RepoRoot "backend"
$PostgresDir = Join-Path $RepoRoot "docker\postgres"
if (-not $OutDir) { $OutDir = Join-Path $RepoRoot "installer\bin" }
New-Item -ItemType Directory -Force $OutDir | Out-Null
$OutZip = Join-Path $OutDir "AgentIQ-app.zip"

function Log { param([string]$M) Write-Host "  $M" }
function Step { param([string]$M) Write-Host "`n[STEP] $M" -ForegroundColor Cyan }
function OK   { param([string]$M) Write-Host "  [OK] $M" -ForegroundColor Green }

# ---------------------------------------------------------------------------
# 1. Frontend build (relative API base so IIS proxies /api)
# ---------------------------------------------------------------------------
Step "Frontend build"
if ($SkipFrontend -and (Test-Path "$FrontendDir\dist\index.html")) {
    Log "SKIP (-SkipFrontend): reusing existing dist\"
} else {
    Push-Location $FrontendDir
    Log "npm ci ..."
    npm ci --silent
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "npm ci failed" }
    Log "npm run build (VITE_API_BASE_URL='') ..."
    # Define VITE_API_BASE_URL as an EMPTY STRING via .env.production —
    # PowerShell deletes an env var assigned "", and the v1.7 apiClient throws
    # at runtime (blank page) when the variable is undefined.
    Set-Content "$FrontendDir\.env.production" -Value "VITE_API_BASE_URL=" -Encoding ASCII
    $savedEAP = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    npm run build
    $buildExit = $LASTEXITCODE
    $ErrorActionPreference = $savedEAP
    Remove-Item "$FrontendDir\.env.production" -Force -ErrorAction SilentlyContinue
    Pop-Location
    if ($buildExit -ne 0) { throw "npm run build failed (exit $buildExit)" }
    OK "Frontend built -> frontend\dist\"
}

# ---------------------------------------------------------------------------
# 2. (optional) backend wheels for offline pip
# ---------------------------------------------------------------------------
if ($IncludeWheels) {
    Step "Backend wheels (win_amd64 / cp311)"
    $WheelsDir = Join-Path $BackendDir "wheels"
    New-Item -ItemType Directory -Force $WheelsDir | Out-Null
    $req = Join-Path $BackendDir "requirements.txt"
    Log "pip download win_amd64 / py3.11 ..."
    python -m pip download -r $req -d $WheelsDir --platform win_amd64 --python-version 311 --only-binary ":all:" --quiet
    python -m pip download -r $req -d $WheelsDir --quiet
    python -m pip download pip setuptools wheel -d $WheelsDir --quiet
    OK "Wheels ready in backend\wheels\"
}

# ---------------------------------------------------------------------------
# 3. Zip the build
# ---------------------------------------------------------------------------
Step "Package AgentIQ-app.zip"
$bundle = @(
    @{ Src = (Join-Path $FrontendDir "dist");             Dst = "frontend\dist"            }
    @{ Src = (Join-Path $BackendDir  "app");              Dst = "backend\app"              }
    @{ Src = (Join-Path $BackendDir  "connectors");       Dst = "backend\connectors"       }
    @{ Src = (Join-Path $BackendDir  "database");         Dst = "backend\database"         }
    @{ Src = (Join-Path $BackendDir  "discovery");        Dst = "backend\discovery"        }
    @{ Src = (Join-Path $BackendDir  "migrations");       Dst = "backend\migrations"       }
    @{ Src = (Join-Path $BackendDir  "alembic.ini");      Dst = "backend\alembic.ini"      }
    @{ Src = (Join-Path $BackendDir  "requirements.txt"); Dst = "backend\requirements.txt" }
    @{ Src = (Join-Path $PostgresDir "init.sql");         Dst = "postgres\init.sql"        }
)
# Include the offline pip wheels if they already exist (or were just refreshed
# with -IncludeWheels), so the VM can install packages without reaching PyPI.
$wheelsPath = Join-Path $BackendDir "wheels"
if (Test-Path $wheelsPath) {
    if ((Get-ChildItem $wheelsPath -File -ErrorAction SilentlyContinue).Count -gt 0) {
        $bundle += @{ Src = $wheelsPath; Dst = "backend\wheels" }
        Log "Including existing backend\wheels\ in the package"
    }
}

if (Test-Path $OutZip) { Remove-Item $OutZip -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($OutZip, "Create")
try {
    foreach ($item in $bundle) {
        if (-not (Test-Path $item.Src)) { Log "WARNING: not found, skipping: $($item.Src)"; continue }
        if (Test-Path $item.Src -PathType Container) {
            Get-ChildItem $item.Src -Recurse -File | ForEach-Object {
                $rel   = $_.FullName.Substring($item.Src.Length).TrimStart('\')
                $entry = ($item.Dst + "\" + $rel) -replace '\\','/'
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $entry, "Optimal") | Out-Null
            }
        } else {
            $entry = $item.Dst -replace '\\','/'
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $item.Src, $entry, "Optimal") | Out-Null
        }
    }
} finally { $zip.Dispose() }

$sizeMB = [Math]::Round((Get-Item $OutZip).Length / 1MB, 1)
OK "AgentIQ-app.zip created ($sizeMB MB)"
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  Build package ready:" -ForegroundColor Green
Write-Host "    $OutZip"
Write-Host ""
Write-Host "  Copy this to the server, then run:"
Write-Host "    DeployApp.ps1 -AppZip `"C:\Temp\AgentIQ-app.zip`""
Write-Host "============================================================" -ForegroundColor Green
