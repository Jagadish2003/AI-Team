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

echo "    $STORE_DIR/backend/   — backend .env"
echo "    $STORE_DIR/postgres/  — postgres data (persistent)"
echo "    $STORE_DIR/logs/      — log files"

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
    grep -E "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '\r"' | xargs
}

PROD_URL=$(read_env PROD_DATABASE_URL)
PG_USER=$(read_env POSTGRES_USER)
PG_PASS=$(read_env POSTGRES_PASSWORD)
PG_DB=$(read_env POSTGRES_DB)

[[ -z "$PG_USER" ]]  && PG_USER="agentiq"
[[ -z "$PG_PASS" ]]  && PG_PASS="agentiq_secret"
[[ -z "$PG_DB" ]]    && PG_DB="agentiqprod"

if [[ -z "$PROD_URL" ]]; then
    echo "ERROR: PROD_DATABASE_URL is empty in $ENV_FILE"
    exit 1
fi

echo "    ENV file : $ENV_FILE  [OK]"
echo "    DB       : $PG_USER@.../$PG_DB"

# ── 3. Generate minimal compose env (avoids passing full .env to compose) ─────
# This prevents docker compose from warning about unrecognised variable names
# that appear as values (e.g. Fernet keys, JWT secrets) in the .env file.
COMPOSE_ENV_FILE="$STORE_DIR/.compose.env"
cat > "$COMPOSE_ENV_FILE" <<EOF
PROD_DATABASE_URL=${PROD_URL}
POSTGRES_USER=${PG_USER}
POSTGRES_PASSWORD=${PG_PASS}
POSTGRES_DB=${PG_DB}
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
for i in $(seq 1 20); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' agentiq-frontend 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        echo "    Frontend ready  [OK]"
        break
    fi
    if [[ $i -eq 20 ]]; then
        echo ""
        echo "ERROR: Frontend did not become healthy in time."
        docker logs --tail 20 agentiq-frontend
        exit 1
    fi
    printf "."
    sleep 2
done
echo ""

# ── 8. Summary ────────────────────────────────────────────────────────────────
echo "============================================================"
echo " AgentIQ stack is running"
echo "============================================================"
docker compose --file "$COMPOSE_FILE" ps \
    --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
echo ""
SERVER_IP=$(hostname -I | awk '{print $1}')
echo "  App  : http://${SERVER_IP}"
echo "  API  : http://${SERVER_IP}:8000/docs"
echo ""
echo "  Logs : docker compose --file $COMPOSE_FILE logs -f"
echo "  Stop : docker compose --file $COMPOSE_FILE down"
echo "  Store: $STORE_DIR"
echo "============================================================"
