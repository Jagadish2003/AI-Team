# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ — Install.ps1
# Called by the MSI as a deferred custom action (runs as SYSTEM, elevated).
# All steps are idempotent — re-running skips already-completed steps.
# ─────────────────────────────────────────────────────────────────────────────
param(
    [string]$InstallDir = "C:\AgentIQ\"
)

$ErrorActionPreference = "Stop"
$InstallDir  = $InstallDir.TrimEnd('\') + '\'
$LogDir      = "${InstallDir}logs"
$LogFile     = "$LogDir\install-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
$DownloadDir = "${InstallDir}downloads"
$BackendDir  = "${InstallDir}backend"
$FrontendDir = "${InstallDir}frontend"
$PostgresDir = "${InstallDir}postgres"
$ToolsDir    = "${InstallDir}tools"

# ── Logging ───────────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-Log {
    param([string]$Msg, [string]$Level = "INFO")
    $ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts][$Level] $Msg"
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    if ($Level -eq "ERROR") { Write-Error  $Msg }
    else                    { Write-Host   $line }
}

function Step { param([string]$Name) Write-Log "─── $Name ───" }
function Skip { param([string]$Msg)  Write-Log "  SKIP: $Msg" }
function OK   { param([string]$Msg)  Write-Log "  OK:   $Msg" }
function Fail { param([string]$Msg)  Write-Log $Msg "ERROR"; throw $Msg }

Write-Log "AgentIQ install started. InstallDir=$InstallDir"
Write-Log "Log: $LogFile"

# ── Download helper (bundle-first, internet fallback) ─────────────────────────
function Get-Installer {
    param([string]$Name, [string]$Url, [string]$OutFile)
    $bundled = "$DownloadDir\$Name"
    if (Test-Path $bundled) {
        Write-Log "  Using bundled: $Name"
        Copy-Item $bundled $OutFile -Force
        return
    }
    Write-Log "  Downloading $Name from $Url ..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    (New-Object Net.WebClient).DownloadFile($Url, $OutFile)
    OK "Downloaded $Name"
}

# ── URLs ──────────────────────────────────────────────────────────────────────
$URLS = @{
    Python      = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    ARR         = "https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi"
    URLRewrite  = "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi"
    NSSM        = "https://nssm.cc/release/nssm-2.24.zip"
    PostgreSQL  = "https://get.enterprisedb.com/postgresql/postgresql-16.3-1-windows-x64-binaries.zip"
    VCRedist    = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    ODBC        = "https://go.microsoft.com/fwlink/?linkid=2249006"
}

$Tmp = "$env:TEMP\AgentIQ-setup"
New-Item -ItemType Directory -Force $Tmp | Out-Null

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Extract application bundle
# ─────────────────────────────────────────────────────────────────────────────
Step "Extract application bundle"
$AppZip = "${InstallDir}AgentIQ-app.zip"
if (-not (Test-Path $AppZip)) { Fail "AgentIQ-app.zip not found at $AppZip" }

foreach ($dir in @($BackendDir, $FrontendDir, $PostgresDir, $ToolsDir)) {
    New-Item -ItemType Directory -Force $dir | Out-Null
}

Write-Log "  Extracting $AppZip ..."
Expand-Archive -Path $AppZip -DestinationPath $InstallDir -Force
OK "Bundle extracted"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Visual C++ Redistributable
# ─────────────────────────────────────────────────────────────────────────────
Step "Visual C++ Redistributable"
$VCKey = "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"
if ((Test-Path $VCKey) -and (Get-ItemProperty $VCKey -ErrorAction SilentlyContinue).Installed -eq 1) {
    Skip "VC++ Redistributable already installed"
} else {
    $VCInst = "$Tmp\vc_redist.x64.exe"
    Get-Installer "vc_redist.x64.exe" $URLS.VCRedist $VCInst
    Write-Log "  Installing VC++ Redistributable..."
    Start-Process $VCInst -ArgumentList "/install /quiet /norestart" -Wait -NoNewWindow
    OK "VC++ Redistributable installed"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Python 3.11
# ─────────────────────────────────────────────────────────────────────────────
Step "Python 3.11"
$PythonExe = "C:\Python311\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue)?.Source
}
$PythonVersion = & $PythonExe --version 2>&1
$NeedPython = (-not $PythonExe) -or ($PythonVersion -notmatch "3\.11\.")

