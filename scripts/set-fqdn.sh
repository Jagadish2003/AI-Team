#!/usr/bin/env bash
# =============================================================================
# AgentIQ — FQDN / URL Configuration
# Creates /opt/aiqtestdir/.env with the five URL-related variables.
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

# ── Build values ──────────────────────────────────────────────────────────────
BASE_URL="${SCHEME}://${FQDN}"

NEW_OAUTH_FRONTEND_BASE_URL="$BASE_URL"
NEW_CORS_ORIGINS="$BASE_URL"
NEW_PUBLIC_HOSTNAME="$BASE_URL"
NEW_AGENTIQ_BACKEND_URL="$BASE_URL"
NEW_OAUTH_REDIRECT_URI="${BASE_URL}/api/connectors/oauth/callback"

echo ""
printf "  ${BOLD}Values to write${N}\n"
printf "    OAUTH_FRONTEND_BASE_URL = ${C}\"%s\"${N}\n" "$NEW_OAUTH_FRONTEND_BASE_URL"
printf "    CORS_ORIGINS            = ${C}\"%s\"${N}\n" "$NEW_CORS_ORIGINS"
printf "    PUBLIC_HOSTNAME         = ${C}\"%s\"${N}\n" "$NEW_PUBLIC_HOSTNAME"
printf "    AGENTIQ_BACKEND_URL     = ${C}\"%s\"${N}\n" "$NEW_AGENTIQ_BACKEND_URL"
printf "    OAUTH_REDIRECT_URI      = ${C}\"%s\"${N}\n" "$NEW_OAUTH_REDIRECT_URI"

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
  CUR_OAUTH_FRONTEND=$(read_existing OAUTH_FRONTEND_BASE_URL)
  CUR_CORS=$(read_existing CORS_ORIGINS)
  CUR_PUBLIC=$(read_existing PUBLIC_HOSTNAME)
  CUR_BACKEND=$(read_existing AGENTIQ_BACKEND_URL)
  CUR_REDIRECT=$(read_existing OAUTH_REDIRECT_URI)

  # Check all five match
  if [[ "$CUR_OAUTH_FRONTEND" == "$NEW_OAUTH_FRONTEND_BASE_URL" ]] &&
     [[ "$CUR_CORS"           == "$NEW_CORS_ORIGINS"           ]] &&
     [[ "$CUR_PUBLIC"         == "$NEW_PUBLIC_HOSTNAME"        ]] &&
     [[ "$CUR_BACKEND"        == "$NEW_AGENTIQ_BACKEND_URL"    ]] &&
     [[ "$CUR_REDIRECT"       == "$NEW_OAUTH_REDIRECT_URI"     ]]; then
    echo ""
    printf "  ${G}✓ No changes needed — %s already matches.${N}\n" "$TARGET_FILE"
    exit 0
  fi

  # Show what will change
  echo ""
  printf "  ${Y}⚠  WARNING: %s exists with different values:${N}\n" "$TARGET_FILE"
  echo ""

  diff_row() {
    local key="$1" old="$2" new="$3"
    if [[ "$old" != "$new" ]]; then
      printf "    ${Y}%-30s${N}\n" "$key"
      printf "      ${DIM}current  :${N} \"%s\"\n" "$old"
      printf "      ${G}new      :${N} \"%s\"\n" "$new"
    fi
  }

  diff_row "OAUTH_FRONTEND_BASE_URL" "$CUR_OAUTH_FRONTEND" "$NEW_OAUTH_FRONTEND_BASE_URL"
  diff_row "CORS_ORIGINS"            "$CUR_CORS"           "$NEW_CORS_ORIGINS"
  diff_row "PUBLIC_HOSTNAME"         "$CUR_PUBLIC"         "$NEW_PUBLIC_HOSTNAME"
  diff_row "AGENTIQ_BACKEND_URL"     "$CUR_BACKEND"        "$NEW_AGENTIQ_BACKEND_URL"
  diff_row "OAUTH_REDIRECT_URI"      "$CUR_REDIRECT"       "$NEW_OAUTH_REDIRECT_URI"

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
OAUTH_FRONTEND_BASE_URL="${NEW_OAUTH_FRONTEND_BASE_URL}"
CORS_ORIGINS="${NEW_CORS_ORIGINS}"
PUBLIC_HOSTNAME="${NEW_PUBLIC_HOSTNAME}"
AGENTIQ_BACKEND_URL="${NEW_AGENTIQ_BACKEND_URL}"
OAUTH_REDIRECT_URI="${NEW_OAUTH_REDIRECT_URI}"
EOF

chmod 600 "$TARGET_FILE"

echo ""
printf "  ${G}${BOLD}✓ Written to %s${N}\n" "$TARGET_FILE"
printf "  ${DIM}Permissions : 600 (owner read/write only)${N}\n"
echo ""
