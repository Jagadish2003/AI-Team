#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ — agentiq-install.sh
# Single entry point for client deployment.
#
# Steps:
#   1. Escalate to root once (all child scripts inherit root)
#   2. Install prerequisites (Docker, Python 3, boto3)
#   3. Create /opt/aiqstore/.env configuration
#   4. Strip Windows CRLF from all scripts
#   5. Pull images from ECR and start containers
#
# Usage:
#   bash agentiq-install.sh
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STORE_DIR="/opt/aiqstore"
LOG_DIR="$STORE_DIR/logs"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${GREEN}[install]${NC} $*"; }
warn()    { echo -e "${YELLOW}[install]${NC} $*"; }
error()   { echo -e "${RED}[install]${NC} $*"; }
banner()  { echo -e "\n${BOLD}$*${NC}\n"; }

# ── Single root escalation ────────────────────────────────────────────────────
if [[ "$EUID" -ne 0 ]]; then
  echo -e "${YELLOW}[install]${NC} Root privileges required. Re-running with sudo..."
  exec sudo bash "$0" "$@"
fi

mkdir -p "$STORE_DIR" "$LOG_DIR"

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BOLD}       AgentIQ Installation${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# ── Step 1: Install prerequisites ────────────────────────────────────────────
banner "Step 1 / 4 — Prerequisites"
bash "$SCRIPT_DIR/install-prereqs.sh"

# ── Step 2: Configure environment ────────────────────────────────────────────
banner "Step 2 / 4 — Environment Configuration"
bash "$SCRIPT_DIR/Configfile-create.sh"

# ── Step 3: Normalise line endings ────────────────────────────────────────────
banner "Step 3 / 4 — Preparing deploy scripts"
info "Stripping Windows CRLF from scripts..."
find "$SCRIPT_DIR" -name "*.py" -o -name "*.sh" -o -name "*.yml" | while read -r f; do
  sed -i 's/\r//' "$f"
done
info "Done."

# ── Step 4: Deploy ────────────────────────────────────────────────────────────
banner "Step 4 / 4 — Pull images and start containers"
python3 "$SCRIPT_DIR/scripts/deploy-ecr.py"

echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
info "Installation complete."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
