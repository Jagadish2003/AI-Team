#!/usr/bin/env bash
# =============================================================================
# AgentIQ — FQDN / URL Configuration
# Creates /opt/aiqtestdir/.env with URL variables and static config lines.
#
# Behaviour:
#   - /opt/aiqtestdir/ created if it does not exist
#   - .env created if it does not exist
#   - If .env exists and all values match  → no action, exits cleanly
#   - If .env exists and values differ     → shows diff, warns, asks before overwriting
#
# Usage: bash scripts/set-fqdn.sh
# =============================================================================
set -euo pipefail

TARGET_DIR="/opt/aiqtestdir"
TARGET_FILE="$TARGET_DIR/.env"

# ── Colors ────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  G='\033[0;32m' R='\033[0;31m' Y='\033[1;33m'
  C='\033[0;36m' BOLD='\033[1m' DIM='\033[2m' N='\033[0m'
else
  G='' R='' Y='' C='' BOLD='' DIM='' N=''
fi

# ── Banner ────────────────────────────────────────────────────────────────────
printf "${C}${BOLD}"
echo   "  ╔════════════════════════════════════════════╗"
echo   "  ║     AgentIQ — FQDN / URL Configuration    ║"
echo   "  ╚════════════════════════════════════════════╝"
printf "${N}"
printf "  Output : ${C}%s${N}\n\n" "$TARGET_FILE"

# ── FQDN validation ───────────────────────────────────────────────────────────
# Returns 0 (valid domain) or 1 (IP address or invalid format)
is_valid_domain() {
  local input="$1"

  # Strip any accidental http:// or https:// prefix
  input="${input#http://}"
  input="${input#https://}"
  # Strip trailing slashes or paths
  input="${input%%/*}"

  # Reject pure IPv4
  if [[ "$input" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
    return 1
  fi

  # Reject if contains colons (IPv6 or port — strip port first)
  local host="${input%%:*}"
  if [[ "$host" == *:* ]]; then
    return 1
  fi
  input="$host"

  # Must contain at least one dot
  if [[ "$input" != *.* ]]; then
    return 1
  fi

  # Only valid domain characters: letters, digits, hyphens, dots
  if [[ ! "$input" =~ ^[a-zA-Z0-9]([a-zA-Z0-9._-]*[a-zA-Z0-9])?$ ]]; then
    return 1
  fi

  # Must not start or end with a hyphen or dot in any label
  if [[ "$input" =~ (^|\.)- ]] || [[ "$input" =~ -(\.| ) ]]; then
    return 1
  fi

  return 0
}

# Strip protocol prefix from user input and return clean FQDN
clean_fqdn() {
  local input="$1"
  input="${input#http://}"
  input="${input#https://}"
  input="${input%%/*}"
  input="${input%%:*}"   # strip port if any
  echo "$input"
}

# ── Step 1: Prompt for FQDN ───────────────────────────────────────────────────
printf "  ${BOLD}Frontend domain name${N}  ${DIM}(e.g. app.example.com — not an IP address)${N}\n"
while true; do
  printf "  > "
  IFS= read -r raw_fqdn
  raw_fqdn="${raw_fqdn// /}"
  if [[ -z "$raw_fqdn" ]]; then
    printf "  ${R}Domain cannot be empty.${N}\n"
    continue
  fi
  FQDN=$(clean_fqdn "$raw_fqdn")
  if is_valid_domain "$FQDN"; then
    break
  else
    printf "  ${R}Invalid: '%s' is not a valid domain name.${N}\n" "$raw_fqdn"
    printf "  ${DIM}Enter a fully-qualified domain like  app.example.com  or  agentiq.mycompany.org${N}\n"
  fi
done

# ── Step 2: Prompt for protocol ───────────────────────────────────────────────
echo ""
printf "  ${BOLD}Protocol${N}\n"
printf "    1) http\n"
printf "    2) https\n"
while true; do
  printf "  Choice [1/2]: "
  IFS= read -r proto_choice
  case "$proto_choice" in
    1) SCHEME="http";  break ;;
    2) SCHEME="https"; break ;;
    *) printf "  ${R}Enter 1 for http or 2 for https.${N}\n" ;;
  esac
done

