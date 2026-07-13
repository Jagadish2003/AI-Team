# -----------------------------------------------------------------------------
# AgentIQ - Enable-Https.ps1
# Imports an SSL certificate (PFX) and switches the AgentIQ IIS site to HTTPS:
#   1. Imports the PFX into LocalMachine\My
#   2. Adds a *:443 binding to the AgentIQ site (answers any hostname)
#   3. Binds the certificate with netsh http add sslcert (more reliable than
#      the WebAdministration AddSslCertificate method)
#   4. Rewrites web.config: adds an HTTP->HTTPS redirect rule ahead of the
#      existing rules (health endpoint exempt so plain-HTTP probes keep working)
#   5. Adds https origins to backend .env CORS and restarts services
#
# Run as Administrator (prompts for the PFX password):
#   powershell -ExecutionPolicy Bypass -File Enable-Https.ps1 `
#       -PfxPath C:\Temp\ssl\agentiq.pfx -Fqdn aiq.example.com
#
# Non-interactive (password as SecureString):
#   $pw = ConvertTo-SecureString "..." -AsPlainText -Force
#   .\Enable-Https.ps1 -PfxPath C:\Temp\ssl\agentiq.pfx -PfxPassword $pw -Fqdn aiq.example.com
#
# To build the PFX from Let's Encrypt style PEM files (needs Git's openssl):
#   & "C:\Program Files\Git\usr\bin\openssl.exe" pkcs12 -export `
#       -out agentiq.pfx -inkey privkey.pem -in fullchain.pem -passout pass:YOURPASS
# -----------------------------------------------------------------------------
param(
    [Parameter(Mandatory=$true)][string]$PfxPath,
    [SecureString]$PfxPassword,  # prompted for when omitted
    [string]$Fqdn = "",          # informational + CORS entry; binding covers all names
    [string]$InstallDir = "C:\AgentIQ"
)

if (-not $PfxPassword) { $PfxPassword = Read-Host "PFX password" -AsSecureString }

$ErrorActionPreference = "Stop"
$InstallDir = $InstallDir.TrimEnd('\')
$SiteName   = "AgentIQ"
$DistDir    = "$InstallDir\frontend\dist"
$EnvFile    = "$InstallDir\backend\.env"

function Say { param([string]$M, [string]$C = "White") Write-Host "  $M" -ForegroundColor $C }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this script as Administrator." }
if (-not (Test-Path $PfxPath)) { throw "PFX not found: $PfxPath" }

# ── 1. Import certificate ────────────────────────────────────────────────────
Say "Importing certificate..."
$cert = Import-PfxCertificate -FilePath $PfxPath -CertStoreLocation "Cert:\LocalMachine\My" -Password $PfxPassword
Say "Imported: $($cert.Subject)  thumbprint $($cert.Thumbprint)" "Green"
Say "Valid until: $($cert.NotAfter)"

# ── 2. IIS 443 binding (no host header -> answers any name, incl. localhost) ─
Import-Module WebAdministration
$existing = Get-WebBinding -Name $SiteName -Protocol https -ErrorAction SilentlyContinue
if (-not $existing) {
    New-WebBinding -Name $SiteName -Protocol https -Port 443 -IPAddress "*"
    Say "Added https *:443 binding to site '$SiteName'" "Green"
} else {
    Say "https binding already present"
}

# ── 3. Attach the certificate via netsh (authoritative for HTTP.SYS) ─────────
# Remove any stale binding first, then add ours. AppId is arbitrary-but-stable.
$appId = "{a1b2c3d4-0000-4a4a-9b9b-aa11bb22cc33}"
netsh http delete sslcert ipport=0.0.0.0:443 2>$null | Out-Null
$out = netsh http add sslcert ipport=0.0.0.0:443 certhash=$($cert.Thumbprint) appid=$appId certstorename=MY 2>&1
if ($LASTEXITCODE -ne 0 -and $out -notmatch "already exists") { throw "netsh add sslcert failed: $out" }
Say "Certificate bound to 0.0.0.0:443" "Green"

# ── 4. web.config: HTTP -> HTTPS redirect (health check exempt) ──────────────
$wcPath = "$DistDir\web.config"
$wc = Get-Content $wcPath -Raw
if ($wc -notmatch "Redirect to HTTPS") {
    $redirect = @"
<rule name="Redirect to HTTPS" stopProcessing="true">
          <match url="(.*)" />
          <conditions>
            <add input="{HTTPS}" pattern="off" />
            <add input="{URL}" pattern="^/api/health" negate="true" />
          </conditions>
          <action type="Redirect" url="https://{HTTP_HOST}/{R:1}" redirectType="Permanent" />
        </rule>

"@
    $wc = $wc -replace "(<rules>\s*)", "`$1$redirect"
    Set-Content $wcPath -Value $wc -Encoding UTF8
    Say "web.config: HTTP->HTTPS redirect rule added (before API proxy / SPA rules)" "Green"
} else {
    Say "web.config: redirect rule already present"
}

# ── 5. Backend CORS + restart ─────────────────────────────────────────────────
$envText = Get-Content $EnvFile -Raw
$origins = "http://localhost,https://localhost"
if ($Fqdn) { $origins += ",http://$Fqdn,https://$Fqdn" }
$envText = $envText -replace "CORS_ORIGINS=.*", "CORS_ORIGINS=$origins"
if ($Fqdn) { $envText = $envText -replace "PUBLIC_HOSTNAME=.*", "PUBLIC_HOSTNAME=https://$Fqdn" }
Set-Content $EnvFile -Value $envText -Encoding UTF8
Restart-Service AgentIQ-Backend -Force
iisreset /restart | Out-Null
Say "Backend + IIS restarted" "Green"

# ── 6. Verify ─────────────────────────────────────────────────────────────────
# Probe with curl.exe (SChannel), NOT Invoke-WebRequest: the .NET Framework
# client in Windows PowerShell 5.1 fails the TLS handshake against ECDSA
# certificates (e.g. Let's Encrypt EC keys) even though browsers, curl and
# openssl all succeed - it reports a false negative.
Start-Sleep -Seconds 12
$code = & "$env:SystemRoot\System32\curl.exe" -sk -o NUL -w "%{http_code}" "https://localhost/api/health"
Say ""
if ($code -eq "200") {
    Say "HTTPS is working: https://localhost/api/health -> 200" "Green"
    if ($Fqdn) { Say "Browse: https://$Fqdn/" "Green" }
    Say "Plain http:// requests now redirect (301) to https."
} else {
    Say "HTTPS probe returned '$code'" "Red"
    Say "Check: netsh http show sslcert ipport=0.0.0.0:443" "Yellow"
}
