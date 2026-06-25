#!/usr/bin/env bash
set -euo pipefail

STORE_DIR="/opt/aiqstore"
ENV_FILE="$STORE_DIR/backend/.env"
COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"

# ── 1. Create directory structure ─────────────────────────────────────────────
echo "==> Setting up storage at $STORE_DIR..."

mkdir -p "$STORE_DIR/backend"
mkdir -p "$STORE_DIR/postgres"
mkdir -p "$STORE_DIR/logs"
mkdir -p "$STORE_DIR/ssl"

echo "    $STORE_DIR/backend/   — backend .env"
echo "    $STORE_DIR/postgres/  — postgres data (persistent)"
echo "    $STORE_DIR/logs/      — log files"
echo "    $STORE_DIR/ssl/       — SSL certificates (cert.pem fullchain.pem privkey.pem)"

# ── 2. Preflight: check .env ──────────────────────────────────────────────────
echo ""
echo "==> Checking prerequisites..."

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found."
    echo "       Place your .env file at $ENV_FILE before running this script."
    echo "       Reference: $(dirname "$0")/backend/.env.example"
    exit 1
fi

# Read required values safely (no sourcing — avoids shell injection from .env)
read_env() {
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -n1 | cut -d= -f2- | tr -d '\r"' | xargs || true
}

PROD_URL=$(read_env PROD_DATABASE_URL)
PG_USER=$(read_env POSTGRES_USER)
PG_PASS=$(read_env POSTGRES_PASSWORD)
PG_DB=$(read_env POSTGRES_DB)

if [[ -z "$PROD_URL" ]]; then
    echo "ERROR: PROD_DATABASE_URL is empty in $ENV_FILE"
    exit 1
fi

# If POSTGRES_USER / POSTGRES_PASSWORD are not set explicitly, derive them from
# PROD_DATABASE_URL so that the postgres container always uses the same credentials
# the backend expects.  URL format: scheme://user:password@host:port/dbname
if [[ -z "$PG_USER" ]]; then
    PG_USER=$(printf '%s' "$PROD_URL" | sed -E 's|^[^:]+://([^:@/]+).*|\1|')
    [[ -z "$PG_USER" ]] && PG_USER="agentiq"
fi
if [[ -z "$PG_PASS" ]]; then
    PG_PASS=$(printf '%s' "$PROD_URL" | sed -E 's|^[^:]+://[^:]+:([^@]+)@.*|\1|')
    [[ -z "$PG_PASS" ]] && PG_PASS="agentiq_secret"
fi
[[ -z "$PG_DB" ]] && PG_DB="agentiqprod"

echo "    ENV file : $ENV_FILE  [OK]"
echo "    DB user  : $PG_USER  /  database : $PG_DB"

# ── 3. Generate minimal compose env (avoids passing full .env to compose) ─────
# Only the 5 vars compose YAML needs for interpolation — never the full .env.
COMPOSE_ENV_FILE="$STORE_DIR/.compose.env"

# Generate a sanitized copy of the backend .env where lone $ in values is doubled
# to $$ so docker compose's env_file loader does not treat them as variable names
# (e.g. Fernet keys, JWT secrets that contain or look like shell variable references).
BACKEND_SAFE_ENV="$STORE_DIR/.backend_safe.env"
awk '!/^[[:space:]]*#/ && /=/ {
    n = index($0, "=")
    key = substr($0, 1, n-1)
    val = substr($0, n+1)
    # protect already-doubled $$ first, then escape lone $, then restore $$
    gsub(/\$\$/, "\001DOLDOL\001", val)
    gsub(/\$/, "$$", val)
    gsub(/\001DOLDOL\001/, "$$$$", val)
    print key "=" val
    next
} { print }' "$ENV_FILE" > "$BACKEND_SAFE_ENV"
chmod 600 "$BACKEND_SAFE_ENV"

