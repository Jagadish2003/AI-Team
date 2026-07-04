# -----------------------------------------------------------------------------
# AgentIQ - DeployApp.ps1
# Deploys the AgentIQ application build onto a server already prepared by
# Prerequisites.ps1. Run as Administrator.
#
# Steps:
#   1  Extract the app build (AgentIQ-app.zip) to C:\AgentIQ
#   2  Write backend\.env (DB URL + generated JWT_SECRET + CREDENTIAL_VAULT_KEY)
#   3  Create Python venv + install backend packages
#   4  Run database setup (migrations + seed) via SetupDatabase.ps1
#   5  Register + start the backend service (NSSM -> uvicorn :8000)
#   6  Create the IIS site + web.config (/api reverse proxy + SPA fallback)
#   7  Health check
#
# Reads C:\AgentIQ\prereqs.json (from Prerequisites.ps1) for DB / Python / NSSM.
#
# Usage (as Administrator):
#   powershell -ExecutionPolicy Bypass -File DeployApp.ps1 -AppZip "C:\Temp\AgentIQ-app.zip"
# -----------------------------------------------------------------------------
param(
    [Parameter(Mandatory=$true)][string]$AppZip,
    [string]$BaseDir = "C:\AgentIQ",
    [string]$AnthropicApiKey = ""   # optional; deterministic fallbacks work without it
)

$ErrorActionPreference = "Stop"
$BaseDir = $BaseDir.TrimEnd('\')
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$LogFile = "C:\Windows\Temp\AgentIQ-deploy-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
function Log { param([string]$M,[string]$L="INFO")
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$L] $M"
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
    if ($L -eq "ERROR") { Write-Error $M -ErrorAction Continue } else { Write-Host $line }
}
function Fail { param([string]$M) Log $M "ERROR"; throw $M }

$noBom = New-Object System.Text.UTF8Encoding $false

Log "AgentIQ app deploy started. BaseDir=$BaseDir"
Log "AppZip: $AppZip"

if (-not (Test-Path $AppZip)) { Fail "App build not found: $AppZip" }

# ---------------------------------------------------------------------------
# Load prerequisites manifest
# ---------------------------------------------------------------------------
$manifestPath = "$BaseDir\prereqs.json"
if (-not (Test-Path $manifestPath)) { Fail "Manifest $manifestPath missing. Run Prerequisites.ps1 first." }
$m = Get-Content $manifestPath -Raw | ConvertFrom-Json
$PgBin      = $m.pgBin
$PgPort     = $m.pgPort
$PgUser     = $m.pgUser
$PgPassword = $m.pgPassword
$PgDatabase = $m.pgDatabase
$PyCmd      = $m.pythonCmd
$NSSM       = $m.nssmExe
Log "PostgreSQL : port $PgPort, db $PgDatabase, user $PgUser"
Log "Python     : $PyCmd"
Log "NSSM       : $NSSM"

$BackendDir  = "$BaseDir\backend"
$FrontendDir = "$BaseDir\frontend"
$DistDir     = "$FrontendDir\dist"
$LogDir      = "$BaseDir\logs"
New-Item -ItemType Directory -Force $LogDir | Out-Null