# ── Step 3: SMTP details ──────────────────────────────────────────────────────
echo ""
printf "  ${BOLD}SMTP details${N}  ${DIM}(press Enter to leave blank)${N}\n"
printf "  SMTP_HOST           : "; IFS= read -r NEW_SMTP_HOST
printf "  SMTP_PORT           : "; IFS= read -r NEW_SMTP_PORT
printf "  SMTP_USERNAME       : "; IFS= read -r NEW_SMTP_USERNAME
printf "  SMTP_PASSWORD       : "; IFS= read -rs NEW_SMTP_PASSWORD; echo ""
echo ""
printf "  SMTP_USE_STARTTLS   [true/false, Enter=blank]: "
while true; do
  IFS= read -r starttls_in
  case "$starttls_in" in
    true|false|"") NEW_SMTP_USE_STARTTLS="$starttls_in"; break ;;
    *) printf "  ${R}Enter true, false, or press Enter to skip.${N}\n  SMTP_USE_STARTTLS   [true/false, Enter=blank]: " ;;
  esac
done
printf "  EMAIL_FROM          : "; IFS= read -r NEW_EMAIL_FROM
printf "  EMAIL_FROM_NAME     : "; IFS= read -r NEW_EMAIL_FROM_NAME
printf "  AGENTIQ_ADMIN_EMAIL : "; IFS= read -r NEW_AGENTIQ_ADMIN_EMAIL

# ── Step 4: Anthropic API Key ─────────────────────────────────────────────────
echo ""
printf "  ${BOLD}Anthropic API Key${N}  ${DIM}(press Enter to leave blank)${N}\n"
printf "  ANTHROPIC_API_KEY   : "; IFS= read -rs NEW_ANTHROPIC_API_KEY; echo ""

# ── Step 5: Salesforce details ────────────────────────────────────────────────
echo ""
printf "  ${BOLD}Salesforce details${N}  ${DIM}(press Enter to leave blank)${N}\n"
printf "  SALESFORCE_INSTANCE      : "; IFS= read -r NEW_SALESFORCE_INSTANCE
printf "  SALESFORCE_CLIENT_ID     : "; IFS= read -r NEW_SALESFORCE_CLIENT_ID
printf "  SALESFORCE_CLIENT_SECRET : "; IFS= read -rs NEW_SALESFORCE_CLIENT_SECRET; echo ""

# ── Step 6: ServiceNow details ────────────────────────────────────────────────
echo ""
printf "  ${BOLD}ServiceNow details${N}  ${DIM}(press Enter to leave blank)${N}\n"
printf "  SERVICENOW_INSTANCE      : "; IFS= read -r NEW_SERVICENOW_INSTANCE
printf "  SERVICENOW_CLIENT_ID     : "; IFS= read -r NEW_SERVICENOW_CLIENT_ID
printf "  SERVICENOW_CLIENT_SECRET : "; IFS= read -rs NEW_SERVICENOW_CLIENT_SECRET; echo ""

# ── Step 7: Jira details ──────────────────────────────────────────────────────
echo ""
printf "  ${BOLD}Jira details${N}  ${DIM}(press Enter to leave blank)${N}\n"
printf "  JIRA_CLIENT_ID     : "; IFS= read -r NEW_JIRA_CLIENT_ID
printf "  JIRA_CLIENT_SECRET : "; IFS= read -rs NEW_JIRA_CLIENT_SECRET; echo ""

# ── Build values ──────────────────────────────────────────────────────────────
BASE_URL="${SCHEME}://${FQDN}"

# Dynamic (from prompts)
NEW_OAUTH_FRONTEND_BASE_URL="$BASE_URL"
NEW_CORS_ORIGINS="$BASE_URL"
NEW_PUBLIC_HOSTNAME="$BASE_URL"
NEW_AGENTIQ_BACKEND_URL="$BASE_URL"
NEW_OAUTH_REDIRECT_URI="${BASE_URL}/api/connectors/oauth/callback"

# Static (fixed — written as-is with comments)
NEW_INGEST_MODE="live"
NEW_TRACKB_RUNNER_MODE="in_process"
NEW_PROD_DATABASE_URL="postgresql://aiqprodusr:iW18nhBs9dMrUl@agentiq-postgres:5432/agentiqprod"
NEW_DATABASE_URL="postgresql://aiqprodusr:iW18nhBs9dMrUl@agentiq-postgres:5432/agentiqprod"
NEW_OAUTH_CALLBACK_ALLOW_UNAUTH="1"
NEW_CREDENTIAL_VAULT_KEY="dAAmXyxZFyZ4H1J8My722jOP4hUGPScLuEMX86tM9SI="
NEW_INFERRED_RELATIONSHIPS_ENABLED="true"
NEW_EMAIL_PROVIDER="smtp"
NEW_LICENSE_API_URL="https://qbusfbie3c.execute-api.us-east-1.amazonaws.com/prod/license"

