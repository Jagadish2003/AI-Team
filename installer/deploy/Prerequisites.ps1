# -----------------------------------------------------------------------------
# AgentIQ - Prerequisites.ps1
# Prepares a fresh Windows Server 2025 for AgentIQ by installing every
# prerequisite from the internet using OFFICIAL installers. Run this FIRST,
# as Administrator. When it finishes the server is ready; deploy the app build
# afterwards with DeployApp.ps1.
#
# Installs / configures:
#   1  IIS + required features
#   2  VC++ 2022 Redistributable
#   3  URL Rewrite 2.1              (IIS module)
#   4  Application Request Routing  (IIS reverse-proxy module) + enable proxy
#   5  ODBC Driver 18 for SQL Server
#   6  PostgreSQL 16 (official EDB installer, port 5433) + agentiq DB & user
#   7  Python 3.11.9 (official installer, all users, on PATH)
#   8  NSSM (service manager, used later to run the backend)
#   9  Firewall rules (80 / 443)
#
# Writes a manifest to C:\AgentIQ\prereqs.json that DeployApp.ps1 reads.
# DB credentials are saved to C:\AgentIQ\db-credentials.txt.
#
# Usage (as Administrator):
#   powershell -ExecutionPolicy Bypass -File Prerequisites.ps1
#   powershell -ExecutionPolicy Bypass -File Prerequisites.ps1 -PgPort 5433
# -----------------------------------------------------------------------------
param(
    [int]$PgPort         = 5433,
    [string]$BaseDir     = "C:\AgentIQ",
    [string]$PgPassword  = "",   # agentiq DB user password (auto-generated if blank)
    [string]$PgSuperPassword = "" # postgres superuser password (auto-generated if blank)
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$LogFile    = "C:\Windows\Temp\AgentIQ-prereqs-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
$Transcript = "C:\Windows\Temp\AgentIQ-prereqs-transcript-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
try { Start-Transcript -Path $Transcript -Force } catch {}

$Tmp     = "$env:TEMP\AgentIQ-prereqs"
$ToolsDir = "$BaseDir\tools"
New-Item -ItemType Directory -Force $Tmp      | Out-Null
New-Item -ItemType Directory -Force $BaseDir  | Out-Null
New-Item -ItemType Directory -Force $ToolsDir | Out-Null

# ---------------------------------------------------------------------------
# Step tracking + logging
# ---------------------------------------------------------------------------
$script:StepList    = [System.Collections.Generic.List[hashtable]]::new()
$script:StepCurrent = ""

function Write-Log {
    param([string]$Msg, [string]$Level = "INFO")
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')][$Level] $Msg"
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
    if ($Level -eq "ERROR") { Write-Error $Msg -ErrorAction Continue } else { Write-Host $line }
}
function Step { param([string]$N)
    $script:StepCurrent = $N
    $script:StepList.Add(@{ Name = $N; Status = "running"; Start = (Get-Date) })
    Write-Log ""; Write-Log "=== $N ==="
}
function _Mark { param([string]$S)
    for ($i = $script:StepList.Count - 1; $i -ge 0; $i--) {
        if ($script:StepList[$i].Name -eq $script:StepCurrent) {
            $st = $script:StepList[$i].Start
            $dur = if ($null -ne $st) { [Math]::Round(((Get-Date) - $st).TotalSeconds) } else { $script:StepList[$i].Secs }
            $script:StepList[$i] = @{ Name = $script:StepCurrent; Status = $S; Secs = $dur }; break
        }
    }
}
function OK   { param([string]$M) Write-Log "  OK:   $M"; _Mark "done" }
function Skip { param([string]$M) Write-Log "  SKIP: $M"; _Mark "skip" }
function Fail { param([string]$M) _Mark "FAIL"; Write-Summary; Write-Log $M "ERROR"; throw $M }

function Write-Summary {
    Write-Log ""
    Write-Log "========== PREREQUISITE STEP SUMMARY =========="
    $d=0;$s=0;$f=0;$p=0
    foreach ($x in $script:StepList) {
        $icon = switch ($x.Status) { "done" {"[OK  ]"} "skip" {"[SKIP]"} "FAIL" {"[FAIL]"} "running" {"[ABRT]"} default {"[?   ]"} }
        $dur = if ($null -ne $x.Secs) { " ($($x.Secs)s)" } else { "" }
        Write-Log "  $icon $($x.Name)$dur"
        switch ($x.Status) { "done" {$d++} "skip" {$s++} "FAIL" {$f++} "running" {$p++} }
    }
    Write-Log "  Completed : $d  Skipped : $s  Failed : $f  Aborted : $p"
    Write-Log "==============================================="
}

function Get-File {
    param([string]$Name, [string]$Url, [string]$OutFile)
    Write-Log "  Downloading $Name ..."
    try {
        $wc = New-Object Net.WebClient
        $wc.Headers.Add("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AgentIQ-Installer")
        $wc.DownloadFile($Url, $OutFile)
        Write-Log "  Downloaded: $Name ($([Math]::Round((Get-Item $OutFile).Length/1MB,1)) MB)"
    } catch { Fail "Download failed for $Name : $_" }
}

# ---------------------------------------------------------------------------
# Download URLs (official sources)
# ---------------------------------------------------------------------------
$URL = @{
    VCRedist   = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    URLRewrite = "https://download.microsoft.com/download/1/2/8/128E2E22-C1B9-44A4-BE2A-5859ED1D4592/rewrite_amd64_en-US.msi"
    ARR        = "https://download.microsoft.com/download/E/9/8/E9849D6A-020E-47E4-9FD0-A023E99B54EB/requestRouter_amd64.msi"
    ODBC       = "https://go.microsoft.com/fwlink/?linkid=2249006"
    PostgreSQL = "https://get.enterprisedb.com/postgresql/postgresql-16.3-1-windows-x64.exe"
    Python     = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
    NSSM       = "https://nssm.cc/release/nssm-2.24.zip"
}

# Auto-generate passwords if not supplied
if (-not $PgPassword)      { $PgPassword      = "aiq_" + [Guid]::NewGuid().ToString("N").Substring(0,24) }
if (-not $PgSuperPassword) { $PgSuperPassword = "pg_"  + [Guid]::NewGuid().ToString("N").Substring(0,24) }

Write-Log "AgentIQ Prerequisites installer started."
Write-Log "Log        : $LogFile"
Write-Log "Base dir   : $BaseDir"
Write-Log "PG port    : $PgPort"
Write-Log "OS         : $([System.Environment]::OSVersion.VersionString)"
Write-Log "PowerShell : $($PSVersionTable.PSVersion)"

# Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Fail "Must be run as Administrator." }

# =============================================================================
# 1. IIS + features
# =============================================================================
Step "IIS features"
Import-Module ServerManager -ErrorAction SilentlyContinue
$feats = @(
    "Web-Server","Web-WebServer","Web-Common-Http","Web-Default-Doc",
    "Web-Http-Errors","Web-Static-Content","Web-Http-Logging",
    "Web-Stat-Compression","Web-Filtering","Web-Mgmt-Console","Web-Mgmt-Tools"
)
$missing = $feats | Where-Object {
    $f = Get-WindowsFeature $_ -ErrorAction SilentlyContinue
    -not $f -or $f.InstallState -ne "Installed"
}
if ($missing.Count -eq 0) {
    Skip "IIS features already installed"
} else {
    Write-Log "  Enabling: $($missing -join ', ')"
    Install-WindowsFeature -Name $missing -IncludeManagementTools | Out-Null
    OK "IIS features enabled"
}

# =============================================================================
# 2. VC++ 2022 Redistributable
# =============================================================================
Step "VC++ 2022 Redistributable"
if (Test-Path "C:\Windows\System32\MSVCP140.dll") {
    Skip "VC++ 2022 already installed"
} else {
    $vc = "$Tmp\vc_redist.x64.exe"
    Get-File "vc_redist.x64.exe" $URL.VCRedist $vc
    $p = Start-Process $vc -ArgumentList "/install /quiet /norestart" -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -in @(0,1638,3010,1641)) { OK "VC++ installed (exit $($p.ExitCode))" }
    else { Fail "VC++ install failed (exit $($p.ExitCode))" }
}