cat > "$COMPOSE_ENV_FILE" <<EOF
PROD_DATABASE_URL=${PROD_URL}
POSTGRES_USER=${PG_USER}
POSTGRES_PASSWORD=${PG_PASS}
POSTGRES_DB=${PG_DB}
BACKEND_ENV_FILE=${BACKEND_SAFE_ENV}
EOF
chmod 600 "$COMPOSE_ENV_FILE"

# ── 4. Build images ───────────────────────────────────────────────────────────
echo ""
echo "==> Building images..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$COMPOSE_ENV_FILE" \
    build --parallel
echo ""

# ── 5. Start PostgreSQL ───────────────────────────────────────────────────────
echo "==> [1/3] Starting PostgreSQL 18..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$COMPOSE_ENV_FILE" \
    up --detach postgres

echo "    Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' agentiq-postgres 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        echo "    PostgreSQL ready  [OK]"
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo ""
        echo "ERROR: PostgreSQL did not become healthy in time."
        docker logs --tail 30 agentiq-postgres
        exit 1
    fi
    printf "."
    sleep 3
done
echo ""

# ── 6. Start Backend ──────────────────────────────────────────────────────────
echo "==> [2/3] Starting Backend..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$COMPOSE_ENV_FILE" \
    up --detach backend

echo "    Waiting for Backend to be ready..."
for i in $(seq 1 40); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' agentiq-backend 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        echo "    Backend ready  [OK]"
        break
    fi
    if [[ "$STATUS" == "unhealthy" ]]; then
        echo ""
        echo "ERROR: Backend is unhealthy. Last 40 log lines:"
        docker logs --tail 40 agentiq-backend
        exit 1
    fi
    if [[ $i -eq 40 ]]; then
        echo ""
        echo "ERROR: Backend did not become healthy in time."
        docker logs --tail 20 agentiq-backend
        exit 1
    fi
    printf "."
    sleep 3
done
echo ""

# ── 7. Start Frontend ─────────────────────────────────────────────────────────
echo "==> [3/3] Starting Frontend..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$COMPOSE_ENV_FILE" \
    up --detach frontend

echo "    Waiting for Frontend to be ready..."
for i in $(seq 1 30); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' agentiq-frontend 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        echo "    Frontend ready  [OK]"
        break
    fi
    if [[ "$STATUS" == "unhealthy" ]]; then
        echo ""
        echo "ERROR: Frontend is unhealthy. Last 20 log lines:"
        docker logs --tail 20 agentiq-frontend
        FRONTEND_FAILED=1
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo ""
        echo "WARN: Frontend did not report healthy in time (nginx may still be starting)."
        FRONTEND_FAILED=1
    fi
    printf "."
    sleep 2
done
echo ""

# ── 8. Summary ────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
SSL_CERT="$STORE_DIR/ssl/fullchain.pem"

echo "============================================================"
echo " AgentIQ stack"
echo "============================================================"
docker compose --file "$COMPOSE_FILE" ps \
    --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
echo ""
if [[ -f "$SSL_CERT" ]]; then
    echo "  Frontend  : http://${SERVER_IP}   (HTTP)"
    echo "  Frontend  : https://${SERVER_IP}  (HTTPS)"
else
    echo "  Frontend  : http://${SERVER_IP}"
    echo "  (Place certs in $STORE_DIR/ssl/ and restart to enable HTTPS)"
fi
echo "  API docs  : http://${SERVER_IP}:8000/docs"
echo "  API base  : http://${SERVER_IP}:8000/api"
echo ""
echo "  Logs : docker compose --file $COMPOSE_FILE logs -f"
echo "  Stop : docker compose --file $COMPOSE_FILE down"
echo "  Store: $STORE_DIR"
echo "============================================================"

if [[ "${FRONTEND_FAILED:-0}" == "1" ]]; then
    echo "WARN: Frontend health check did not pass — check logs above."
    exit 1
fi
