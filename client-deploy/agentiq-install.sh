#!/usr/bin/env bash
# =============================================================================
# AgentIQ — On-Premise Installer
#
# Single entry point. Run this file — it handles everything in order:
#   1. install-prereqs.sh   — installs Python, Docker, and supporting tools
#   2. scripts/deploy-ecr.py — configures .env, pulls images, starts the stack
#
# Usage:
#   bash install.sh
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

# ── Verify sub-scripts are present ───────────────────────────────────────────
PREREQ_SCRIPT="$SCRIPT_DIR/install-prereqs.sh"
DEPLOY_SCRIPT="$SCRIPT_DIR/scripts/deploy-ecr.py"

missing=0
for f in "$PREREQ_SCRIPT" "$DEPLOY_SCRIPT"; do
  if [[ ! -f "$f" ]]; then
    printf "  ${R}[ERROR]${N} Required file not found: %s\n" "$f"
    missing=1
  fi
done
if [[ $missing -eq 1 ]]; then
  echo ""
  echo "  The install package is incomplete. Re-download and retry."
  exit 1
fi

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

bash "$PREREQ_SCRIPT"

echo ""
printf "  ${G}${BOLD}✓ Step 1 complete.${N}\n"
echo ""

# =============================================================================
# Step 2 — Deploy
# =============================================================================
printf "${BOLD}  ══ Step 2 / 2 — Application Deployment ═══════════════════${N}\n\n"

# python3 is guaranteed to exist at this point (installed by step 1).
python3 "$DEPLOY_SCRIPT"

# deploy-ecr.py prints its own completion banner and URL, so no extra message
# is needed here. We only reach this line if deploy succeeded (exit 0).
echo ""
printf "  ${G}${BOLD}✓ Installation complete.${N}\n"
echo ""
