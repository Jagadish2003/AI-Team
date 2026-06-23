#!/usr/bin/env bash
# =============================================================================
# AgentIQ — Environment Setup Wizard
# Creates /opt/agstore/.env interactively, section by section.
#
# Usage:
#   bash setup-env.sh                         # writes to /opt/agstore/.env
#   bash setup-env.sh --output /custom/.env   # write to a different path
#   bash setup-env.sh --skip-optional         # skip connectors/email/db sections
# =============================================================================
set -euo pipefail

trap 'printf "\n\n  ${R}Interrupted.${N}\n"; exit 130' INT

# ── CLI args ─────────────────────────────────────────────────────────────────
OUT_FILE="/opt/agstore/.env"
SKIP_OPT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output|-o)    OUT_FILE="$2"; shift 2 ;;
    --skip-optional) SKIP_OPT=1; shift ;;
    -h|--help)
      echo "Usage: $0 [--output /path/to/.env] [--skip-optional]"
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Colors (only when writing to a real terminal) ─────────────────────────────
if [[ -t 1 ]]; then
  R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m'
  C='\033[0;36m' B='\033[1;34m' DIM='\033[2m' BOLD='\033[1m' N='\033[0m'
else
  R='' G='' Y='' C='' B='' DIM='' BOLD='' N=''
fi

# ── State ─────────────────────────────────────────────────────────────────────
declare -A VALS
SEC_NUM=0
SEC_TOTAL=7
[[ "$SKIP_OPT" == "1" ]] && SEC_TOTAL=4

# ── Helpers ───────────────────────────────────────────────────────────────────

section() {
  local label="$1"
  ((SEC_NUM++)) || true
  printf "\n${B}  ┌─ %-30s [%d/%d] ──────────────────${N}\n\n" "$label" "$SEC_NUM" "$SEC_TOTAL"
}

# ask KEY LABEL DEFAULT HINT REQUIRED(0|1) SECRET(0|1)
ask() {
  local key="$1" label="$2" default="${3:-}" hint="${4:-}"
  local req="${5:-0}" secret="${6:-0}"

  local req_mark=""
  [[ "$req" == "1" ]] && req_mark=" ${R}*${N}"

  printf "  ${BOLD}%s${N}%b\n" "$label" "$req_mark"
  [[ -n "$hint" ]] && printf "  ${DIM}%s${N}\n" "$hint"

  local prompt="  "
  [[ -n "$default" ]] && prompt+="${DIM}[${default}]${N} > " || prompt+="> "

  local val
  while true; do
    if [[ "$secret" == "1" ]]; then
      IFS= read -r -s -p "$(printf "%b" "$prompt")" val; echo
    else
      IFS= read -r -p "$(printf "%b" "$prompt")" val
    fi
    [[ -z "$val" && -n "$default" ]] && val="$default"
    if [[ "$req" == "1" && -z "$val" ]]; then
      printf "  ${R}This field is required — cannot be empty.${N}\n"
      continue
    fi
    break
  done
  VALS["$key"]="$val"
  echo
}

