#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ — Configfile-create.sh
# Creates /opt/aiqstore/.env with all required deployment settings.
# Must run as root (called by agentiq-install.sh which escalates once).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

STORE_DIR="/opt/aiqstore"
TARGET_FILE="$STORE_DIR/.env"
LOG_DIR="$STORE_DIR/logs"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[config]${NC} $*"; }
warn()  { echo -e "${YELLOW}[config]${NC} $*"; }
error() { echo -e "${RED}[config]${NC} $*"; }

# ── Early overwrite check ─────────────────────────────────────────────────────
if [[ -f "$TARGET_FILE" ]]; then
  warn "$TARGET_FILE already exists."
  printf "  Reconfigure and overwrite? [y/N] "
  IFS= read -r _early_confirm
  if [[ "${_early_confirm,,}" != "y" ]]; then
    info "Keeping existing configuration."
    exit 0
  fi
fi

# ── Create directories ────────────────────────────────────────────────────────
mkdir -p "$STORE_DIR"
mkdir -p "$LOG_DIR"
mkdir -p "$STORE_DIR/ssl"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AgentIQ — Environment Configuration"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  This wizard creates $TARGET_FILE"
echo "  Press Enter to accept the default shown in [brackets]."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Helper: prompt with default ───────────────────────────────────────────────
prompt() {
  local var="$1" msg="$2" default="$3" secret="${4:-no}"
  if [[ "$secret" == "yes" ]]; then
    printf "  %s [%s]: " "$msg" "********"
    IFS= read -rs _val; echo
  else
    printf "  %s [%s]: " "$msg" "$default"
    IFS= read -r _val
  fi
  [[ -z "$_val" ]] && _val="$default"
  printf -v "$var" '%s' "$_val"
}

# ── Section 1: Security ───────────────────────────────────────────────────────
echo
echo "  ── Security ──────────────────────────────────────────────────"
prompt JWT_SECRET   "JWT secret key"       "$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)"
prompt DEV_JWT      "Dev API token"        "dev-token-change-me"
prompt POSTGRES_PASSWORD "Postgres password" "agentiq_$(openssl rand -hex 8 2>/dev/null || echo 'changeme')" "yes"
prompt POSTGRES_USER     "Postgres user"     "agentiq"
prompt POSTGRES_DB       "Postgres database" "agentiq"
prompt CREDENTIAL_VAULT_KEY "Vault encryption key (Fernet, leave blank to auto-generate)" ""

if [[ -z "$CREDENTIAL_VAULT_KEY" ]]; then
  # Generate a Fernet key via Python if available
  CREDENTIAL_VAULT_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || echo "")
  if [[ -z "$CREDENTIAL_VAULT_KEY" ]]; then
    warn "Could not auto-generate Fernet key — leaving blank (set manually before connecting OAuth integrations)."
  else
    info "Auto-generated Fernet vault key."
  fi
fi

# ── Section 2: Database ───────────────────────────────────────────────────────
echo
echo "  ── Database ──────────────────────────────────────────────────"
# DATABASE_URL points backend ORM to the postgres container
DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}"
info "DATABASE_URL set to postgres container (internal Docker network)."

# ── Section 3: LLM / AI ───────────────────────────────────────────────────────
echo
echo "  ── AI / LLM (optional — leave blank for offline mode) ────────"
prompt ANTHROPIC_API_KEY "Anthropic API key" ""
INGEST_MODE="offline"
if [[ -n "$ANTHROPIC_API_KEY" ]]; then
  INGEST_MODE="online"
  info "LLM key provided — setting INGEST_MODE=online."
fi

# ── Section 4: CORS / Networking ─────────────────────────────────────────────
echo
echo "  ── Networking ────────────────────────────────────────────────"
prompt PUBLIC_HOSTNAME "Public hostname or IP (e.g. https://agentiq.example.com)" "http://localhost"
CORS_ORIGINS="${PUBLIC_HOSTNAME},http://localhost,http://localhost:80"