# ---------------------------------------------------------------------------
# 1. Extract app build (clean copy of frontend + backend)
# ---------------------------------------------------------------------------
Log ""
Log "=== Extract app build ==="
# Stop backend service so files aren't locked
if (Get-Service "AgentIQ-Backend" -ErrorAction SilentlyContinue) {
    Stop-Service "AgentIQ-Backend" -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
foreach ($d in @($FrontendDir, "$BackendDir\app", "$BackendDir\connectors",
                 "$BackendDir\database", "$BackendDir\discovery", "$BackendDir\migrations")) {
    if (Test-Path $d) { Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [System.IO.Compression.ZipFile]::OpenRead($AppZip)
try {
    foreach ($entry in $archive.Entries) {
        $dest = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($BaseDir, $entry.FullName))
        if ($entry.FullName.EndsWith('/')) {
            [System.IO.Directory]::CreateDirectory($dest) | Out-Null
        } else {
            [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($dest)) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $dest, $true)
        }
    }
} finally { $archive.Dispose() }
if (-not (Test-Path "$DistDir\index.html")) { Fail "frontend\dist\index.html missing after extract" }
if (-not (Test-Path "$BackendDir\requirements.txt")) { Fail "backend\requirements.txt missing after extract" }
Log "  App build extracted to $BaseDir"

# ---------------------------------------------------------------------------
# 2. Write backend .env
# ---------------------------------------------------------------------------
Log ""
Log "=== Backend .env ==="
$EnvFile = "$BackendDir\.env"
if (Test-Path $EnvFile) {
    Log "  .env already exists - keeping existing (delete it to regenerate)"
} else {
    # Generate strong secrets
    $jwtSecret  = [Guid]::NewGuid().ToString("N") + [Guid]::NewGuid().ToString("N")
    # Fernet key: 32 random bytes, url-safe base64
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $keyBytes = New-Object byte[] 32
    $rng.GetBytes($keyBytes)
    $vaultKey = [Convert]::ToBase64String($keyBytes).Replace('+','-').Replace('/','_')
    $dbUrl = "postgresql://${PgUser}:${PgPassword}@localhost:${PgPort}/${PgDatabase}"

    $envText = @"
# AgentIQ backend environment - generated by DeployApp.ps1
DEV_JWT=dev-token-change-me
JWT_SECRET=$jwtSecret
ENVIRONMENT=production
DATABASE_URL=$dbUrl
CORS_ORIGINS=http://localhost
INGEST_MODE=offline
TRACKB_RUNNER_MODE=offline
CREDENTIAL_VAULT_KEY=$vaultKey
ANTHROPIC_API_KEY=$AnthropicApiKey
MODEL_GENERATION_PROVIDER=hosted
MODEL_EMBEDDING_PROVIDER=hosted
EMAIL_PROVIDER=smtp
"@
    [System.IO.File]::WriteAllText($EnvFile, $envText, $noBom)
    Log "  .env written (DATABASE_URL, generated JWT_SECRET + CREDENTIAL_VAULT_KEY)"
    if (-not $AnthropicApiKey) {
        Log "  NOTE: ANTHROPIC_API_KEY is blank - LLM enrichment uses deterministic fallbacks" "WARN"
    }
}

# ---------------------------------------------------------------------------
# 3. Python venv + packages
# ---------------------------------------------------------------------------
Log ""
Log "=== Python venv + packages ==="
$VenvDir = "$BackendDir\.venv"
$VenvPy  = "$VenvDir\Scripts\python.exe"
$VenvPip = "$VenvDir\Scripts\pip.exe"
if (-not (Test-Path $VenvPy)) {
    Log "  Creating venv ..."
    Invoke-Expression "$PyCmd -m venv `"$VenvDir`""
    if (-not (Test-Path $VenvPy)) { Fail "venv creation failed" }
}
& $VenvPip install --upgrade pip --quiet 2>&1 | ForEach-Object { Log "  $_" }
$ReqFile   = "$BackendDir\requirements.txt"
$WheelsDir = "$BackendDir\wheels"
if (Test-Path $WheelsDir) {
    Log "  Installing from bundled wheels ..."
    & $VenvPip install --no-index --find-links="$WheelsDir" -r "$ReqFile" --quiet 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) {
        Log "  Wheels failed - falling back to PyPI ..." "WARN"
        & $VenvPip install -r "$ReqFile" --quiet 2>&1 | ForEach-Object { Log "  $_" }
        if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
    }
} else {
    Log "  Installing from PyPI ..."
    & $VenvPip install -r "$ReqFile" --quiet 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
}
Log "  Backend packages installed"

# ---------------------------------------------------------------------------
# 4. Database setup (migrations + seed)
# ---------------------------------------------------------------------------
Log ""
Log "=== Database setup ==="
$dbScript = Join-Path $ScriptDir "SetupDatabase.ps1"
if (-not (Test-Path $dbScript)) { Fail "SetupDatabase.ps1 not found next to DeployApp.ps1" }
& powershell.exe -ExecutionPolicy Bypass -NoProfile -File $dbScript -BaseDir $BaseDir 2>&1 |
    ForEach-Object { Log "  $_" }
if ($LASTEXITCODE -ne 0) { Fail "Database setup failed" }
Log "  Database setup complete"

# ---------------------------------------------------------------------------
# 5. Backend service (NSSM -> uvicorn :8000)
# ---------------------------------------------------------------------------
Log ""
Log "=== Backend service ==="
$svcName = "AgentIQ-Backend"
$uvicorn = "$VenvDir\Scripts\uvicorn.exe"
if (-not (Test-Path $uvicorn)) { Fail "uvicorn not found at $uvicorn" }
if (Get-Service $svcName -ErrorAction SilentlyContinue) {
    Log "  Service exists - reconfiguring"
    Stop-Service $svcName -Force -ErrorAction SilentlyContinue
    & $NSSM remove $svcName confirm 2>$null | Out-Null
    Start-Sleep -Seconds 2
}
& $NSSM install $svcName $uvicorn "app.main:app --host 0.0.0.0 --port 8000" | Out-Null
& $NSSM set $svcName AppDirectory $BackendDir | Out-Null
& $NSSM set $svcName AppEnvironmentExtra "PYTHONPATH=$BackendDir" | Out-Null
& $NSSM set $svcName AppStdout "$LogDir\backend-stdout.log" | Out-Null
& $NSSM set $svcName AppStderr "$LogDir\backend-stderr.log" | Out-Null
& $NSSM set $svcName Start SERVICE_AUTO_START | Out-Null
Start-Service $svcName
Start-Sleep -Seconds 4
Log "  Backend service '$svcName' registered and started"

# ---------------------------------------------------------------------------
# 6. IIS site + web.config
# ---------------------------------------------------------------------------
Log ""
Log "=== IIS site ==="
Import-Module WebAdministration -ErrorAction SilentlyContinue

# web.config: /api reverse proxy to backend + SPA fallback. Requires ARR + URL
# Rewrite (installed by Prerequisites.ps1).
$WebConfig = "$DistDir\web.config"
@'
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <rule name="API Proxy" stopProcessing="true">
          <match url="^api/(.*)" />
          <action type="Rewrite" url="http://localhost:8000/api/{R:1}" />
        </rule>
        <rule name="SPA Fallback" stopProcessing="true">
          <match url=".*" />
          <conditions>
            <add input="{REQUEST_FILENAME}" matchType="IsFile"      negate="true" />
            <add input="{REQUEST_FILENAME}" matchType="IsDirectory" negate="true" />
          </conditions>
          <action type="Rewrite" url="/index.html" />
        </rule>
      </rules>
    </rewrite>
    <staticContent>
      <remove fileExtension=".woff2" />
      <mimeMap fileExtension=".woff2" mimeType="font/woff2" />
      <remove fileExtension=".json" />
      <mimeMap fileExtension=".json" mimeType="application/json" />
    </staticContent>
    <httpErrors existingResponse="PassThrough" />
    <defaultDocument>
      <files>
        <clear />
        <add value="index.html" />
      </files>
    </defaultDocument>
    <security>
      <requestFiltering allowDoubleEscaping="true" />
    </security>
  </system.webServer>
</configuration>
'@ | Set-Content $WebConfig -Encoding UTF8

$SiteName = "AgentIQ"
if (Get-Website -Name $SiteName -ErrorAction SilentlyContinue) {
    Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $DistDir
    Start-Website -Name $SiteName -ErrorAction SilentlyContinue
    Log "  IIS site '$SiteName' updated (physicalPath -> $DistDir)"
} else {
    if (Get-Website -Name "Default Web Site" -ErrorAction SilentlyContinue) {
        Stop-Website -Name "Default Web Site" -ErrorAction SilentlyContinue
    }
    New-Website -Name $SiteName -PhysicalPath $DistDir -Port 80 -Force | Out-Null
    Log "  IIS site '$SiteName' created on port 80"
}

# Ensure ARR reverse proxy is enabled
try {
    Set-WebConfigurationProperty -PSPath "MACHINE/WEBROOT/APPHOST" `
        -Filter "system.webServer/proxy" -Name "enabled" -Value $true
    Log "  ARR reverse proxy enabled"
} catch { Log "  Could not enable ARR proxy: $_ (is ARR installed?)" "WARN" }

& iisreset /restart /noforce 2>&1 | ForEach-Object { Log "  iisreset: $_" }

# ---------------------------------------------------------------------------
# 7. Health check
# ---------------------------------------------------------------------------
Log ""
Log "=== Health check ==="
Start-Sleep -Seconds 5
$ok = $false
for ($i=0; $i -lt 12; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
    Log "  Waiting for backend... ($i/12)"
    Start-Sleep -Seconds 5
}
if ($ok) { Log "  Backend /api/health OK" }
else { Log "  Backend health check failed - see $LogDir\backend-stderr.log" "WARN" }

try {
    $r2 = Invoke-WebRequest -Uri "http://localhost/api/health" -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
    Log "  IIS proxy /api/health -> HTTP $($r2.StatusCode)"
} catch { Log "  IIS proxy check: $_ (ARR/URL Rewrite may need a moment)" "WARN" }

Log ""
Log "==========================================================="
Log "  DEPLOYMENT COMPLETE."
Log "  Open http://localhost/ in a browser -> AgentIQ login."
Log "  Backend  : http://localhost:8000/api/health"
Log "  Services : AgentIQ-Backend, $($m.pgService)"
Log "  Config   : $EnvFile"
Log "  Logs     : $LogDir"
Log "==========================================================="