# ask_sel KEY LABEL DEFAULT opt1 opt2 ...
ask_sel() {
  local key="$1" label="$2" default="$3"; shift 3
  local opts=("$@")

  printf "  ${BOLD}%s${N}  ${DIM}(default: %s)${N}\n" "$label" "$default"
  for i in "${!opts[@]}"; do
    local mark="    "
    [[ "${opts[$i]}" == "$default" ]] && mark="  ${G}▶${N} "
    printf "%b${DIM}%d${N}) %s\n" "$mark" "$((i+1))" "${opts[$i]}"
  done
  printf "  > "

  local val
  IFS= read -r val
  if [[ -z "$val" ]]; then
    val="$default"
  elif [[ "$val" =~ ^[0-9]+$ ]] && (( val >= 1 && val <= ${#opts[@]} )); then
    val="${opts[$((val-1))]}"
  fi
  VALS["$key"]="$val"
  echo
}

# skip_section — fills all remaining keys with empty without prompting
# Called when user chooses to skip an optional section
skip_section() {
  for key in "$@"; do
    VALS["$key"]=""
  done
}

mask() {
  local k="$1" v="$2"
  local upper="${k^^}"
  for kw in PASSWORD SECRET KEY TOKEN; do
    if [[ "$upper" == *"$kw"* && ${#v} -gt 6 ]]; then
      echo "${v:0:4}…${v: -3}"; return
    fi
  done
  echo "$v"
}

write_kv() {
  # write_kv KEY — writes KEY=<stored value or empty>
  echo "${1}=${VALS[$1]:-}"
}

# ── Banner ────────────────────────────────────────────────────────────────────
clear 2>/dev/null || true
printf "${C}${BOLD}"
echo   "  ╔══════════════════════════════════════════════════╗"
echo   "  ║         AgentIQ  Environment  Setup              ║"
echo   "  ╚══════════════════════════════════════════════════╝"
printf "${N}"
printf "  Output  : ${C}%s${N}\n" "$OUT_FILE"
printf "  ${DIM}Required fields marked ${R}*${N}${DIM} — press Enter to accept [default]${N}\n"
[[ "$SKIP_OPT" == "1" ]] && printf "  ${Y}--skip-optional: connectors / email / DB connector sections will be blank${N}\n"

if [[ -f "$OUT_FILE" ]]; then
  printf "\n  ${Y}WARNING: %s already exists.${N}\n" "$OUT_FILE"
  printf "  Overwrite? [y/N] "
  read -r yn
  [[ "${yn,,}" != "y" ]] && { echo "  Aborted."; exit 0; }
fi
echo

# ═════════════════════════════════════════════════════════════════════════════
# 1. DATABASE
# ═════════════════════════════════════════════════════════════════════════════
section "Database"

ask PROD_DATABASE_URL \
  "Production Database URL" \
  "postgresql://agentiq:agentiq_secret@agentiq-postgres:5432/agentiqprod" \
  "Full connection string — user/password must match POSTGRES_USER/PASSWORD below" 1 0

ask POSTGRES_USER \
  "Postgres User" "agentiq" \
  "Must match username in PROD_DATABASE_URL"

ask POSTGRES_PASSWORD \
  "Postgres Password" "agentiq_secret" \
  "Must match password in PROD_DATABASE_URL" 0 1

ask POSTGRES_DB \
  "Postgres Database" "agentiqprod" \
  "Must match the database name in PROD_DATABASE_URL"

# ═════════════════════════════════════════════════════════════════════════════
# 2. CORE / SERVER
# ═════════════════════════════════════════════════════════════════════════════
section "Core / Server"

ask_sel ENVIRONMENT "Environment" "development" "development" "production"
ask_sel INGEST_MODE "Ingest Mode" "offline" "offline" "live"
ask_sel TRACKB_RUNNER_MODE "Track B Runner Mode" "offline" "offline" "subprocess" "in_process"

ask DEV_JWT \
  "Dev JWT Token" "dev-token-change-me" \
  "Bearer token for local auth — change before any shared deploy"

ask JWT_SECRET \
  "JWT Secret" "" \
  "HS256 signing key. Generate: openssl rand -hex 32" 1 1

ask CORS_ORIGINS \
  "CORS Origins" "http://localhost:5173" \
  "Comma-separated allowed origins"

# ═════════════════════════════════════════════════════════════════════════════
# 3. SECURITY & LICENSING
# ═════════════════════════════════════════════════════════════════════════════
section "Security & Licensing"

ask CREDENTIAL_VAULT_KEY \
  "Credential Vault Key" "" \
  "Fernet key for OAuth token encryption at rest.
     Generate: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"" 1 1

ask LICENSE_KEY \
  "License Key" "" \
  "Signed license key from CloudFulcrum. Leave empty for unlicensed (read-only) install."

ask LICENSE_CHECK_INTERVAL_HOURS \
  "License Check Interval (hours)" "12" ""

# ═════════════════════════════════════════════════════════════════════════════
# 4. LLM
# ═════════════════════════════════════════════════════════════════════════════
section "LLM"

ask ANTHROPIC_API_KEY \
  "Anthropic API Key" "" \
  "Powers LLM enrichment and entity extraction. Optional — deterministic fallbacks run without it." 0 1

# ═════════════════════════════════════════════════════════════════════════════
# 5. OAUTH / CONNECTORS  (optional — can skip)
# ═════════════════════════════════════════════════════════════════════════════
OAUTH_KEYS=(
  OAUTH_REDIRECT_URI OAUTH_FRONTEND_BASE_URL
  OAUTH_HTTP_TIMEOUT_SECONDS REFRESH_THRESHOLD_SECONDS
  SALESFORCE_CLIENT_ID SALESFORCE_CLIENT_SECRET
  SERVICENOW_CLIENT_ID SERVICENOW_CLIENT_SECRET
  ATLASSIAN_CLIENT_ID JIRA_CLIENT_SECRET CONFLUENCE_CLIENT_SECRET
  GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET
  SLACK_CLIENT_ID SLACK_CLIENT_SECRET
  SAP_CLIENT_ID SAP_CLIENT_SECRET
  DYNAMICS365_CLIENT_ID DYNAMICS365_CLIENT_SECRET
)

if [[ "$SKIP_OPT" == "1" ]]; then
  skip_section "${OAUTH_KEYS[@]}"
else
  section "OAuth / Connectors"
  printf "  ${DIM}Press Enter to leave any connector blank if not used.${N}\n\n"

  ask OAUTH_REDIRECT_URI "OAuth Redirect URI" "" \
    "e.g. https://your-domain/api/connectors/oauth/callback"
  ask OAUTH_FRONTEND_BASE_URL "Frontend Base URL" "" \
    "Backend redirects here after OAuth. Leave empty for same-origin deploys."
  ask OAUTH_HTTP_TIMEOUT_SECONDS "OAuth HTTP Timeout (s)" "30" ""
  ask REFRESH_THRESHOLD_SECONDS "Token Refresh Threshold (s)" "300" \
    "Seconds before token expiry to trigger auto-refresh"

  printf "  ${DIM}── Connector credentials ──────────────────────────────────${N}\n\n"
  ask SALESFORCE_CLIENT_ID     "Salesforce Client ID"     "" ""
  ask SALESFORCE_CLIENT_SECRET "Salesforce Client Secret" "" "" 0 1
  ask SERVICENOW_CLIENT_ID     "ServiceNow Client ID"     "" ""
  ask SERVICENOW_CLIENT_SECRET "ServiceNow Client Secret" "" "" 0 1
  ask ATLASSIAN_CLIENT_ID      "Atlassian Client ID"      "" "Shared by Jira and Confluence"
  ask JIRA_CLIENT_SECRET       "Jira Client Secret"       "" "" 0 1
  ask CONFLUENCE_CLIENT_SECRET "Confluence Client Secret" "" "" 0 1
  ask GITHUB_CLIENT_ID         "GitHub Client ID"         "" ""
  ask GITHUB_CLIENT_SECRET     "GitHub Client Secret"     "" "" 0 1
  ask SLACK_CLIENT_ID          "Slack Client ID"          "" ""
  ask SLACK_CLIENT_SECRET      "Slack Client Secret"      "" "" 0 1
  ask SAP_CLIENT_ID            "SAP Client ID"            "" ""
  ask SAP_CLIENT_SECRET        "SAP Client Secret"        "" "" 0 1
  ask DYNAMICS365_CLIENT_ID    "Dynamics 365 Client ID"   "" ""
  ask DYNAMICS365_CLIENT_SECRET "Dynamics 365 Client Secret" "" "" 0 1
fi

# ═════════════════════════════════════════════════════════════════════════════
# 6. EMAIL / SMTP  (optional — can skip)
# ═════════════════════════════════════════════════════════════════════════════
EMAIL_KEYS=(
  EMAIL_PROVIDER EMAIL_FROM EMAIL_FROM_NAME
  SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_PASSWORD SMTP_USE_STARTTLS
  PUBLIC_HOSTNAME AGENTIQ_BACKEND_URL AGENTIQ_ADMIN_EMAIL
)

if [[ "$SKIP_OPT" == "1" ]]; then
  skip_section "${EMAIL_KEYS[@]}"
else
  section "Email / SMTP"
  printf "  ${DIM}Transactional email for invites and org approvals. Leave SMTP_HOST blank to disable.${N}\n\n"

  ask_sel EMAIL_PROVIDER "Email Provider" "smtp" "smtp"
  ask EMAIL_FROM      "From Address"   "noreply@cloudfulcrum.com" ""
  ask EMAIL_FROM_NAME "From Name"      "AgentIQ"                  ""
  ask SMTP_HOST       "SMTP Host"      "" "e.g. smtp.office365.com — leave blank to disable email"
  ask SMTP_PORT       "SMTP Port"      "587" ""
  ask SMTP_USERNAME   "SMTP Username"  "" "" 0 0
  ask SMTP_PASSWORD   "SMTP Password"  "" "" 0 1
  ask_sel SMTP_USE_STARTTLS "Use STARTTLS" "true" "true" "false"
  ask PUBLIC_HOSTNAME      "Public Hostname"  "" "Base URL for invite/reset links  e.g. https://app.agentiq.example"
  ask AGENTIQ_BACKEND_URL  "Backend URL"      "" "External backend URL for admin links  e.g. https://api.agentiq.example"
  ask AGENTIQ_ADMIN_EMAIL  "Admin Email"      "" "Receives new org approval request emails"
fi

# ═════════════════════════════════════════════════════════════════════════════
# 7. DB CONNECTORS  (optional — can skip)
# ═════════════════════════════════════════════════════════════════════════════
DB_KEYS=(
  ORACLE_HOST ORACLE_PORT ORACLE_DATABASE ORACLE_DB_USERNAME ORACLE_DB_PASSWORD
  POSTGRESQL_HOST POSTGRESQL_PORT POSTGRESQL_DATABASE POSTGRESQL_USERNAME POSTGRESQL_PASSWORD
)

if [[ "$SKIP_OPT" == "1" ]]; then
  skip_section "${DB_KEYS[@]}"
else
  section "DB Connectors (optional)"
  printf "  ${DIM}Oracle and PostgreSQL native signal ingestors — separate from the app database.${N}\n"
  printf "  ${DIM}Press Enter to leave all blank if not used.${N}\n\n"

  ask ORACLE_HOST          "Oracle Host"          "" ""
  ask ORACLE_PORT          "Oracle Port"          "1521" ""
  ask ORACLE_DATABASE      "Oracle Database"      "ORCL" ""
  ask ORACLE_DB_USERNAME   "Oracle Username"      "" ""
  ask ORACLE_DB_PASSWORD   "Oracle Password"      "" "" 0 1
  ask POSTGRESQL_HOST      "PG Connector Host"    "" "Native PostgreSQL ingestor — separate from the app DB"
  ask POSTGRESQL_PORT      "PG Connector Port"    "5432" ""
  ask POSTGRESQL_DATABASE  "PG Connector Database" "postgres" ""
  ask POSTGRESQL_USERNAME  "PG Connector Username" "" ""
  ask POSTGRESQL_PASSWORD  "PG Connector Password" "" "" 0 1
fi

# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY + CONFIRM
# ═════════════════════════════════════════════════════════════════════════════
echo
printf "${B}  ┌─ Summary ───────────────────────────────────────────────────────${N}\n"

filled=0
for k in "${!VALS[@]}"; do
  [[ -n "${VALS[$k]}" ]] && ((filled++)) || true
done

printf "  Fields filled  : ${G}%d${N}\n" "$filled"
printf "  Output file    : ${C}%s${N}\n\n" "$OUT_FILE"

# Print filled values (mask secrets)
for k in PROD_DATABASE_URL POSTGRES_USER POSTGRES_DB JWT_SECRET \
         CREDENTIAL_VAULT_KEY ANTHROPIC_API_KEY OAUTH_REDIRECT_URI \
         EMAIL_FROM SMTP_HOST; do
  v="${VALS[$k]:-}"
  [[ -z "$v" ]] && continue
  printf "  ${DIM}%-38s${N}= ${C}%s${N}\n" "$k" "$(mask "$k" "$v")"
done

echo
printf "  Write to ${C}%s${N}? [Y/n] " "$OUT_FILE"
read -r confirm
if [[ "${confirm,,}" == "n" ]]; then
  printf "  ${Y}Aborted — nothing written.${N}\n"
  exit 0
fi

# ═════════════════════════════════════════════════════════════════════════════
# WRITE FILE
# ═════════════════════════════════════════════════════════════════════════════
mkdir -p "$(dirname "$OUT_FILE")"

{
printf "# =============================================================================\n"
printf "# AgentIQ — Environment Configuration\n"
printf "# Generated by AgentIQ Setup Wizard on %s\n" "$(date -u '+%Y-%m-%d %H:%M UTC')"
printf "# =============================================================================\n"
echo ""
echo "# Database"
for k in PROD_DATABASE_URL POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB; do
  write_kv "$k"
done
echo ""
echo "# Core / Server"
for k in ENVIRONMENT INGEST_MODE TRACKB_RUNNER_MODE DEV_JWT JWT_SECRET CORS_ORIGINS; do
  write_kv "$k"
done
echo ""
echo "# Security & Licensing"
for k in CREDENTIAL_VAULT_KEY LICENSE_KEY LICENSE_CHECK_INTERVAL_HOURS; do
  write_kv "$k"
done
echo ""
echo "# LLM"
write_kv ANTHROPIC_API_KEY
echo ""
echo "# OAuth / Connectors"
for k in OAUTH_REDIRECT_URI OAUTH_FRONTEND_BASE_URL OAUTH_HTTP_TIMEOUT_SECONDS \
  REFRESH_THRESHOLD_SECONDS \
  SALESFORCE_CLIENT_ID SALESFORCE_CLIENT_SECRET \
  SERVICENOW_CLIENT_ID SERVICENOW_CLIENT_SECRET \
  ATLASSIAN_CLIENT_ID JIRA_CLIENT_SECRET CONFLUENCE_CLIENT_SECRET \
  GITHUB_CLIENT_ID GITHUB_CLIENT_SECRET \
  SLACK_CLIENT_ID SLACK_CLIENT_SECRET \
  SAP_CLIENT_ID SAP_CLIENT_SECRET \
  DYNAMICS365_CLIENT_ID DYNAMICS365_CLIENT_SECRET; do
  write_kv "$k"
done
echo ""
echo "# Email / SMTP"
for k in EMAIL_PROVIDER EMAIL_FROM EMAIL_FROM_NAME \
  SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_PASSWORD SMTP_USE_STARTTLS \
  PUBLIC_HOSTNAME AGENTIQ_BACKEND_URL AGENTIQ_ADMIN_EMAIL; do
  write_kv "$k"
done
echo ""
echo "# DB Connectors"
for k in ORACLE_HOST ORACLE_PORT ORACLE_DATABASE ORACLE_DB_USERNAME ORACLE_DB_PASSWORD \
  POSTGRESQL_HOST POSTGRESQL_PORT POSTGRESQL_DATABASE POSTGRESQL_USERNAME POSTGRESQL_PASSWORD; do
  write_kv "$k"
done
} > "$OUT_FILE"

chmod 600 "$OUT_FILE"

# ── Done ─────────────────────────────────────────────────────────────────────
printf "\n  ${G}${BOLD}✓ Created %s${N}\n" "$OUT_FILE"
printf "  ${DIM}Permissions : 600 (owner read/write only)${N}\n"
printf "\n  ${DIM}To use with AgentIQ:${N}\n"
printf "  ${C}  cp %s /opt/aiqstore/backend/.env${N}\n" "$OUT_FILE"
printf "  ${C}  bash /opt/agentiq/AgentIQ/start.sh${N}\n"
echo