# =============================================================================
# 3. URL Rewrite 2.1  (install BEFORE ARR)
# =============================================================================
Step "URL Rewrite"
if (Test-Path "HKLM:\SOFTWARE\Microsoft\IIS Extensions\URL Rewrite") {
    Skip "URL Rewrite already installed"
} else {
    $m = "$Tmp\rewrite_amd64_en-US.msi"
    Get-File "URL Rewrite 2.1" $URL.URLRewrite $m
    $p = Start-Process msiexec.exe -ArgumentList "/i `"$m`" /qn /norestart" -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -in @(0,3010,1638)) { OK "URL Rewrite installed (exit $($p.ExitCode))" }
    else { Fail "URL Rewrite install failed (exit $($p.ExitCode))" }
}

# =============================================================================
# 4. Application Request Routing + enable proxy
# =============================================================================
Step "ARR (reverse proxy)"
Import-Module WebAdministration -ErrorAction SilentlyContinue
$arrMod = $null
try { $arrMod = Get-WebGlobalModule -Name "ApplicationRequestRouting*" -ErrorAction SilentlyContinue } catch {}
if ($null -ne $arrMod -and $arrMod.Count -gt 0) {
    Skip "ARR already installed"
} else {
    $m = "$Tmp\requestRouter_amd64.msi"
    Get-File "ARR 3.0" $URL.ARR $m
    $p = Start-Process msiexec.exe -ArgumentList "/i `"$m`" /qn /norestart" -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -in @(0,3010,1638)) { OK "ARR installed (exit $($p.ExitCode))" }
    else { Write-Log "  ARR exit $($p.ExitCode) - continuing" "WARN"; _Mark "skip" }
}

