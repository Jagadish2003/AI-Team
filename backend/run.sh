#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=backend
 
usage() {
  echo "Usage: ./run.sh --db <dev|prod>" >&2
  echo "  Selects DATABASE_URL from DEV_DATABASE_URL / PROD_DATABASE_URL in .env" >&2
  exit 1
}
 
DB_ENV=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB_ENV="${2:-}"; shift 2 || usage ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done
 
[[ -z "$DB_ENV" ]] && { echo "Error: --db <dev|prod> is required." >&2; usage; }
 
# Read a single KEY=value line from .env without sourcing it (connection
# strings may contain characters the shell would otherwise interpret).
# Strips a trailing CR (Windows CRLF .env) and surrounding single/double
# quotes, since psycopg2 treats quote characters as part of the DSN.
read_env() {
  local v
  v="$(grep -E "^$1=" .env | head -n1 | cut -d= -f2-)"
  v="${v%$'\r'}"
  v="${v#\"}"; v="${v%\"}"
  v="${v#\'}"; v="${v%\'}"
  echo "$v"
}
 
case "$DB_ENV" in
  dev)  SELECTED_URL="$(read_env DEV_DATABASE_URL)" ;;
  prod) SELECTED_URL="$(read_env PROD_DATABASE_URL)" ;;
  *) echo "Error: --db must be 'dev' or 'prod' (got '$DB_ENV')." >&2; usage ;;
esac
 
if [[ -z "$SELECTED_URL" ]]; then
  echo "Error: ${DB_ENV^^}_DATABASE_URL is empty or missing in .env." >&2
  exit 1
fi
 
# Rewrite the DATABASE_URL line in .env in place (line-by-line, not sed, since
# connection strings contain '/', ':', '?', '&', '=' that break sed delimiters).
# This persists the choice so later tooling that reads .env directly — notably
# `alembic upgrade head` — targets the same database.
update_env_var() {
  local key="$1" value="$2" file=".env" tmp found=0
  tmp="$(mktemp)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$key="* ]]; then
      printf '%s=%s\n' "$key" "$value" >> "$tmp"
      found=1
    else
      printf '%s\n' "$line" >> "$tmp"
    fi
  done < "$file"
  [[ $found -eq 0 ]] && printf '%s=%s\n' "$key" "$value" >> "$tmp"
  mv "$tmp" "$file"
}
 
# Write the value double-quoted (DATABASE_URL="...") so the persisted .env line
# is well-formed even when the DSN contains characters a bare value could expose;
# read_env above and provision.sh / python-dotenv all strip the surrounding
# quotes on read. The exported value below stays unquoted so psycopg2 receives a
# clean DSN (a literal quote in the live value would break the connection).
update_env_var DATABASE_URL "\"$SELECTED_URL\""
 
# Also export it for this process, so the server uses it even before any
# re-read of .env (the app loads .env with override=False, so this wins too).
export DATABASE_URL="$SELECTED_URL"
 
# Strip credentials (scheme + userinfo) and query params so we can echo the
# host/port/db for a visual sanity check without leaking the password.
mask_url() {
  local rest="${1#*://}"     # drop scheme://  -> [user:pass@]host:port/db?params
  rest="${rest#*@}"          # drop userinfo@  -> host:port/db?params (no-op if absent)
  echo "${rest%%\?*}"        # drop ?params    -> host:port/db
}
 
echo "Set DATABASE_URL in .env to ${DB_ENV^^}_DATABASE_URL"
echo "  -> $(mask_url "$SELECTED_URL")"
# Default AWS region for the SDK. The AWS Event Connector's hub "Test connection"
# has no region field, so boto3 needs a region from the environment to sign the
# STS call (without one it fails with NoRegionError, which the UI mislabels as
# rejected credentials). The ${VAR:-default} form only sets a default when the
# developer has not already exported their own, so it never overrides anyone.
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_REGION="${AWS_REGION:-$AWS_DEFAULT_REGION}"

echo "Starting AgentIQ backend against ${DB_ENV^^} database"
echo "  AWS region -> ${AWS_DEFAULT_REGION}"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload