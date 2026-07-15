#!/usr/bin/env bash
# =============================================================================
# AgentIQ — Build SEALED offline bundle  (vendor build machine only)
#
# Produces a self-contained client bundle that needs NO AWS account, NO
# credentials, and NO registry access on the client side:
#
#     agentiq-sealed-<version>.tar.gz
#       ├─ agentiq-install.sh          entry point (client runs this)
#       ├─ install-prereqs.sh          Docker/Python install
#       ├─ Configfile-create.sh        app config prompts
#       ├─ docker-compose.yml          the stack
#       ├─ scripts/deploy-ecr.py       orchestrator (auto-detects offline mode)
#       └─ images/agentiq-images-<version>.tar.gz   the 3 pre-built images
#
# The images are saved WITH their full tag, so the compose reference resolves
# to the loaded local images and Docker never contacts a registry.
#
# Optional hardening (--compile): compile the orchestration scripts to binaries
# (shc for shell, Nuitka for Python) so they are not casually readable. NOTE:
# this is obfuscation, not encryption — a determined root user on the client
# box can still recover the logic. The real protection is that no AWS account,
# credential, or registry logic is present in the bundle at all.
#
# Usage (on a Linux build machine with Docker):
#   bash client-deploy/build-sealed-bundle.sh              # source orchestration
#   bash client-deploy/build-sealed-bundle.sh --compile    # + compile to binaries
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ACCOUNT_ID="393354520949"          # baked into image tags (identifier, not a secret)
REGION="us-east-1"
ECR_REPO="agentiq"
IMAGE_VERSION="1.7.0"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

COMPILE=0
[[ "${1:-}" == "--compile" ]] && COMPILE=1

OUT_DIR="$SCRIPT_DIR/dist"
BUNDLE="$OUT_DIR/agentiq-sealed-$IMAGE_VERSION"
TARBALL="$OUT_DIR/agentiq-sealed-$IMAGE_VERSION.tar.gz"

if [[ -t 1 ]]; then G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; N='\033[0m'
else G=''; R=''; Y=''; C=''; N=''; fi
info() { printf "  ${C}%s${N}\n" "$*"; }
ok()   { printf "  ${G}OK${N}  %s\n" "$*"; }
fail() { printf "  ${R}ERROR${N} %s\n" "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker is not installed."
docker info >/dev/null 2>&1       || fail "Docker daemon not running (try sudo)."
for f in postgres/Dockerfile backend/Dockerfile frontend/Dockerfile; do
    [[ -f "$REPO_ROOT/$f" ]] || fail "Missing $f — run from a full AgentIQ checkout."
done

echo ""
echo "  =============================================="
echo "   AgentIQ - Build SEALED offline bundle ($IMAGE_VERSION)"
echo "  =============================================="
echo ""

# ── 1. Build images (tagged with the full registry path) ─────────────────────
cd "$REPO_ROOT"
info "Building images ..."
docker build -t "$REGISTRY/$ECR_REPO:postgres-$IMAGE_VERSION" -f postgres/Dockerfile . || fail "postgres build failed."
ok "postgres-$IMAGE_VERSION"
docker build -t "$REGISTRY/$ECR_REPO:backend-$IMAGE_VERSION"  backend/                 || fail "backend build failed."
ok "backend-$IMAGE_VERSION"
docker build -t "$REGISTRY/$ECR_REPO:frontend-$IMAGE_VERSION" frontend/                || fail "frontend build failed."
ok "frontend-$IMAGE_VERSION"

# ── 2. Assemble the bundle skeleton ──────────────────────────────────────────
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/scripts" "$BUNDLE/images"
cp "$SCRIPT_DIR/agentiq-install.sh"        "$BUNDLE/"
cp "$SCRIPT_DIR/install-prereqs.sh"        "$BUNDLE/"
cp "$SCRIPT_DIR/Configfile-create.sh"      "$BUNDLE/"
cp "$SCRIPT_DIR/docker-compose.yml"        "$BUNDLE/"
cp "$SCRIPT_DIR/scripts/deploy-ecr.py"     "$BUNDLE/scripts/"
# Normalise line endings so the bundle is clean regardless of build host.
find "$BUNDLE" -type f \( -name "*.sh" -o -name "*.py" -o -name "*.yml" \) -exec sed -i 's/\r$//' {} +
ok "Bundle skeleton assembled"

# ── 3. Save the images into the bundle ───────────────────────────────────────
info "Saving images (this is the large step) ..."
docker save \
    "$REGISTRY/$ECR_REPO:postgres-$IMAGE_VERSION" \
    "$REGISTRY/$ECR_REPO:backend-$IMAGE_VERSION" \
    "$REGISTRY/$ECR_REPO:frontend-$IMAGE_VERSION" \
    | gzip -1 > "$BUNDLE/images/agentiq-images-$IMAGE_VERSION.tar.gz"
ok "Images saved ($(du -h "$BUNDLE/images/agentiq-images-$IMAGE_VERSION.tar.gz" | cut -f1))"

# ── 4. Optional: compile orchestration scripts to binaries ───────────────────
if [[ $COMPILE -eq 1 ]]; then
    info "Compiling orchestration to binaries (best-effort obfuscation) ..."
    if command -v shc >/dev/null 2>&1; then
        for s in agentiq-install install-prereqs Configfile-create; do
            if [[ -f "$BUNDLE/$s.sh" ]]; then
                shc -r -f "$BUNDLE/$s.sh" -o "$BUNDLE/$s" && rm -f "$BUNDLE/$s.sh" "$BUNDLE/$s.sh.x.c"
                ok "shc: $s"
            fi
        done
    else
        printf "    ${Y}shc not found — shell scripts left as source. Install: apt-get install shc${N}\n"
    fi
    if command -v nuitka3 >/dev/null 2>&1 || python3 -c "import nuitka" 2>/dev/null; then
        ( cd "$BUNDLE/scripts" && python3 -m nuitka --onefile --quiet deploy-ecr.py -o deploy-ecr 2>/dev/null ) \
            && rm -f "$BUNDLE/scripts/deploy-ecr.py" && ok "nuitka: deploy-ecr" \
            || printf "    ${Y}nuitka compile failed — deploy-ecr.py left as source.${N}\n"
    else
        printf "    ${Y}nuitka not found — deploy-ecr.py left as source. Install: pip install nuitka${N}\n"
    fi
    printf "    ${Y}Note: compiled binaries are obfuscation, not encryption — a root${N}\n"
    printf "    ${Y}user on the client box can still recover the logic with effort.${N}\n"
fi

# ── 5. Package ────────────────────────────────────────────────────────────────
info "Packaging ..."
( cd "$OUT_DIR" && tar czf "$TARBALL" "agentiq-sealed-$IMAGE_VERSION" )
rm -rf "$BUNDLE"
echo ""
echo "  =============================================="
printf "   ${G}Sealed bundle ready:${N}\n"
echo "     $TARBALL  ($(du -h "$TARBALL" | cut -f1))"
echo "  =============================================="
echo ""
echo "  Deliver that ONE file to the client. They run:"
echo "     tar xzf agentiq-sealed-$IMAGE_VERSION.tar.gz"
echo "     cd agentiq-sealed-$IMAGE_VERSION"
echo "     sudo bash agentiq-install.sh      (auto-detects offline images; no AWS)"
echo ""