# Enable the reverse proxy at the server level
try {
    Set-WebConfigurationProperty -PSPath "MACHINE/WEBROOT/APPHOST" `
        -Filter "system.webServer/proxy" -Name "enabled" -Value $true
    Write-Log "  ARR reverse proxy enabled"
} catch {
    Write-Log "  Could not enable ARR proxy yet: $_ (DeployApp will retry)" "WARN"
}

# =============================================================================
# 5. ODBC Driver 18 for SQL Server
# =============================================================================
Step "ODBC Driver 18"
if ((Test-Path "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 18 for SQL Server") -or
    (Test-Path "HKLM:\SOFTWARE\ODBC\ODBCINST.INI\ODBC Driver 17 for SQL Server")) {
    Skip "ODBC Driver already installed"
} else {
    $m = "$Tmp\msodbcsql18.msi"
    Get-File "ODBC Driver 18" $URL.ODBC $m
    $p = Start-Process msiexec.exe `
        -ArgumentList "/i `"$m`" /qn /norestart IACCEPTMSODBCSQLLICENSETERMS=YES" `
        -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -in @(0,3010,1638)) { OK "ODBC Driver 18 installed (exit $($p.ExitCode))" }
    else { Write-Log "  ODBC exit $($p.ExitCode) - non-fatal" "WARN"; _Mark "skip" }
}

# =============================================================================
# 6. PostgreSQL 16 (official EDB installer) + agentiq DB & user
# =============================================================================
Step "PostgreSQL 16"
$pgService = "postgresql-x64-16"
$pgBase    = "C:\Program Files\PostgreSQL\16"
$pgBin     = "$pgBase\bin"

