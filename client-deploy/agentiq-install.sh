#!/usr/bin/env bash
# =============================================================================
# AgentIQ — On-Premise Installer
#
# Single entry point. Run this file — it handles everything in order:
#   1. install-prereqs.sh   — installs Python, Docker, and supporting tools
#   2. scripts/deploy-ecr.py — configures .env, pulls images, starts the stack
#
# Usage:
#   bash agentiq-install.sh
#   (sudo escalation happens automatically — you will be prompted once)
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colors ────────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'
  C='\033[0;36m'; BOLD='\033[1m'; N='\033[0m'
else
  G=''; R=''; Y=''; C=''; BOLD=''; N=''
fi

# ── Single sudo escalation — both child scripts inherit root ──────────────────
# Centralising this here avoids a second password prompt from the sub-scripts.
if [[ $EUID -ne 0 ]]; then
  echo ""
  echo "  AgentIQ installer requires root privileges."
  echo "  You will be prompted for your sudo password once."
  echo ""
  exec sudo bash "$0" "$@"
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
printf "${C}${BOLD}"
echo   "  ╔══════════════════════════════════════════════════════╗"
echo   "  ║          AgentIQ — On-Premise Installer             ║"
echo   "  ╚══════════════════════════════════════════════════════╝"
printf "${N}"
echo ""
echo "  This installer will:"
printf "    ${BOLD}Step 1${N}  Install Python, Docker, and required tools\n"
printf "    ${BOLD}Step 2${N}  Configure the application, pull images, start the stack\n"
echo ""
printf "  ${DIM:-}Started : $(date)${N}\n"
echo ""

# ── Resolve sub-steps: prefer a compiled binary, fall back to source ─────────
# The hardened build (build-client-package.sh --compile) ships binaries with no
# extension (install-prereqs, scripts/deploy-ecr); the plain build ships the
# .sh / .py source. Support both transparently.
if [[ -x "$SCRIPT_DIR/install-prereqs" ]]; then
  PREREQ_CMD=("$SCRIPT_DIR/install-prereqs")
elif [[ -f "$SCRIPT_DIR/install-prereqs.sh" ]]; then
  PREREQ_CMD=(bash "$SCRIPT_DIR/install-prereqs.sh")
else
  printf "  ${R}[ERROR]${N} Prerequisite step not found (install-prereqs[.sh]).\n"; exit 1
fi

if [[ -x "$SCRIPT_DIR/scripts/deploy-ecr" ]]; then
  DEPLOY_CMD=("$SCRIPT_DIR/scripts/deploy-ecr")
elif [[ -f "$SCRIPT_DIR/scripts/deploy-ecr.py" ]]; then
  DEPLOY_CMD=(python3 "$SCRIPT_DIR/scripts/deploy-ecr.py")
else
  printf "  ${R}[ERROR]${N} Deploy step not found (scripts/deploy-ecr[.py]).\n"; exit 1
fi

# ── Pre-check: required ports must be free ────────────────────────────────────
# AgentIQ needs 80/443 (frontend) and 8000 (backend). Detect conflicts NOW,
# before any installation work, instead of failing 15 minutes in at deploy.
# Listeners that are AgentIQ's own containers are fine (re-run / upgrade).
echo "  Checking required ports (80, 443, 8000) ..."
PORT_CONFLICT=0
for port in 80 443 8000; do
  # ss output example: users:(("nginx",pid=123,fd=6))
  line="$(ss -ltnpH "sport = :$port" 2>/dev/null | head -1 || true)"
  [[ -z "$line" ]] && continue
  proc="$(sed -n 's/.*users:(("\([^"]*\)",pid=\([0-9]*\).*/\1 (pid \2)/p' <<<"$line")"
  # Our own stack? docker-proxy listeners backed by an agentiq-* container are OK.
  if [[ "$proc" == docker-proxy* ]] && command -v docker >/dev/null 2>&1 \
     && docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null | grep -E "^agentiq-" | grep -q ":$port->"; then
    printf "    ${Y}Port %s is held by an existing AgentIQ container — it will be replaced.${N}\n" "$port"
    continue
  fi
  printf "    ${R}Port %s is in use by: %s${N}\n" "$port" "${proc:-unknown process}"
  PORT_CONFLICT=1
done
if [[ $PORT_CONFLICT -eq 1 ]]; then
  echo ""
  printf "  ${R}${BOLD}Cannot continue: required ports are in use.${N}\n"
  echo "  Free them first, e.g. for a preinstalled web server:"
  echo "      sudo systemctl stop apache2 nginx"
  echo "      sudo systemctl disable apache2 nginx"
  echo "  Identify other listeners with:"
  echo "      sudo ss -ltnp | grep -E ':80 |:443 |:8000 '"
  echo "  Then re-run this installer."
  echo ""
  exit 1
fi
printf "    ${G}All required ports are free.${N}\n\n"

# ── Error trap ────────────────────────────────────────────────────────────────
trap '_ec=$?
  echo ""
  printf "  ${R}${BOLD}Installation failed (exit %d).${N}\n" "$_ec"
  echo "  Review the output above for details."
  echo "  Logs are in /opt/aiqstore/logs/"
  echo ""
  exit $_ec' ERR

# =============================================================================
# Step 1 — Pre-requisites
# =============================================================================
printf "${BOLD}  ══ Step 1 / 2 — Pre-requisite Installation ═══════════════${N}\n\n"

"${PREREQ_CMD[@]}"

echo ""
printf "  ${G}${BOLD}✓ Step 1 complete.${N}\n"
echo ""

# =============================================================================
# Step 2 — Deploy
# =============================================================================
printf "${BOLD}  ══ Step 2 / 2 — Application Deployment ═══════════════════${N}\n\n"

# Strip Windows CRLF (\r) from any source scripts (no-op for compiled binaries).
printf "  Normalising line endings ...\n"
find "$SCRIPT_DIR" -type f \( -name "*.py" -o -name "*.sh" \) \
  -exec sed -i 's/\r//' {} + 2>/dev/null || true
printf "  ${G}done${N}\n\n"

# python3 is guaranteed to exist at this point (installed by step 1).
"${DEPLOY_CMD[@]}"

# deploy-ecr.py prints its own completion banner and URL, so no extra message
# is needed here. We only reach this line if deploy succeeded (exit 0).
echo ""
printf "  ${G}${BOLD}✓ Installation complete.${N}\n"
echo ""
