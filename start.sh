#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="/opt/v1-5/backend/.env"
COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"

# ── Preflight ─────────────────────────────────────────────────────────────────
echo "==> Checking prerequisites..."

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: $ENV_FILE not found."
    echo "       Copy backend/.env.example to $ENV_FILE and fill in the values."
    exit 1
fi

PROD_URL=$(grep -E "^PROD_DATABASE_URL=" "$ENV_FILE" | head -n1 | cut -d= -f2- | tr -d '\r"' | xargs)
if [[ -z "$PROD_URL" ]]; then
    echo "ERROR: PROD_DATABASE_URL is empty in $ENV_FILE"
    exit 1
fi

echo "    ENV file : $ENV_FILE  [OK]"
echo "    Compose  : $COMPOSE_FILE"
echo ""

# ── Build images ──────────────────────────────────────────────────────────────
echo "==> Building images..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
    build --parallel
echo ""

# ── Start PostgreSQL ──────────────────────────────────────────────────────────
echo "==> [1/3] Starting PostgreSQL 18..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
    up --detach postgres

echo "    Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' agentiq-postgres 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        echo "    PostgreSQL ready  [OK]"
        break
    fi
    if [[ $i -eq 30 ]]; then
        echo "ERROR: PostgreSQL did not become healthy in time."
        docker logs --tail 20 agentiq-postgres
        exit 1
    fi
    printf "."
    sleep 3
done
echo ""

# ── Start Backend ─────────────────────────────────────────────────────────────
echo "==> [2/3] Starting Backend..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
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
        echo "ERROR: Backend did not become healthy in time."
        docker logs --tail 20 agentiq-backend
        exit 1
    fi
    printf "."
    sleep 3
done
echo ""

# ── Start Frontend ────────────────────────────────────────────────────────────
echo "==> [3/3] Starting Frontend..."
docker compose \
    --file "$COMPOSE_FILE" \
    --env-file "$ENV_FILE" \
    up --detach frontend

echo "    Waiting for Frontend to be ready..."
for i in $(seq 1 20); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' agentiq-frontend 2>/dev/null || echo "starting")
    if [[ "$STATUS" == "healthy" ]]; then
        echo "    Frontend ready  [OK]"
        break
    fi
    if [[ $i -eq 20 ]]; then
        echo "ERROR: Frontend did not become healthy in time."
        docker logs --tail 20 agentiq-frontend
        exit 1
    fi
    printf "."
    sleep 2
done
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
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
echo "============================================================"
