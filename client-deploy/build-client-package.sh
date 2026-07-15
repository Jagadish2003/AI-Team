#!/usr/bin/env bash
# =============================================================================
# AgentIQ — Build the CLIENT package  (vendor build machine only)
#
# Produces the bundle you hand to a client for the ONLINE (ECR pull) flow.
# The client runs it and enters AWS Account ID + Access Key + Secret at the
# prompt (nothing AWS is baked into the scripts).
#
#   agentiq-client-<version>.tar.gz
#     ├─ agentiq-install.sh        (or compiled binary: agentiq-install)
#     ├─ install-prereqs.sh        (or install-prereqs)
#     ├─ Configfile-create.sh      (or Configfile-create)
#     ├─ docker-compose.yml
#     └─ scripts/deploy-ecr.py     (or scripts/deploy-ecr)
#
# Default: ships readable source.
# --compile: ships the scripts as BINARIES (shc for shell, Nuitka for Python)
#            so they are not casually readable on the client box.
#
#   IMPORTANT — honest limit: compiled binaries are OBFUSCATION, not
#   encryption. The client runs as root on their own machine and can, with
#   effort, still recover the logic. What is genuinely protected is the AWS
#   surface: no account id or credentials are in the bundle — the client
#   supplies them at run time.
#
# Build-host tools for --compile:  shc  (apt-get install shc)
#                                  nuitka  (pip install nuitka) + a C compiler
#
# Usage (Linux build machine):
#   bash client-deploy/build-client-package.sh            # readable source
#   bash client-deploy/build-client-package.sh --compile  # binaries
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_VERSION="1.7.0"
COMPILE=0
[[ "${1:-}" == "--compile" ]] && COMPILE=1

OUT_DIR="$SCRIPT_DIR/dist"
BUNDLE="$OUT_DIR/agentiq-client-$IMAGE_VERSION"
TARBALL="$OUT_DIR/agentiq-client-$IMAGE_VERSION.tar.gz"

if [[ -t 1 ]]; then G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; N='\033[0m'
else G=''; R=''; Y=''; C=''; N=''; fi
info() { printf "  ${C}%s${N}\n" "$*"; }
ok()   { printf "  ${G}OK${N}  %s\n" "$*"; }
fail() { printf "  ${R}ERROR${N} %s\n" "$*" >&2; exit 1; }

echo ""
echo "  =============================================="
echo "   AgentIQ - Build client package ($IMAGE_VERSION)$([[ $COMPILE -eq 1 ]] && echo '  [compiled]')"
echo "  =============================================="
echo ""

# ── Assemble source skeleton ─────────────────────────────────────────────────
rm -rf "$BUNDLE"; mkdir -p "$BUNDLE/scripts"
cp "$SCRIPT_DIR/agentiq-install.sh"    "$BUNDLE/"
cp "$SCRIPT_DIR/install-prereqs.sh"    "$BUNDLE/"
cp "$SCRIPT_DIR/Configfile-create.sh"  "$BUNDLE/"
cp "$SCRIPT_DIR/docker-compose.yml"    "$BUNDLE/"
cp "$SCRIPT_DIR/scripts/deploy-ecr.py" "$BUNDLE/scripts/"
find "$BUNDLE" -type f \( -name "*.sh" -o -name "*.py" -o -name "*.yml" \) -exec sed -i 's/\r$//' {} +
ok "Skeleton assembled"

if [[ $COMPILE -eq 1 ]]; then
  # ── Compile shell scripts with shc ─────────────────────────────────────────
  command -v shc >/dev/null 2>&1 || fail "shc not found (apt-get install shc). Omit --compile for source."
  for s in agentiq-install install-prereqs Configfile-create; do
    shc -r -f "$BUNDLE/$s.sh" -o "$BUNDLE/$s"
    chmod +x "$BUNDLE/$s"
    rm -f "$BUNDLE/$s.sh" "$BUNDLE/$s.sh.x.c"
    ok "shc: $s"
  done
  # ── Compile the Python orchestrator with Nuitka ────────────────────────────
  python3 -c "import nuitka" 2>/dev/null || fail "nuitka not found (pip install nuitka). Omit --compile for source."
  ( cd "$BUNDLE/scripts" && python3 -m nuitka --onefile --assume-yes-for-downloads --remove-output \
        deploy-ecr.py -o deploy-ecr ) || fail "nuitka compile failed."
  chmod +x "$BUNDLE/scripts/deploy-ecr"
  rm -f "$BUNDLE/scripts/deploy-ecr.py"
  ok "nuitka: deploy-ecr"
  echo ""
  printf "  ${Y}Note: compiled binaries are obfuscation, not encryption. A root user${N}\n"
  printf "  ${Y}on the client machine can still recover the logic with effort. The AWS${N}\n"
  printf "  ${Y}account and credentials are protected by being entered at run time,${N}\n"
  printf "  ${Y}never stored in the bundle.${N}\n"
fi

# ── Package ───────────────────────────────────────────────────────────────────
( cd "$OUT_DIR" && tar czf "$TARBALL" "agentiq-client-$IMAGE_VERSION" )
rm -rf "$BUNDLE"
echo ""
echo "  =============================================="
printf "   ${G}Client package ready:${N}\n"
echo "     $TARBALL  ($(du -h "$TARBALL" | cut -f1))"
echo "  =============================================="
echo ""
echo "  The client runs:"
echo "     tar xzf agentiq-client-$IMAGE_VERSION.tar.gz && cd agentiq-client-$IMAGE_VERSION"
echo "     sudo bash agentiq-install.sh          # (or: sudo ./agentiq-install if compiled)"
echo "  and enters Account ID + Access Key + Secret at the prompt."
echo ""
echo "  Prerequisite: images must already be in ECR (run build-and-push-ecr.sh)."
echo ""