# ── Section 5: Email / SMTP (optional) ────────────────────────────────────────
echo
echo "  ── SMTP / Email (optional — press Enter to skip) ─────────────"
prompt SMTP_HOST     "SMTP host"         ""
prompt SMTP_PORT     "SMTP port"         "587"
prompt SMTP_USERNAME "SMTP username"     ""
prompt SMTP_PASSWORD "SMTP password"     "" "yes"
prompt EMAIL_FROM    "From address"      "noreply@example.com"

# ── Section 6: OAuth Connectors (optional) ────────────────────────────────────
echo
echo "  ── OAuth Connectors (optional — press Enter to skip each) ────"
prompt SALESFORCE_CLIENT_ID     "Salesforce Client ID"     ""
prompt SALESFORCE_CLIENT_SECRET "Salesforce Client Secret" "" "yes"
prompt SERVICENOW_CLIENT_ID     "ServiceNow Client ID"     ""
prompt SERVICENOW_CLIENT_SECRET "ServiceNow Client Secret" "" "yes"
prompt JIRA_CLIENT_ID           "Jira Client ID"           ""
prompt JIRA_CLIENT_SECRET       "Jira Client Secret"       "" "yes"
prompt GITHUB_CLIENT_ID         "GitHub Client ID"         ""
prompt GITHUB_CLIENT_SECRET     "GitHub Client Secret"     "" "yes"

OAUTH_REDIRECT_URI="${PUBLIC_HOSTNAME}/api/connectors/oauth/callback"

# ── Section 7: License ────────────────────────────────────────────────────────
echo
echo "  ── License ────────────────────────────────────────────────────"
prompt LICENSE_KEY "License key" ""

# ── Write .env ────────────────────────────────────────────────────────────────
cat > "$TARGET_FILE" <<EOF
# AgentIQ environment configuration
# Generated by Configfile-create.sh
# DO NOT commit this file — it contains secrets.

# ── Core / Server ────────────────────────────────────────────────
DEV_JWT=${DEV_JWT}
JWT_SECRET=${JWT_SECRET}
ENVIRONMENT=production

# ── Database ─────────────────────────────────────────────────────
DATABASE_URL=${DATABASE_URL}
POSTGRES_DB=${POSTGRES_DB}
POSTGRES_USER=${POSTGRES_USER}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# ── Credential Vault ─────────────────────────────────────────────
CREDENTIAL_VAULT_KEY=${CREDENTIAL_VAULT_KEY}

# ── CORS / Networking ─────────────────────────────────────────────
CORS_ORIGINS=${CORS_ORIGINS}
PUBLIC_HOSTNAME=${PUBLIC_HOSTNAME}
AGENTIQ_BACKEND_URL=${PUBLIC_HOSTNAME}

# ── LLM / Ingest ─────────────────────────────────────────────────
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
INGEST_MODE=${INGEST_MODE}
TRACKB_RUNNER_MODE=offline

# ── OAuth Connectors ─────────────────────────────────────────────
OAUTH_REDIRECT_URI=${OAUTH_REDIRECT_URI}
SALESFORCE_CLIENT_ID=${SALESFORCE_CLIENT_ID}
SALESFORCE_CLIENT_SECRET=${SALESFORCE_CLIENT_SECRET}
SERVICENOW_CLIENT_ID=${SERVICENOW_CLIENT_ID}
SERVICENOW_CLIENT_SECRET=${SERVICENOW_CLIENT_SECRET}
JIRA_CLIENT_ID=${JIRA_CLIENT_ID}
JIRA_CLIENT_SECRET=${JIRA_CLIENT_SECRET}
GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID}
GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET}

# ── Email / SMTP ─────────────────────────────────────────────────
EMAIL_PROVIDER=smtp
EMAIL_FROM=${EMAIL_FROM}
EMAIL_FROM_NAME=AgentIQ
SMTP_HOST=${SMTP_HOST}
SMTP_PORT=${SMTP_PORT}
SMTP_USERNAME=${SMTP_USERNAME}
SMTP_PASSWORD=${SMTP_PASSWORD}
SMTP_USE_STARTTLS=true

# ── License ──────────────────────────────────────────────────────
LICENSE_KEY=${LICENSE_KEY}
EOF

chmod 600 "$TARGET_FILE"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Configuration saved to $TARGET_FILE"
info "File permissions set to 600 (root read-only)."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