echo ""
printf "  ${BOLD}Values to write${N}\n"
printf "    INGEST_MODE                  = ${C}\"%s\"${N}\n" "$NEW_INGEST_MODE"
printf "    TRACKB_RUNNER_MODE           = ${C}\"%s\"${N}\n" "$NEW_TRACKB_RUNNER_MODE"
printf "    PROD_DATABASE_URL            = ${C}\"postgresql://...\"${N}\n"
printf "    DATABASE_URL                 = ${C}\"postgresql://...\"${N}\n"
printf "    OAUTH_FRONTEND_BASE_URL      = ${C}\"%s\"${N}\n" "$NEW_OAUTH_FRONTEND_BASE_URL"
printf "    CORS_ORIGINS                 = ${C}\"%s\"${N}\n" "$NEW_CORS_ORIGINS"
printf "    PUBLIC_HOSTNAME              = ${C}\"%s\"${N}\n" "$NEW_PUBLIC_HOSTNAME"
printf "    AGENTIQ_BACKEND_URL          = ${C}\"%s\"${N}\n" "$NEW_AGENTIQ_BACKEND_URL"
printf "    OAUTH_REDIRECT_URI           = ${C}\"%s\"${N}\n" "$NEW_OAUTH_REDIRECT_URI"
printf "    OAUTH_CALLBACK_ALLOW_UNAUTH  = ${C}1${N}\n"
printf "    CREDENTIAL_VAULT_KEY         = ${C}\"...\"${N}\n"
printf "    INFERRED_RELATIONSHIPS_ENABLED = ${C}\"%s\"${N}\n" "$NEW_INFERRED_RELATIONSHIPS_ENABLED"
printf "    EMAIL_PROVIDER               = ${C}\"%s\"${N}\n" "$NEW_EMAIL_PROVIDER"
printf "    LICENSE_API_URL              = ${C}\"...\"${N}\n"
printf "    SMTP_HOST                    = ${C}\"%s\"${N}\n" "$NEW_SMTP_HOST"
printf "    SMTP_PORT                    = ${C}\"%s\"${N}\n" "$NEW_SMTP_PORT"
printf "    SMTP_USERNAME                = ${C}\"%s\"${N}\n" "$NEW_SMTP_USERNAME"
printf "    SMTP_PASSWORD                = ${C}%s${N}\n" "${NEW_SMTP_PASSWORD:+(set — hidden)}"
printf "    SMTP_USE_STARTTLS            = ${C}\"%s\"${N}\n" "$NEW_SMTP_USE_STARTTLS"
printf "    EMAIL_FROM                   = ${C}\"%s\"${N}\n" "$NEW_EMAIL_FROM"
printf "    EMAIL_FROM_NAME              = ${C}\"%s\"${N}\n" "$NEW_EMAIL_FROM_NAME"
printf "    AGENTIQ_ADMIN_EMAIL          = ${C}\"%s\"${N}\n" "$NEW_AGENTIQ_ADMIN_EMAIL"
printf "    ANTHROPIC_API_KEY            = ${C}%s${N}\n" "${NEW_ANTHROPIC_API_KEY:+(set — hidden)}"
printf "    SALESFORCE_INSTANCE          = ${C}\"%s\"${N}\n" "$NEW_SALESFORCE_INSTANCE"
printf "    SALESFORCE_CLIENT_ID         = ${C}\"%s\"${N}\n" "$NEW_SALESFORCE_CLIENT_ID"
printf "    SALESFORCE_CLIENT_SECRET     = ${C}%s${N}\n" "${NEW_SALESFORCE_CLIENT_SECRET:+(set — hidden)}"
printf "    SERVICENOW_INSTANCE          = ${C}\"%s\"${N}\n" "$NEW_SERVICENOW_INSTANCE"
printf "    SERVICENOW_CLIENT_ID         = ${C}\"%s\"${N}\n" "$NEW_SERVICENOW_CLIENT_ID"
printf "    SERVICENOW_CLIENT_SECRET     = ${C}%s${N}\n" "${NEW_SERVICENOW_CLIENT_SECRET:+(set — hidden)}"
printf "    JIRA_CLIENT_ID               = ${C}\"%s\"${N}\n" "$NEW_JIRA_CLIENT_ID"
printf "    JIRA_CLIENT_SECRET           = ${C}%s${N}\n" "${NEW_JIRA_CLIENT_SECRET:+(set — hidden)}"