if ($NeedPython) {
    $PyInst = "$Tmp\python-3.11.9-amd64.exe"
    Get-Installer "python-3.11.9-amd64.exe" $URLS.Python $PyInst
    Write-Log "  Installing Python 3.11.9 ..."
    Start-Process $PyInst `
        -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 TargetDir=C:\Python311 Include_test=0" `
        -Wait -NoNewWindow
    $PythonExe = "C:\Python311\python.exe"
    if (-not (Test-Path $PythonExe)) { Fail "Python installation failed" }
    OK "Python 3.11.9 installed at $PythonExe"
} else {
    Skip "Python already installed: $PythonVersion"
    $PythonExe = "C:\Python311\python.exe"
    if (-not (Test-Path $PythonExe)) {
        $PythonExe = (Get-Command python).Source
    }
}
$env:PATH = "C:\Python311;C:\Python311\Scripts;" + $env:PATH

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — IIS + required Windows features
# ─────────────────────────────────────────────────────────────────────────────
Step "IIS"
$IISFeatures = @(
    "Web-Server", "Web-WebServer", "Web-Common-Http",
    "Web-Default-Doc", "Web-Dir-Browsing", "Web-Http-Errors",
    "Web-Static-Content", "Web-Http-Redirect",
    "Web-Health", "Web-Http-Logging",
    "Web-Performance", "Web-Stat-Compression",
    "Web-Security", "Web-Filtering",
    "Web-Mgmt-Tools", "Web-Mgmt-Console",
    "Web-Scripting-Tools", "Web-Mgmt-Service"
)
$missing = $IISFeatures | Where-Object {
    (Get-WindowsFeature $_).InstallState -ne "Installed"
}
if ($missing.Count -eq 0) {
    Skip "IIS already installed"
} else {
    Write-Log "  Enabling IIS features: $($missing -join ', ')"
    Install-WindowsFeature -Name $missing -IncludeManagementTools | Out-Null
    OK "IIS enabled"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — IIS ARR (Application Request Routing)
# ─────────────────────────────────────────────────────────────────────────────
Step "IIS ARR"
$ARRKey = "HKLM:\SOFTWARE\Microsoft\IIS Extensions\Application Request Routing"
if (Test-Path $ARRKey) {
    Skip "IIS ARR already installed"
} else {
    $ARRInst = "$Tmp\requestRouter_amd64.msi"
    Get-Installer "requestRouter_amd64.msi" $URLS.ARR $ARRInst
    Write-Log "  Installing IIS ARR..."
    Start-Process msiexec -ArgumentList "/i `"$ARRInst`" /quiet /norestart" -Wait -NoNewWindow
    OK "IIS ARR installed"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — IIS URL Rewrite
# ─────────────────────────────────────────────────────────────────────────────
Step "IIS URL Rewrite"
$RWKey = "HKLM:\SOFTWARE\Microsoft\IIS Extensions\URL Rewrite"
if (Test-Path $RWKey) {
    Skip "URL Rewrite already installed"
} else {
    $RWInst = "$Tmp\rewrite_amd64_en-US.msi"
    Get-Installer "rewrite_amd64_en-US.msi" $URLS.URLRewrite $RWInst
    Write-Log "  Installing URL Rewrite..."
    Start-Process msiexec -ArgumentList "/i `"$RWInst`" /quiet /norestart" -Wait -NoNewWindow
    OK "URL Rewrite installed"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — ODBC Driver 18 (SQL Server connector)
# ─────────────────────────────────────────────────────────────────────────────
Step "ODBC Driver 18"
$ODBCKey = "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 18 for SQL Server"
if (Test-Path $ODBCKey) {
    Skip "ODBC Driver 18 already installed"
} else {
    $ODBCInst = "$Tmp\msodbcsql18_amd64.msi"
    Get-Installer "msodbcsql18_amd64.msi" $URLS.ODBC $ODBCInst
    Write-Log "  Installing ODBC Driver 18..."
    Start-Process msiexec `
        -ArgumentList "/i `"$ODBCInst`" /quiet /norestart IACCEPTMSODBCSQLLICENSETERMS=YES" `
        -Wait -NoNewWindow
    OK "ODBC Driver 18 installed"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — NSSM (service manager)
# ─────────────────────────────────────────────────────────────────────────────
Step "NSSM"
$NSSMExe = "$ToolsDir\nssm.exe"
if (Test-Path $NSSMExe) {
    Skip "NSSM already present"
} else {
    $NSSMZip = "$Tmp\nssm-2.24.zip"
    Get-Installer "nssm-2.24.zip" $URLS.NSSM $NSSMZip
    Write-Log "  Extracting NSSM..."
    Expand-Archive -Path $NSSMZip -DestinationPath $Tmp -Force
    Copy-Item "$Tmp\nssm-2.24\win64\nssm.exe" $NSSMExe -Force
    OK "NSSM placed at $NSSMExe"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 9 — PostgreSQL
# ─────────────────────────────────────────────────────────────────────────────
Step "PostgreSQL"
$PGDir     = "$PostgresDir\pgsql"
$PGData    = "$PostgresDir\data"
$PGBin     = "$PGDir\bin"
$PGService = "AgentIQ-PostgreSQL"
$PGPort    = 5433   # non-default port to avoid conflicts with existing PG installs
$PGUser    = "agentiq"
$PGPass    = "agentiq_local_$(([System.Guid]::NewGuid().ToString('N')).Substring(0,8))"
$PGPassFile = "${InstallDir}backend\.pgpass"

$PGSvc = Get-Service $PGService -ErrorAction SilentlyContinue
if ($PGSvc -and $PGSvc.Status -ne "Stopped") {
    Skip "PostgreSQL service '$PGService' already running"
} else {
    # Extract binaries if not present
    if (-not (Test-Path "$PGBin\postgres.exe")) {
        $PGZip = "$Tmp\postgresql-16-windows-x64-binaries.zip"
        Get-Installer "postgresql-16-windows-x64-binaries.zip" $URLS.PostgreSQL $PGZip
        Write-Log "  Extracting PostgreSQL binaries..."
        Expand-Archive -Path $PGZip -DestinationPath $PostgresDir -Force
        OK "PostgreSQL binaries extracted"
    }

    # VC++ runtime required by PG (already installed in Step 2)
    $env:PATH = "$PGBin;" + $env:PATH

    # Initialize data directory
    if (-not (Test-Path "$PGData\PG_VERSION")) {
        Write-Log "  Initialising PostgreSQL data directory..."
        New-Item -ItemType Directory -Force $PGData | Out-Null
        & "$PGBin\initdb.exe" -D $PGData -U postgres -E UTF8 --locale=en-US 2>&1 | Add-Content $LogFile
        OK "PostgreSQL data directory initialised"
    } else {
        Skip "PostgreSQL data directory already initialised"
    }

    # Adjust port in postgresql.conf
    $PGConf = "$PGData\postgresql.conf"
    (Get-Content $PGConf) -replace "^#?port\s*=.*", "port = $PGPort" |
        Set-Content $PGConf

    # Register as Windows Service
    if (-not (Get-Service $PGService -ErrorAction SilentlyContinue)) {
        Write-Log "  Registering PostgreSQL service..."
        & "$PGBin\pg_ctl.exe" register -N $PGService -D $PGData -o "-p $PGPort" 2>&1 | Add-Content $LogFile
    }
    Start-Service $PGService
    Start-Sleep -Seconds 5

    # Create agentiq user and database
    $env:PGPASSWORD = "postgres_admin"
    $CreateSQL = @"
CREATE USER $PGUser WITH PASSWORD '$PGPass';
CREATE DATABASE agentiq OWNER $PGUser;
GRANT ALL PRIVILEGES ON DATABASE agentiq TO $PGUser;
"@
    $CreateSQL | & "$PGBin\psql.exe" -U postgres -p $PGPort 2>&1 | Add-Content $LogFile

    # Run init.sql if present
    $InitSQL = "$PostgresDir\init.sql"
    if (Test-Path $InitSQL) {
        & "$PGBin\psql.exe" -U $PGUser -d agentiq -p $PGPort -f $InitSQL 2>&1 | Add-Content $LogFile
    }

    # Save generated password for backend .env
    $PGPass | Set-Content $PGPassFile -Encoding UTF8
    Write-Log "  PostgreSQL running on port $PGPort"
    OK "PostgreSQL configured"
}
# Read stored PG password for use in .env
if (Test-Path $PGPassFile) { $PGPass = (Get-Content $PGPassFile -Raw).Trim() }

# ─────────────────────────────────────────────────────────────────────────────
# STEP 10 — Python virtual environment + packages
# ─────────────────────────────────────────────────────────────────────────────
Step "Python backend environment"
$VenvDir   = "$BackendDir\.venv"
$PipExe    = "$VenvDir\Scripts\pip.exe"
$WheelsDir = "$BackendDir\wheels"

if (-not (Test-Path "$VenvDir\Scripts\python.exe")) {
    Write-Log "  Creating Python virtual environment..."
    & $PythonExe -m venv $VenvDir 2>&1 | Add-Content $LogFile
    OK "venv created"
} else {
    Skip "Virtual environment already exists"
}

# Upgrade pip silently
& $PipExe install --quiet --upgrade pip 2>&1 | Add-Content $LogFile

if (Test-Path $WheelsDir) {
    Write-Log "  Installing from bundled wheels (offline)..."
    & $PipExe install --quiet --no-index --find-links $WheelsDir -r "$BackendDir\requirements.txt" 2>&1 |
        Add-Content $LogFile
} else {
    Write-Log "  Installing from PyPI (wheels folder not found)..."
    & $PipExe install --quiet -r "$BackendDir\requirements.txt" 2>&1 | Add-Content $LogFile
}
OK "Python packages installed"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 11 — Write default backend .env
# ─────────────────────────────────────────────────────────────────────────────
Step "Backend .env"
$EnvFile = "$BackendDir\.env"
if (-not (Test-Path $EnvFile)) {
    $JwtSecret = [System.Convert]::ToBase64String(
        [System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    $DatabaseURL = "postgresql://${PGUser}:${PGPass}@localhost:${PGPort}/agentiq"
    @"
# AgentIQ configuration — edit this file then restart the AgentIQ-Backend service.

DEV_JWT=dev-token-change-me
JWT_SECRET=$JwtSecret
ENVIRONMENT=production

DATABASE_URL=$DatabaseURL
POSTGRES_DB=agentiq
POSTGRES_USER=$PGUser
POSTGRES_PASSWORD=$PGPass

CORS_ORIGINS=http://localhost,http://127.0.0.1
PUBLIC_HOSTNAME=http://localhost
AGENTIQ_BACKEND_URL=http://localhost

ANTHROPIC_API_KEY=
INGEST_MODE=offline
TRACKB_RUNNER_MODE=offline

CREDENTIAL_VAULT_KEY=
LICENSE_KEY=
"@ | Set-Content $EnvFile -Encoding UTF8
    OK "Default .env written to $EnvFile"
} else {
    Skip ".env already exists — keeping existing configuration"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 12 — Seed database (first run only)
# ─────────────────────────────────────────────────────────────────────────────
Step "Database seed"
$DevDB = "$BackendDir\database\dev.db"
if (-not (Test-Path $DevDB)) {
    Write-Log "  Seeding SQLite database..."
    $env:PYTHONPATH = $BackendDir
    & "$VenvDir\Scripts\python.exe" "$BackendDir\database\seed_loader.py" 2>&1 | Add-Content $LogFile
    OK "Database seeded"
} else {
    Skip "Database already seeded"
}

# Run Alembic migrations
Write-Log "  Running Alembic migrations..."
Push-Location $BackendDir
& "$VenvDir\Scripts\alembic.exe" upgrade head 2>&1 | Add-Content $LogFile
Pop-Location
OK "Migrations applied"

# ─────────────────────────────────────────────────────────────────────────────
# STEP 13 — Register backend Windows Service via NSSM
# ─────────────────────────────────────────────────────────────────────────────
Step "AgentIQ Backend service"
$SvcName  = "AgentIQ-Backend"
$PythonVenv = "$VenvDir\Scripts\python.exe"

$existingSvc = Get-Service $SvcName -ErrorAction SilentlyContinue
if ($existingSvc) {
    Skip "Service '$SvcName' already registered"
    if ($existingSvc.Status -ne "Running") { Start-Service $SvcName }
} else {
    Write-Log "  Registering $SvcName with NSSM..."
    & $NSSMExe install $SvcName $PythonVenv
    & $NSSMExe set $SvcName AppParameters "-m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    & $NSSMExe set $SvcName AppDirectory  $BackendDir
    & $NSSMExe set $SvcName AppEnvironmentExtra "PYTHONPATH=$BackendDir"
    & $NSSMExe set $SvcName AppStdout     "$LogDir\backend.log"
    & $NSSMExe set $SvcName AppStderr     "$LogDir\backend-error.log"
    & $NSSMExe set $SvcName AppRotateFiles 1
    & $NSSMExe set $SvcName AppRotateBytes 10485760
    & $NSSMExe set $SvcName Start         SERVICE_AUTO_START
    & $NSSMExe set $SvcName ObjectName    LocalSystem
    & $NSSMExe set $SvcName AppNoConsole  1
    & $NSSMExe set $SvcName DisplayName   "AgentIQ Backend API"
    & $NSSMExe set $SvcName Description   "AgentIQ FastAPI backend service"

    # Recovery: restart on failure
    & sc.exe failure $SvcName reset= 60 actions= restart/5000/restart/10000/restart/30000 | Out-Null

    Start-Service $SvcName
    OK "Service '$SvcName' registered and started"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 14 — Configure IIS site
# ─────────────────────────────────────────────────────────────────────────────
Step "IIS site"
Import-Module WebAdministration -ErrorAction SilentlyContinue

$SiteName  = "AgentIQ"
$DistDir   = "$FrontendDir\dist"
$WebConfig = "$DistDir\web.config"

# Write web.config (SPA fallback + ARR proxy to backend)
@'
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
        <!-- Proxy /api/* to backend FastAPI -->
        <rule name="API Proxy" stopProcessing="true">
          <match url="^api/(.*)" />
          <action type="Rewrite" url="http://localhost:8000/api/{R:1}" />
        </rule>
        <!-- SPA fallback: non-file requests → index.html -->
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
      <mimeMap fileExtension=".woff2" mimeType="font/woff2" />
      <mimeMap fileExtension=".json"  mimeType="application/json" />
    </staticContent>
    <httpErrors existingResponse="PassThrough" />
    <security>
      <requestFiltering allowDoubleEscaping="true" />
    </security>
  </system.webServer>
</configuration>
'@ | Set-Content $WebConfig -Encoding UTF8

# Enable proxy in ARR
$arrPropPath = "MACHINE/WEBROOT/APPHOST"
try {
    Set-WebConfigurationProperty `
        -PSPath $arrPropPath `
        -Filter "system.webServer/proxy" `
        -Name "enabled" -Value $true
} catch {
    Write-Log "  WARNING: Could not enable ARR proxy via cmdlet — may already be enabled: $_"
}

# Create or update IIS site
if (Get-Website -Name $SiteName -ErrorAction SilentlyContinue) {
    Skip "IIS site '$SiteName' already exists"
    Set-ItemProperty "IIS:\Sites\$SiteName" -Name physicalPath -Value $DistDir
} else {
    # Remove default site if it occupies port 80
    $defaultSite = Get-Website -Name "Default Web Site" -ErrorAction SilentlyContinue
    if ($defaultSite -and ($defaultSite.Bindings.Collection | Where-Object { $_.bindingInformation -match ":80:" })) {
        Stop-Website -Name "Default Web Site"
        Write-Log "  Stopped 'Default Web Site' to free port 80"
    }

    New-Website -Name $SiteName `
                -PhysicalPath $DistDir `
                -Port 80 `
                -Force | Out-Null

    # Enable anonymous authentication, disable Windows auth
    Set-WebConfigurationProperty `
        -Filter "system.webServer/security/authentication/anonymousAuthentication" `
        -PSPath "IIS:\Sites\$SiteName" -Name "enabled" -Value $true
    Set-WebConfigurationProperty `
        -Filter "system.webServer/security/authentication/windowsAuthentication" `
        -PSPath "IIS:\Sites\$SiteName" -Name "enabled" -Value $false

    Start-Website -Name $SiteName
    OK "IIS site '$SiteName' created on port 80"
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 15 — Windows Firewall rules
# ─────────────────────────────────────────────────────────────────────────────
Step "Firewall"
$FWRules = @(
    @{ Name="AgentIQ HTTP";    Port=80;   Proto="TCP" }
    @{ Name="AgentIQ HTTPS";   Port=443;  Proto="TCP" }
    @{ Name="AgentIQ Backend"; Port=8000; Proto="TCP"; Local=$true }
)
foreach ($rule in $FWRules) {
    if (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue) {
        Skip "Firewall rule '$($rule.Name)' already exists"
    } else {
        $params = @{
            DisplayName = $rule.Name
            Direction   = "Inbound"
            Protocol    = $rule.Proto
            LocalPort   = $rule.Port
            Action      = "Allow"
            Profile     = "Any"
        }
        if ($rule.Local) { $params["RemoteAddress"] = "LocalSubnet" }
        New-NetFirewallRule @params | Out-Null
        OK "Firewall rule '$($rule.Name)' added"
    }
}

# ─────────────────────────────────────────────────────────────────────────────
# STEP 16 — Health check
# ─────────────────────────────────────────────────────────────────────────────
Step "Health check"
Write-Log "  Waiting for backend to start (up to 90s)..."
$deadline = (Get-Date).AddSeconds(90)
$backendOK = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8000/docs" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $backendOK = $true; break }
    } catch { }
    Start-Sleep -Seconds 5
    Write-Host "." -NoNewline
}
Write-Host ""
if ($backendOK) { OK "Backend is healthy at http://localhost:8000" }
else            { Write-Log "  WARNING: backend did not respond within 90s — check $LogDir\backend-error.log" "WARN" }

Write-Log "  Waiting for frontend (IIS) to respond..."
$frontendOK = $false
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost/" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -lt 500) { $frontendOK = $true; break }
    } catch { }
    Start-Sleep -Seconds 3
}
if ($frontendOK) { OK "Frontend is healthy at http://localhost" }
else             { Write-Log "  WARNING: IIS did not respond within 30s" "WARN" }

# ─────────────────────────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────────────────────────
Write-Log "══════════════════════════════════════════════════════════"
Write-Log "  AgentIQ installation complete."
Write-Log "  URL         : http://localhost"
Write-Log "  Backend API : http://localhost:8000/docs"
Write-Log "  Config file : $EnvFile"
Write-Log "  Log dir     : $LogDir"
Write-Log "  To reconfigure: run $($InstallDir)scripts\Configure.ps1"
Write-Log "══════════════════════════════════════════════════════════"
