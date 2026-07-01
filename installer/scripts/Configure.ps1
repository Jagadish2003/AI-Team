# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ — Configure.ps1
# Interactive configuration wizard. Launched from Start Menu shortcut.
# Writes C:\AgentIQ\backend\.env and restarts the backend service.
# ─────────────────────────────────────────────────────────────────────────────
param([string]$InstallDir = "C:\AgentIQ\")

$InstallDir = $InstallDir.TrimEnd('\') + '\'
$EnvFile    = "${InstallDir}backend\.env"

# Must run as admin to restart the service
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()`
    ).IsInRole([Security.Principal.WindowsBuiltInRole]"Administrator")) {
    Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

function Prompt-Value {
    param([string]$Label, [string]$Default, [switch]$Secret)
    if ($Secret) {
        $input = Read-Host "  $Label [$('*' * [Math]::Min(8,$Default.Length))]"
    } else {
        $input = Read-Host "  $Label [$Default]"
    }
    if ([string]::IsNullOrWhiteSpace($input)) { return $Default }
    return $input.Trim()
}

# Read current values
$current = @{}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | Where-Object { $_ -match "^[^#].*=.*" } | ForEach-Object {
        $k, $v = $_ -split "=", 2
        $current[$k.Trim()] = $v.Trim()
    }
}

Clear-Host
Write-Host ""
Write-Host "══════════════════════════════════════════════════════════"
Write-Host "  AgentIQ — Configuration Wizard"
Write-Host "  File: $EnvFile"
Write-Host "  Press Enter to keep current value shown in [ ]."
Write-Host "══════════════════════════════════════════════════════════"

Write-Host "`n  ── Security ──────────────────────────────────────────────"
$DEV_JWT       = Prompt-Value "Dev API Token"        ($current["DEV_JWT"]        ?? "dev-token-change-me")
$JWT_SECRET    = Prompt-Value "JWT Secret Key"       ($current["JWT_SECRET"]     ?? "change-me") -Secret

Write-Host "`n  ── AI / LLM (leave blank for offline mode) ────────────────"
$ANTHROPIC_KEY = Prompt-Value "Anthropic API Key"    ($current["ANTHROPIC_API_KEY"] ?? "")
$INGEST_MODE   = if ($ANTHROPIC_KEY) { "online" } else { "offline" }

Write-Host "`n  ── Connectors (OAuth — leave blank to skip) ──────────────"
$SF_ID         = Prompt-Value "Salesforce Client ID"     ($current["SALESFORCE_CLIENT_ID"]     ?? "")
$SF_SECRET     = Prompt-Value "Salesforce Client Secret" ($current["SALESFORCE_CLIENT_SECRET"] ?? "") -Secret
$SN_ID         = Prompt-Value "ServiceNow Client ID"     ($current["SERVICENOW_CLIENT_ID"]     ?? "")
$SN_SECRET     = Prompt-Value "ServiceNow Client Secret" ($current["SERVICENOW_CLIENT_SECRET"] ?? "") -Secret
$JIRA_ID       = Prompt-Value "Jira Client ID"           ($current["JIRA_CLIENT_ID"]           ?? "")
$JIRA_SECRET   = Prompt-Value "Jira Client Secret"       ($current["JIRA_CLIENT_SECRET"]       ?? "") -Secret
$GH_ID         = Prompt-Value "GitHub Client ID"         ($current["GITHUB_CLIENT_ID"]         ?? "")
$GH_SECRET     = Prompt-Value "GitHub Client Secret"     ($current["GITHUB_CLIENT_SECRET"]     ?? "") -Secret

Write-Host "`n  ── License ────────────────────────────────────────────────"
$LICENSE_KEY   = Prompt-Value "License Key"          ($current["LICENSE_KEY"] ?? "")

Write-Host "`n  ── SMTP (optional) ────────────────────────────────────────"
$SMTP_HOST     = Prompt-Value "SMTP Host"            ($current["SMTP_HOST"]     ?? "")
$SMTP_PORT     = Prompt-Value "SMTP Port"            ($current["SMTP_PORT"]     ?? "587")
$SMTP_USER     = Prompt-Value "SMTP Username"        ($current["SMTP_USERNAME"] ?? "")
$SMTP_PASS     = Prompt-Value "SMTP Password"        ($current["SMTP_PASSWORD"] ?? "") -Secret

# Preserve database/postgres settings from existing .env
$DB_URL  = $current["DATABASE_URL"]     ?? ""
$PG_PASS = $current["POSTGRES_PASSWORD"] ?? ""
$VAULT   = $current["CREDENTIAL_VAULT_KEY"] ?? ""

# Write updated .env
@"
# AgentIQ configuration — edited by Configure.ps1 on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')

DEV_JWT=$DEV_JWT
JWT_SECRET=$JWT_SECRET
ENVIRONMENT=production

DATABASE_URL=$DB_URL
POSTGRES_DB=agentiq
POSTGRES_USER=agentiq
POSTGRES_PASSWORD=$PG_PASS

CREDENTIAL_VAULT_KEY=$VAULT

CORS_ORIGINS=http://localhost,http://127.0.0.1
PUBLIC_HOSTNAME=http://localhost
AGENTIQ_BACKEND_URL=http://localhost

ANTHROPIC_API_KEY=$ANTHROPIC_KEY
INGEST_MODE=$INGEST_MODE
TRACKB_RUNNER_MODE=offline

OAUTH_REDIRECT_URI=http://localhost/api/connectors/oauth/callback
SALESFORCE_CLIENT_ID=$SF_ID
SALESFORCE_CLIENT_SECRET=$SF_SECRET
SERVICENOW_CLIENT_ID=$SN_ID
SERVICENOW_CLIENT_SECRET=$SN_SECRET
JIRA_CLIENT_ID=$JIRA_ID
JIRA_CLIENT_SECRET=$JIRA_SECRET
GITHUB_CLIENT_ID=$GH_ID
GITHUB_CLIENT_SECRET=$GH_SECRET

EMAIL_PROVIDER=smtp
EMAIL_FROM=noreply@example.com
EMAIL_FROM_NAME=AgentIQ
SMTP_HOST=$SMTP_HOST
SMTP_PORT=$SMTP_PORT
SMTP_USERNAME=$SMTP_USER
SMTP_PASSWORD=$SMTP_PASS
SMTP_USE_STARTTLS=true

LICENSE_KEY=$LICENSE_KEY
"@ | Set-Content $EnvFile -Encoding UTF8

Write-Host ""
Write-Host "  Configuration saved."

# Restart backend service
$svc = Get-Service "AgentIQ-Backend" -ErrorAction SilentlyContinue
if ($svc) {
    Write-Host "  Restarting AgentIQ-Backend service..."
    Restart-Service "AgentIQ-Backend" -Force
    Write-Host "  Service restarted."
}

Write-Host ""
Write-Host "══════════════════════════════════════════════════════════"
Write-Host "  Done. AgentIQ is available at http://localhost"
Write-Host "══════════════════════════════════════════════════════════"
Write-Host ""
pause