# ── Create directory if needed ────────────────────────────────────────────────
if [[ ! -d "$TARGET_DIR" ]]; then
  echo ""
  printf "  Creating %s ...\n" "$TARGET_DIR"
  mkdir -p "$TARGET_DIR"
  printf "  ${G}✓ Directory created${N}\n"
fi

# ── Read helper ───────────────────────────────────────────────────────────────
read_existing() {
  grep -E "^${1}=" "$TARGET_FILE" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '"' | xargs 2>/dev/null || true
}

# ── Compare with existing .env ────────────────────────────────────────────────
if [[ -f "$TARGET_FILE" ]]; then
  diff_row() {
    local key="$1" old="$2" new="$3"
    if [[ "$old" != "$new" ]]; then
      printf "    ${Y}%-38s${N}\n" "$key"
      printf "      ${DIM}current :${N} \"%s\"\n" "$old"
      printf "      ${G}new     :${N} \"%s\"\n" "$new"
    fi
  }

  # Read all keys from existing file
  CUR_INGEST_MODE=$(read_existing INGEST_MODE)
  CUR_TRACKB=$(read_existing TRACKB_RUNNER_MODE)
  CUR_PROD_DB=$(read_existing PROD_DATABASE_URL)
  CUR_DB=$(read_existing DATABASE_URL)
  CUR_OAUTH_FRONTEND=$(read_existing OAUTH_FRONTEND_BASE_URL)
  CUR_CORS=$(read_existing CORS_ORIGINS)
  CUR_PUBLIC=$(read_existing PUBLIC_HOSTNAME)
  CUR_BACKEND=$(read_existing AGENTIQ_BACKEND_URL)
  CUR_REDIRECT=$(read_existing OAUTH_REDIRECT_URI)
  CUR_CALLBACK_UNAUTH=$(read_existing OAUTH_CALLBACK_ALLOW_UNAUTH)
  CUR_VAULT_KEY=$(read_existing CREDENTIAL_VAULT_KEY)
  CUR_INFERRED=$(read_existing INFERRED_RELATIONSHIPS_ENABLED)
  CUR_EMAIL=$(read_existing EMAIL_PROVIDER)
  CUR_LICENSE=$(read_existing LICENSE_API_URL)
  CUR_SMTP_HOST=$(read_existing SMTP_HOST)
  CUR_SMTP_PORT=$(read_existing SMTP_PORT)
  CUR_SMTP_USERNAME=$(read_existing SMTP_USERNAME)
  CUR_SMTP_PASSWORD=$(read_existing SMTP_PASSWORD)
  CUR_SMTP_USE_STARTTLS=$(read_existing SMTP_USE_STARTTLS)
  CUR_EMAIL_FROM=$(read_existing EMAIL_FROM)
  CUR_EMAIL_FROM_NAME=$(read_existing EMAIL_FROM_NAME)
  CUR_AGENTIQ_ADMIN_EMAIL=$(read_existing AGENTIQ_ADMIN_EMAIL)
  CUR_ANTHROPIC_API_KEY=$(read_existing ANTHROPIC_API_KEY)
  CUR_SALESFORCE_INSTANCE=$(read_existing SALESFORCE_INSTANCE)
  CUR_SALESFORCE_CLIENT_ID=$(read_existing SALESFORCE_CLIENT_ID)
  CUR_SALESFORCE_CLIENT_SECRET=$(read_existing SALESFORCE_CLIENT_SECRET)
  CUR_SERVICENOW_INSTANCE=$(read_existing SERVICENOW_INSTANCE)
  CUR_SERVICENOW_CLIENT_ID=$(read_existing SERVICENOW_CLIENT_ID)
  CUR_SERVICENOW_CLIENT_SECRET=$(read_existing SERVICENOW_CLIENT_SECRET)
  CUR_JIRA_CLIENT_ID=$(read_existing JIRA_CLIENT_ID)
  CUR_JIRA_CLIENT_SECRET=$(read_existing JIRA_CLIENT_SECRET)

  # Check all keys match
  if [[ "$CUR_INGEST_MODE"     == "$NEW_INGEST_MODE"               ]] &&
     [[ "$CUR_TRACKB"          == "$NEW_TRACKB_RUNNER_MODE"         ]] &&
     [[ "$CUR_PROD_DB"         == "$NEW_PROD_DATABASE_URL"          ]] &&
     [[ "$CUR_DB"              == "$NEW_DATABASE_URL"               ]] &&
     [[ "$CUR_OAUTH_FRONTEND"  == "$NEW_OAUTH_FRONTEND_BASE_URL"    ]] &&
     [[ "$CUR_CORS"            == "$NEW_CORS_ORIGINS"               ]] &&
     [[ "$CUR_PUBLIC"          == "$NEW_PUBLIC_HOSTNAME"            ]] &&
     [[ "$CUR_BACKEND"         == "$NEW_AGENTIQ_BACKEND_URL"        ]] &&
     [[ "$CUR_REDIRECT"        == "$NEW_OAUTH_REDIRECT_URI"         ]] &&
     [[ "$CUR_CALLBACK_UNAUTH" == "$NEW_OAUTH_CALLBACK_ALLOW_UNAUTH" ]] &&
     [[ "$CUR_VAULT_KEY"       == "$NEW_CREDENTIAL_VAULT_KEY"       ]] &&
     [[ "$CUR_INFERRED"        == "$NEW_INFERRED_RELATIONSHIPS_ENABLED" ]] &&
     [[ "$CUR_EMAIL"           == "$NEW_EMAIL_PROVIDER"             ]] &&
     [[ "$CUR_LICENSE"         == "$NEW_LICENSE_API_URL"            ]] &&
     [[ "$CUR_SMTP_HOST"       == "$NEW_SMTP_HOST"                  ]] &&
     [[ "$CUR_SMTP_PORT"       == "$NEW_SMTP_PORT"                  ]] &&
     [[ "$CUR_SMTP_USERNAME"   == "$NEW_SMTP_USERNAME"              ]] &&
     [[ "$CUR_SMTP_PASSWORD"   == "$NEW_SMTP_PASSWORD"              ]] &&
     [[ "$CUR_SMTP_USE_STARTTLS" == "$NEW_SMTP_USE_STARTTLS"        ]] &&
     [[ "$CUR_EMAIL_FROM"      == "$NEW_EMAIL_FROM"                 ]] &&
     [[ "$CUR_EMAIL_FROM_NAME" == "$NEW_EMAIL_FROM_NAME"            ]] &&
     [[ "$CUR_AGENTIQ_ADMIN_EMAIL" == "$NEW_AGENTIQ_ADMIN_EMAIL"    ]] &&
     [[ "$CUR_ANTHROPIC_API_KEY"   == "$NEW_ANTHROPIC_API_KEY"      ]] &&
     [[ "$CUR_SALESFORCE_INSTANCE" == "$NEW_SALESFORCE_INSTANCE"    ]] &&
     [[ "$CUR_SALESFORCE_CLIENT_ID" == "$NEW_SALESFORCE_CLIENT_ID"  ]] &&
     [[ "$CUR_SALESFORCE_CLIENT_SECRET" == "$NEW_SALESFORCE_CLIENT_SECRET" ]] &&
     [[ "$CUR_SERVICENOW_INSTANCE" == "$NEW_SERVICENOW_INSTANCE"    ]] &&
     [[ "$CUR_SERVICENOW_CLIENT_ID" == "$NEW_SERVICENOW_CLIENT_ID"  ]] &&
     [[ "$CUR_SERVICENOW_CLIENT_SECRET" == "$NEW_SERVICENOW_CLIENT_SECRET" ]] &&
     [[ "$CUR_JIRA_CLIENT_ID"  == "$NEW_JIRA_CLIENT_ID"             ]] &&
     [[ "$CUR_JIRA_CLIENT_SECRET" == "$NEW_JIRA_CLIENT_SECRET"      ]]; then
    echo ""
    printf "  ${G}✓ No changes needed — %s already matches.${N}\n" "$TARGET_FILE"
    exit 0
  fi

  # Show what will change
  echo ""
  printf "  ${Y}⚠  WARNING: %s exists with different values:${N}\n" "$TARGET_FILE"
  echo ""

  diff_row "INGEST_MODE"                    "$CUR_INGEST_MODE"     "$NEW_INGEST_MODE"
  diff_row "TRACKB_RUNNER_MODE"             "$CUR_TRACKB"          "$NEW_TRACKB_RUNNER_MODE"
  diff_row "PROD_DATABASE_URL"              "$CUR_PROD_DB"         "$NEW_PROD_DATABASE_URL"
  diff_row "DATABASE_URL"                   "$CUR_DB"              "$NEW_DATABASE_URL"
  diff_row "OAUTH_FRONTEND_BASE_URL"        "$CUR_OAUTH_FRONTEND"  "$NEW_OAUTH_FRONTEND_BASE_URL"
  diff_row "CORS_ORIGINS"                   "$CUR_CORS"            "$NEW_CORS_ORIGINS"
  diff_row "PUBLIC_HOSTNAME"                "$CUR_PUBLIC"          "$NEW_PUBLIC_HOSTNAME"
  diff_row "AGENTIQ_BACKEND_URL"            "$CUR_BACKEND"         "$NEW_AGENTIQ_BACKEND_URL"
  diff_row "OAUTH_REDIRECT_URI"             "$CUR_REDIRECT"        "$NEW_OAUTH_REDIRECT_URI"
  diff_row "OAUTH_CALLBACK_ALLOW_UNAUTH"    "$CUR_CALLBACK_UNAUTH" "$NEW_OAUTH_CALLBACK_ALLOW_UNAUTH"
  diff_row "CREDENTIAL_VAULT_KEY"           "$CUR_VAULT_KEY"       "$NEW_CREDENTIAL_VAULT_KEY"
  diff_row "INFERRED_RELATIONSHIPS_ENABLED" "$CUR_INFERRED"        "$NEW_INFERRED_RELATIONSHIPS_ENABLED"
  diff_row "EMAIL_PROVIDER"                 "$CUR_EMAIL"                    "$NEW_EMAIL_PROVIDER"
  diff_row "LICENSE_API_URL"                "$CUR_LICENSE"                  "$NEW_LICENSE_API_URL"
  diff_row "SMTP_HOST"                      "$CUR_SMTP_HOST"                "$NEW_SMTP_HOST"
  diff_row "SMTP_PORT"                      "$CUR_SMTP_PORT"                "$NEW_SMTP_PORT"
  diff_row "SMTP_USERNAME"                  "$CUR_SMTP_USERNAME"            "$NEW_SMTP_USERNAME"
  diff_row "SMTP_PASSWORD"                  "$CUR_SMTP_PASSWORD"            "$NEW_SMTP_PASSWORD"
  diff_row "SMTP_USE_STARTTLS"              "$CUR_SMTP_USE_STARTTLS"        "$NEW_SMTP_USE_STARTTLS"
  diff_row "EMAIL_FROM"                     "$CUR_EMAIL_FROM"               "$NEW_EMAIL_FROM"
  diff_row "EMAIL_FROM_NAME"               "$CUR_EMAIL_FROM_NAME"          "$NEW_EMAIL_FROM_NAME"
  diff_row "AGENTIQ_ADMIN_EMAIL"           "$CUR_AGENTIQ_ADMIN_EMAIL"      "$NEW_AGENTIQ_ADMIN_EMAIL"
  diff_row "ANTHROPIC_API_KEY"             "$CUR_ANTHROPIC_API_KEY"        "$NEW_ANTHROPIC_API_KEY"
  diff_row "SALESFORCE_INSTANCE"           "$CUR_SALESFORCE_INSTANCE"      "$NEW_SALESFORCE_INSTANCE"
  diff_row "SALESFORCE_CLIENT_ID"          "$CUR_SALESFORCE_CLIENT_ID"     "$NEW_SALESFORCE_CLIENT_ID"
  diff_row "SALESFORCE_CLIENT_SECRET"      "$CUR_SALESFORCE_CLIENT_SECRET" "$NEW_SALESFORCE_CLIENT_SECRET"
  diff_row "SERVICENOW_INSTANCE"           "$CUR_SERVICENOW_INSTANCE"      "$NEW_SERVICENOW_INSTANCE"
  diff_row "SERVICENOW_CLIENT_ID"          "$CUR_SERVICENOW_CLIENT_ID"     "$NEW_SERVICENOW_CLIENT_ID"
  diff_row "SERVICENOW_CLIENT_SECRET"      "$CUR_SERVICENOW_CLIENT_SECRET" "$NEW_SERVICENOW_CLIENT_SECRET"
  diff_row "JIRA_CLIENT_ID"                "$CUR_JIRA_CLIENT_ID"           "$NEW_JIRA_CLIENT_ID"
  diff_row "JIRA_CLIENT_SECRET"            "$CUR_JIRA_CLIENT_SECRET"       "$NEW_JIRA_CLIENT_SECRET"

  echo ""
  printf "  ${Y}Overwrite %s with new values? [y/N]${N} " "$TARGET_FILE"
  IFS= read -r confirm
  if [[ "${confirm,,}" != "y" ]]; then
    printf "  Aborted — %s was not changed.\n" "$TARGET_FILE"
    exit 0
  fi
