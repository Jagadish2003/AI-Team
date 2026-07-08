# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ — build.ps1
# Builds the AgentIQ-Setup.msi on the developer machine.
#
# Prerequisites (handled automatically if missing):
#   .NET SDK 8+, WiX Toolset v4, Node.js 20+, Python 3.11
#
# Output: installer\bin\AgentIQ-Setup-1.6.0.msi
#
# Usage:
#   cd <repo-root>
#   .\installer\build.ps1
#   .\installer\build.ps1 -SkipFrontend   # reuse existing frontend\dist
#   .\installer\build.ps1 -SkipWheels     # reuse existing backend\wheels
#   .\installer\build.ps1 -SkipDownloads  # reuse existing installer\downloads
# ─────────────────────────────────────────────────────────────────────────────
param(
    [switch]$SkipFrontend,
    [switch]$SkipWheels,
    [switch]$SkipDownloads
)

$ErrorActionPreference = "Stop"
$RepoRoot     = Split-Path $PSScriptRoot -Parent
$InstallerDir = $PSScriptRoot
$StagingDir   = "$InstallerDir\staging"
$DownloadDir  = "$InstallerDir\downloads"
$BinDir       = "$InstallerDir\bin"
$FrontendDir  = "$RepoRoot\frontend"
$BackendDir   = "$RepoRoot\backend"

$Version      = "1.7.0"
$OutMSI       = "$BinDir\AgentIQ-Setup-$Version.msi"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
foreach ($d in @($StagingDir, $DownloadDir, $BinDir)) {
    New-Item -ItemType Directory -Force $d | Out-Null
}

function Log   { param([string]$M) Write-Host "  $M" }
function Step  { param([string]$M) Write-Host "`n▶ $M" -ForegroundColor Cyan }
function OK    { param([string]$M) Write-Host "  ✓ $M" -ForegroundColor Green }
function Fail  { param([string]$M) Write-Host "  ✗ $M" -ForegroundColor Red; exit 1 }

function New-DownloadClient {
    $wc = New-Object Net.WebClient
    $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AgentIQ-Installer")
    return $wc
}

function Download {
    param([string]$Url, [string]$Out, [string]$Name)
    if (Test-Path $Out) { Log "  SKIP (cached): $Name"; return }
    Log "Downloading $Name..."
    (New-DownloadClient).DownloadFile($Url, $Out)
    OK "$Name downloaded"
}

# ── STEP 1: Prerequisites on build machine ────────────────────────────────────
Step "Build machine prerequisites"

# .NET SDK
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    Log "dotnet not found — installing .NET SDK 8..."
    $dotnetInst = "$env:TEMP\dotnet-install.ps1"
    (New-DownloadClient).DownloadFile(
        "https://dot.net/v1/dotnet-install.ps1", $dotnetInst)
    & $dotnetInst -Channel 8.0 -InstallDir "$env:ProgramFiles\dotnet"
    $env:PATH = "$env:ProgramFiles\dotnet;" + $env:PATH
}
OK ".NET SDK: $(dotnet --version)"

# WiX Toolset v4
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    Log "Installing WiX Toolset v4..."
    dotnet tool install --global wix --version 4.0.5
    $env:PATH = "$env:USERPROFILE\.dotnet\tools;" + $env:PATH
}
OK "WiX: $(wix --version)"

# Node.js
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Fail "Node.js not found. Install Node.js 20+ from https://nodejs.org and re-run."
}
OK "Node: $(node --version)"

# Python
$python = @("python", "py -3.11", "C:\Python311\python.exe") |
    Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $python) { Fail "Python 3.11 not found on build machine." }
OK "Python: $(& $python --version 2>&1)"

# ── STEP 2: Build frontend ─────────────────────────────────────────────────────
Step "Frontend build"
if ($SkipFrontend -and (Test-Path "$FrontendDir\dist\index.html")) {
    Log "SKIP (-SkipFrontend): reusing existing dist\"
} else {
    Push-Location $FrontendDir
    Log "npm ci..."
    npm ci --silent
    Log "npm run build (VITE_API_BASE_URL empty string → relative /api/ calls)..."
    # VITE_API_BASE_URL must be DEFINED as an empty string: the v1.7 apiClient
    # throws at runtime when it is nullish (blank page). PowerShell deletes an
    # env var assigned "", so define it via Vite's .env.production instead.
    $enviroFile = "$FrontendDir\.env.production"
    Set-Content $enviroFile -Value "VITE_API_BASE_URL=" -Encoding ASCII
    try {
        npm run build
    } finally {
        Remove-Item $enviroFile -Force -ErrorAction SilentlyContinue
    }
    Pop-Location
    OK "Frontend built → frontend\dist\"
}

