#!/usr/bin/env bash
# =============================================================================
# AgentIQ — Enable SSL on the running frontend container (addon, no rebuild).
#
# Copies certs + the HTTPS nginx config into the live container and reloads
# nginx. Changes survive container restarts only if certs are mounted via
# docker-compose.yml (which they are after the first `bash start.sh` rebuild).
#
# Usage:
#   bash scripts/enable-ssl.sh                        # uses /opt/certs/ssl
#   bash scripts/enable-ssl.sh --certs /custom/path   # custom cert directory
# =============================================================================
set -euo pipefail

CERTS_DIR="/opt/certs/ssl"
CONTAINER="agentiq-frontend"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SSL_CONF="$SCRIPT_DIR/../frontend/nginx-ssl.conf"

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --certs|-c) CERTS_DIR="$2"; shift 2 ;;
    -h|--help)  echo "Usage: $0 [--certs /path/to/ssl]"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Colours ──────────────────────────────────────────────────────────────────
G='\033[0;32m' R='\033[0;31m' Y='\033[1;33m' C='\033[0;36m' N='\033[0m'

echo -e "${C}==> AgentIQ SSL enablement (addon — no rebuild)${N}"

# ── Preflight ────────────────────────────────────────────────────────────────
for f in fullchain.pem privkey.pem; do
  if [[ ! -f "$CERTS_DIR/$f" ]]; then
    echo -e "${R}ERROR: $CERTS_DIR/$f not found.${N}"
    exit 1
  fi
done
echo "    Certs    : $CERTS_DIR  [OK]"

if ! docker inspect "$CONTAINER" --format='{{.State.Status}}' 2>/dev/null | grep -q "^running"; then
  echo -e "${R}ERROR: Container $CONTAINER is not running.${N}"
  echo "       Start the stack first: bash start.sh"
  exit 1
fi
echo "    Container: $CONTAINER  [running]"

if [[ ! -f "$SSL_CONF" ]]; then
  echo -e "${R}ERROR: nginx-ssl.conf not found at $SSL_CONF${N}"
  echo "       Pull the latest dev-docker branch first."
  exit 1
fi

# ── Copy certs into container ────────────────────────────────────────────────
echo ""
echo "==> Copying SSL certificates into container..."
docker exec "$CONTAINER" mkdir -p /etc/nginx/ssl
docker cp "$CERTS_DIR/fullchain.pem" "$CONTAINER:/etc/nginx/ssl/fullchain.pem"
docker cp "$CERTS_DIR/privkey.pem"   "$CONTAINER:/etc/nginx/ssl/privkey.pem"
echo "    fullchain.pem  [OK]"
echo "    privkey.pem    [OK]"

# ── Activate SSL nginx config ─────────────────────────────────────────────────
echo ""
echo "==> Activating HTTPS nginx config..."
docker cp "$SSL_CONF" "$CONTAINER:/etc/nginx/conf.d/default.conf"

# ── Test and reload ───────────────────────────────────────────────────────────
echo ""
echo "==> Testing nginx configuration..."
docker exec "$CONTAINER" nginx -t

echo ""
echo "==> Reloading nginx..."
docker exec "$CONTAINER" nginx -s reload

# ── Done ─────────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${G}============================================================${N}"
echo -e "${G} SSL enabled on $CONTAINER${N}"
echo -e "${G}============================================================${N}"
echo "  HTTPS    : https://${SERVER_IP}"
echo "  HTTP     : http://${SERVER_IP}  (redirects to HTTPS)"
echo ""
echo -e "${Y}NOTE: This change is in-container only and will be lost if the${N}"
echo -e "${Y}      container is removed. To make it permanent:${N}"
echo "        git pull && docker compose -f /opt/agentiq/AgentIQ/docker-compose.yml \\"
echo "          build frontend && \\"
echo "          docker compose -f /opt/agentiq/AgentIQ/docker-compose.yml \\"
echo "          up --detach --force-recreate frontend"
echo ""