fi

# ── Write .env ────────────────────────────────────────────────────────────────
cat > "$TARGET_FILE" <<EOF
INGEST_MODE="${NEW_INGEST_MODE}"
TRACKB_RUNNER_MODE="${NEW_TRACKB_RUNNER_MODE}"
# Databases
PROD_DATABASE_URL="${NEW_PROD_DATABASE_URL}"
DATABASE_URL="${NEW_DATABASE_URL}"
OAUTH_FRONTEND_BASE_URL="${NEW_OAUTH_FRONTEND_BASE_URL}"
CORS_ORIGINS="${NEW_CORS_ORIGINS}"
PUBLIC_HOSTNAME="${NEW_PUBLIC_HOSTNAME}"
AGENTIQ_BACKEND_URL="${NEW_AGENTIQ_BACKEND_URL}"
OAUTH_REDIRECT_URI="${NEW_OAUTH_REDIRECT_URI}"
OAUTH_CALLBACK_ALLOW_UNAUTH=${NEW_OAUTH_CALLBACK_ALLOW_UNAUTH}
CREDENTIAL_VAULT_KEY="${NEW_CREDENTIAL_VAULT_KEY}"
#For Realtionship mapping UI
INFERRED_RELATIONSHIPS_ENABLED=${NEW_INFERRED_RELATIONSHIPS_ENABLED}
# Transactional Email
EMAIL_PROVIDER="${NEW_EMAIL_PROVIDER}"
# License API URL
LICENSE_API_URL="${NEW_LICENSE_API_URL}"
#SMTP details
SMTP_HOST="${NEW_SMTP_HOST}"
SMTP_PORT="${NEW_SMTP_PORT}"
SMTP_USERNAME="${NEW_SMTP_USERNAME}"
SMTP_PASSWORD="${NEW_SMTP_PASSWORD}"
SMTP_USE_STARTTLS="${NEW_SMTP_USE_STARTTLS}"
EMAIL_FROM="${NEW_EMAIL_FROM}"
EMAIL_FROM_NAME="${NEW_EMAIL_FROM_NAME}"
AGENTIQ_ADMIN_EMAIL="${NEW_AGENTIQ_ADMIN_EMAIL}"
#Anthropic API Key details
ANTHROPIC_API_KEY="${NEW_ANTHROPIC_API_KEY}"
#Sales force details
SALESFORCE_INSTANCE="${NEW_SALESFORCE_INSTANCE}"
SALESFORCE_CLIENT_ID="${NEW_SALESFORCE_CLIENT_ID}"
SALESFORCE_CLIENT_SECRET="${NEW_SALESFORCE_CLIENT_SECRET}"
# ServiceNow details
SERVICENOW_INSTANCE="${NEW_SERVICENOW_INSTANCE}"
SERVICENOW_CLIENT_ID="${NEW_SERVICENOW_CLIENT_ID}"
SERVICENOW_CLIENT_SECRET="${NEW_SERVICENOW_CLIENT_SECRET}"
# Jira
JIRA_CLIENT_ID="${NEW_JIRA_CLIENT_ID}"
JIRA_CLIENT_SECRET="${NEW_JIRA_CLIENT_SECRET}"
EOF

chmod 600 "$TARGET_FILE"

echo ""
printf "  ${G}${BOLD}✓ Written to %s${N}\n" "$TARGET_FILE"
printf "  ${DIM}Permissions : 600 (owner read/write only)${N}\n"
echo ""