# ── STEP 3: Pre-download Python wheels ────────────────────────────────────────
Step "Backend wheels (Windows/Python 3.11)"
$WheelsDir = "$BackendDir\wheels"
if ($SkipWheels -and (Test-Path $WheelsDir)) {
    Log "SKIP (-SkipWheels): reusing existing wheels\"
} else {
    New-Item -ItemType Directory -Force $WheelsDir | Out-Null
    Log "Downloading wheels for win_amd64 / cp311..."
    # Download binary wheels — some packages have no binary wheel and are downloaded as source
    & $python -m pip download `
        -r "$BackendDir\requirements.txt" `
        -d $WheelsDir `
        --platform win_amd64 `
        --python-version 311 `
        --only-binary ":all:" `
        --quiet
    # Second pass for source-only packages (no --only-binary)
    & $python -m pip download `
        -r "$BackendDir\requirements.txt" `
        -d $WheelsDir `
        --quiet
    OK "Wheels downloaded to backend\wheels\ ($((Get-ChildItem $WheelsDir).Count) files)"
}

# ── STEP 4: Download prerequisite installers ──────────────────────────────────
Step "Prerequisite installers"
if ($SkipDownloads) {
    Log "SKIP (-SkipDownloads): reusing existing downloads\"
} else {
    $prereqs = @(
        @{ Name="python-3.11.9-amd64.exe";                 Url="https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" }
        @{ Name="requestRouter_amd64.msi";                 Url="https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi" }
        @{ Name="rewrite_amd64_en-US.msi";                 Url="https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi" }
        @{ Name="nssm-2.24.zip";                           Url="https://nssm.cc/release/nssm-2.24.zip" }
        @{ Name="postgresql-16-windows-x64-binaries.zip";  Url="https://get.enterprisedb.com/postgresql/postgresql-16.3-1-windows-x64-binaries.zip" }
        @{ Name="vc_redist.x64.exe";                       Url="https://aka.ms/vs/17/release/vc_redist.x64.exe" }
        @{ Name="msodbcsql18_amd64.msi";                   Url="https://go.microsoft.com/fwlink/?linkid=2249006" }
    )
    foreach ($p in $prereqs) {
        Download $p.Url "$DownloadDir\$($p.Name)" $p.Name
    }
    OK "All prerequisite installers in installer\downloads\"
}

# ── STEP 5: Package application zip ───────────────────────────────────────────
Step "Package AgentIQ-app.zip"
$AppZip = "$StagingDir\AgentIQ-app.zip"
Log "Creating $AppZip ..."

# Items to bundle
$bundle = @(
    @{ Src="$FrontendDir\dist";            Dst="frontend\dist" }
    @{ Src="$BackendDir\app";              Dst="backend\app"   }
    @{ Src="$BackendDir\connectors";       Dst="backend\connectors" }
    @{ Src="$BackendDir\database";         Dst="backend\database"   }
    @{ Src="$BackendDir\discovery";        Dst="backend\discovery"  }
    @{ Src="$BackendDir\migrations";       Dst="backend\migrations" }
    @{ Src="$BackendDir\wheels";           Dst="backend\wheels"     }
    @{ Src="$BackendDir\alembic.ini";      Dst="backend\alembic.ini" }
    @{ Src="$BackendDir\requirements.txt"; Dst="backend\requirements.txt" }
    # v1.7: docker\postgres\init.sql removed with the uuid-ossp dependency;
    # Install.ps1 treats postgres\init.sql as optional.
)

if (Test-Path $AppZip) { Remove-Item $AppZip -Force }

Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($AppZip, "Create")
try {
    foreach ($item in $bundle) {
        if (-not (Test-Path $item.Src)) {
            Log "  WARNING: missing $($item.Src) — skipping"
            continue
        }
        if (Test-Path $item.Src -PathType Container) {
            Get-ChildItem $item.Src -Recurse -File | ForEach-Object {
                $rel  = $_.FullName.Substring($item.Src.Length).TrimStart('\')
                $entry = "$($item.Dst)\$rel" -replace '\\', '/'
                [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                    $zip, $_.FullName, $entry, "Optimal") | Out-Null
            }
        } else {
            $entry = $item.Dst -replace '\\', '/'
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, $item.Src, $entry, "Optimal") | Out-Null
        }
    }
} finally {
    $zip.Dispose()
}
$sizeMB = [Math]::Round((Get-Item $AppZip).Length / 1MB, 1)
OK "AgentIQ-app.zip created (${sizeMB} MB)"

# ── STEP 6: Build MSI ─────────────────────────────────────────────────────────
# Built via the SDK-style AgentIQ.wixproj (dotnet build), not the raw "wix build"
# CLI: the .wixproj declares WixToolset.UI.wixext / WixToolset.Util.wixext as
# normal NuGet PackageReferences, resolved by the standard NuGet restore. The
# "wix extension add" CLI cache is a separate, less reliable mechanism and is
# not used here.
Step "Build MSI"
Push-Location $InstallerDir
Log "Running dotnet build (restores WiX extensions via NuGet)..."
dotnet build AgentIQ.wixproj -c Release 2>&1 | Tee-Object -Variable buildOut
$buildExit = $LASTEXITCODE
if ($buildExit -ne 0) {
    $buildOut | Write-Host
    Pop-Location
    Fail "WiX project build failed (exit $buildExit)"
}
$builtMsi = Get-ChildItem -Path "$InstallerDir\bin" -Filter "AgentIQ-Setup.msi" -Recurse |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $builtMsi) { Pop-Location; Fail "Build succeeded but AgentIQ-Setup.msi not found under $InstallerDir\bin" }
Copy-Item $builtMsi.FullName $OutMSI -Force
Pop-Location

$msiMB = [Math]::Round((Get-Item $OutMSI).Length / 1MB, 1)
OK "MSI built: $OutMSI (${msiMB} MB)"

# ── Summary ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Build complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Output : $OutMSI"
Write-Host "  Size   : ${msiMB} MB"
Write-Host ""
Write-Host "  Deliver this single file to the customer."
Write-Host "  They run it as Administrator and the application"
Write-Host "  installs and opens automatically in their browser."
Write-Host "══════════════════════════════════════════════════════════" -ForegroundColor Green
