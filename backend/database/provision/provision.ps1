<#
.SYNOPSIS
    Provision the complete AgentIQ schema onto a target PostgreSQL database
    (Alembic migrations + core {id,payload} tables + lazy-only tables + seed).

.DESCRIPTION
    Maintained provisioning path. Idempotent and safe to re-run. Assumes the
    role and database already exist (run 00_create_role_and_db.sql once as a
    superuser first).

.EXAMPLE
    $env:DATABASE_URL = "postgresql://agentiq:secret@db-host:5432/agentiq"
    .\provision.ps1

.EXAMPLE
    .\provision.ps1 -DatabaseUrl "postgresql://agentiq:secret@db-host:5432/agentiq" -NoSeed
#>
param(
    [string]$DatabaseUrl = $env:DATABASE_URL,
    [switch]$NoSeed
)

$ErrorActionPreference = "Stop"

# backend/ is two levels up from this script (provision -> database -> backend).
$BackendDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if ($DatabaseUrl) { $env:DATABASE_URL = $DatabaseUrl }
if (-not $env:DATABASE_URL) {
    throw "DATABASE_URL is not set. Pass -DatabaseUrl or set the env var."
}

$pythonArgs = @((Join-Path $PSScriptRoot "provision_schema.py"))
if ($NoSeed) { $pythonArgs += "--no-seed" }

Push-Location $BackendDir
try {
    python @pythonArgs
    if ($LASTEXITCODE -ne 0) { throw "provision_schema.py exited with code $LASTEXITCODE" }
}
finally {
    Pop-Location
}