$existing = Get-Service $pgService -ErrorAction SilentlyContinue
if ($null -ne $existing -and (Test-Path "$pgBin\psql.exe")) {
    Skip "PostgreSQL 16 already installed (service '$pgService')"
} else {
    $pgExe = "$Tmp\postgresql-16-windows-x64.exe"
    Get-File "PostgreSQL 16.3 installer" $URL.PostgreSQL $pgExe
    Write-Log "  Installing PostgreSQL 16 unattended (port $PgPort) ..."
    $args = @(
        "--mode","unattended",
        "--unattendedmodeui","minimal",
        "--superpassword",$PgSuperPassword,
        "--serverport","$PgPort",
        "--servicename",$pgService,
        "--enable-components","server,commandlinetools",
        "--disable-components","stackbuilder"
    )
    $p = Start-Process $pgExe -ArgumentList $args -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -ne 0) { Fail "PostgreSQL installer failed (exit $($p.ExitCode))" }
    Start-Sleep -Seconds 5
    if (-not (Test-Path "$pgBin\psql.exe")) { Fail "PostgreSQL install did not produce psql.exe at $pgBin" }
    OK "PostgreSQL 16 installed (service '$pgService', port $PgPort)"
}

# Ensure service is running
$svc = Get-Service $pgService -ErrorAction SilentlyContinue
if ($null -ne $svc -and $svc.Status -ne "Running") {
    Start-Service $pgService; Start-Sleep -Seconds 4
}

# Wait for readiness
Step "PostgreSQL ready + database"
$ready = $false
for ($i=0; $i -lt 30; $i++) {
    & "$pgBin\pg_isready.exe" -p $PgPort 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) { Fail "PostgreSQL not accepting connections on port $PgPort" }

# Create agentiq user + database (idempotent) using the superuser
$env:PGPASSWORD = $PgSuperPassword
$userExists = & "$pgBin\psql.exe" -U postgres -p $PgPort -d postgres -tAc `
    "SELECT 1 FROM pg_roles WHERE rolname='agentiq';" 2>&1
if ($userExists -match "^1") {
    Write-Log "  Role 'agentiq' already exists - updating password"
    & "$pgBin\psql.exe" -U postgres -p $PgPort -d postgres `
        -c "ALTER USER agentiq WITH PASSWORD '$PgPassword';" 2>&1 | Out-Null
} else {
    & "$pgBin\psql.exe" -U postgres -p $PgPort -d postgres `
        -c "CREATE USER agentiq WITH PASSWORD '$PgPassword';" 2>&1 | Out-Null
    Write-Log "  Role 'agentiq' created"
}
$dbExists = & "$pgBin\psql.exe" -U postgres -p $PgPort -d postgres -tAc `
    "SELECT 1 FROM pg_database WHERE datname='agentiq';" 2>&1
if ($dbExists -match "^1") {
    Write-Log "  Database 'agentiq' already exists"
} else {
    & "$pgBin\psql.exe" -U postgres -p $PgPort -d postgres `
        -c "CREATE DATABASE agentiq OWNER agentiq;" 2>&1 | Out-Null
    Write-Log "  Database 'agentiq' created"
}
# Enable uuid-ossp on the agentiq DB (required by ORM models - see
# docker/postgres/init.sql). CREATE EXTENSION needs superuser, so we do it here
# as 'postgres' while we hold the superuser password.
& "$pgBin\psql.exe" -U postgres -p $PgPort -d agentiq `
    -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>&1 | Out-Null
Write-Log "  Extension uuid-ossp ensured on 'agentiq'"
OK "PostgreSQL database + user + extension ready"

# =============================================================================
# 7. Python 3.11.9
# =============================================================================
Step "Python 3.11"
function Find-Py311 {
    try { if ((& py -3.11 --version 2>&1) -match "3\.11") { return "py -3.11" } } catch {}
    foreach ($c in @("C:\Program Files\Python311\python.exe","C:\Python311\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    return $null
}
if (Find-Py311) {
    Skip "Python 3.11 already installed"
} else {
    $py = "$Tmp\python-3.11.9-amd64.exe"
    Get-File "Python 3.11.9" $URL.Python $py
    $p = Start-Process $py -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait -PassThru -NoNewWindow
    if ($p.ExitCode -notin @(0,3010)) { Fail "Python install failed (exit $($p.ExitCode))" }
    $env:PATH = [Environment]::GetEnvironmentVariable("PATH","Machine") + ";" +
                [Environment]::GetEnvironmentVariable("PATH","User")
    if (-not (Find-Py311)) { Fail "Python 3.11 not found after install" }
    OK "Python 3.11.9 installed"
}

# =============================================================================
# 8. NSSM (service manager for the backend)
# =============================================================================
Step "NSSM"
$nssmExe = "$ToolsDir\nssm.exe"
if (Test-Path $nssmExe) {
    Skip "NSSM already present at $nssmExe"
} else {
    $zip = "$Tmp\nssm-2.24.zip"
    Get-File "NSSM 2.24" $URL.NSSM $zip
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $arch = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $entry = $arch.Entries | Where-Object { $_.FullName -like "*win64/nssm.exe" } | Select-Object -First 1
        if ($null -eq $entry) { Fail "nssm.exe (win64) not found in NSSM zip" }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $nssmExe, $true)
    } finally { $arch.Dispose() }
    OK "NSSM extracted to $nssmExe"
}

# =============================================================================
# 9. Firewall rules
# =============================================================================
Step "Firewall"
foreach ($r in @(
    @{ Name="AgentIQ HTTP";  Port=80  },
    @{ Name="AgentIQ HTTPS"; Port=443 }
)) {
    if (-not (Get-NetFirewallRule -DisplayName $r.Name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $r.Name -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $r.Port | Out-Null
        Write-Log "  Added firewall rule '$($r.Name)' (port $($r.Port))"
    }
}
OK "Firewall rules ready"

# =============================================================================
# Write manifest + credentials for DeployApp.ps1
# =============================================================================
Step "Write manifest"
$pyCmd = Find-Py311
$manifest = [ordered]@{
    baseDir      = $BaseDir
    pgService    = $pgService
    pgBin        = $pgBin
    pgPort       = $PgPort
    pgUser       = "agentiq"
    pgDatabase   = "agentiq"
    pgPassword   = $PgPassword
    pythonCmd    = $pyCmd
    nssmExe      = $nssmExe
    generatedUtc = (Get-Date).ToUniversalTime().ToString("o")
}
$noBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText("$BaseDir\prereqs.json",
    ($manifest | ConvertTo-Json), $noBom)

$cred = @"
AgentIQ Prerequisite Credentials
Generated (UTC): $((Get-Date).ToUniversalTime().ToString("o"))

PostgreSQL service   : $pgService
PostgreSQL bin       : $pgBin
Port                 : $PgPort

Superuser (postgres) : $PgSuperPassword
App user             : agentiq
App user password    : $PgPassword
App database         : agentiq
Connection string    : postgresql://agentiq:$PgPassword@localhost:$PgPort/agentiq

Python               : $pyCmd
NSSM                 : $nssmExe

Keep this file secure. DeployApp.ps1 reads $BaseDir\prereqs.json.
"@
[System.IO.File]::WriteAllText("$BaseDir\db-credentials.txt", $cred, $noBom)
OK "Manifest written to $BaseDir\prereqs.json"

# =============================================================================
# Done
# =============================================================================
Write-Summary
$failed = ($script:StepList | Where-Object { $_.Status -eq "FAIL" }).Count
Write-Log ""
if ($failed -eq 0) {
    Write-Log "==========================================================="
    Write-Log "  PREREQUISITES READY."
    Write-Log "  PostgreSQL : service '$pgService' on port $PgPort"
    Write-Log "  Python     : $pyCmd"
    Write-Log "  Credentials: $BaseDir\db-credentials.txt"
    Write-Log "  Manifest   : $BaseDir\prereqs.json"
    Write-Log ""
    Write-Log "  NEXT: deploy the application build with DeployApp.ps1"
    Write-Log "==========================================================="
} else {
    Write-Log "  Prerequisites finished with $failed FAILED step(s). Review the log."
}
try { Stop-Transcript } catch {}
