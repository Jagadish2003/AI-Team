#!/usr/bin/env bash
# =============================================================================
# AgentIQ — Build & Push images to AWS ECR
#
# Run on any machine with Docker and this repository checked out (Linux/macOS/
# WSL). Builds the three AgentIQ images and pushes them to ECR with exactly
# the tags client-deploy/docker-compose.yml pulls:
#
#     <registry>/agentiq:postgres-1.7.0
#     <registry>/agentiq:backend-1.7.0
#     <registry>/agentiq:frontend-1.7.0
#
# Bump IMAGE_VERSION below for each release so every image carries an
# immutable, traceable tag (no moving "latest") - the client compose pins
# the exact version, so a later push cannot silently change a running client.
#
# AWS credentials are PROMPTED interactively (secret hidden), used only for
# the login token, and cleared afterwards. Nothing is written to disk.
#
# Usage:
#   bash client-deploy/build-and-push-ecr.sh
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DEFAULT_ACCOUNT_ID="393354520949"
DEFAULT_REGION="us-east-1"
ECR_REPO="agentiq"
IMAGE_VERSION="1.7.0"    # bump per release; client compose pins this exact version

if [[ -t 1 ]]; then G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'; C='\033[0;36m'; N='\033[0m'
else G=''; R=''; Y=''; C=''; N=''; fi
info() { printf "  ${C}%s${N}\n" "$*"; }
ok()   { printf "  ${G}OK${N}  %s\n" "$*"; }
fail() { printf "  ${R}ERROR${N} %s\n" "$*" >&2; exit 1; }

# ── Preflight ────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || fail "Docker is not installed (or not on PATH)."
docker info >/dev/null 2>&1       || fail "Docker daemon is not running (or you lack permission — try sudo)."
for f in postgres/Dockerfile backend/Dockerfile frontend/Dockerfile; do
    [[ -f "$REPO_ROOT/$f" ]] || fail "Missing $f — run from a full AgentIQ checkout."
done

echo ""
echo "  =============================================="
echo "   AgentIQ - Build & Push to AWS ECR"
echo "  =============================================="
echo ""

# ── Prompt for registry + credentials ────────────────────────────────────────
read -r -p "  AWS Account ID   [$DEFAULT_ACCOUNT_ID]: " ACCOUNT_ID
ACCOUNT_ID="${ACCOUNT_ID:-$DEFAULT_ACCOUNT_ID}"
read -r -p "  AWS Region       [$DEFAULT_REGION]: " REGION
REGION="${REGION:-$DEFAULT_REGION}"
read -r -p "  AWS Access Key ID     : " AWS_ACCESS_KEY_ID
read -r -s -p "  AWS Secret Access Key : " AWS_SECRET_ACCESS_KEY; echo ""
read -r -s -p "  AWS Session Token (Enter if none) : " AWS_SESSION_TOKEN; echo ""
[[ -n "$AWS_ACCESS_KEY_ID" && -n "$AWS_SECRET_ACCESS_KEY" ]] || fail "Access Key ID and Secret Access Key are required."
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
[[ -n "$AWS_SESSION_TOKEN" ]] && export AWS_SESSION_TOKEN

REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
cleanup() {
    docker logout "$REGISTRY" >/dev/null 2>&1 || true
    unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
}
trap cleanup EXIT

# ── ECR login (aws CLI if present, otherwise python3 + boto3) ────────────────
info "Logging in to $REGISTRY ..."
if command -v aws >/dev/null 2>&1; then
    aws ecr get-login-password --region "$REGION" \
        | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null \
        || fail "ECR login failed — check the credentials and region."
    # Ensure the repository exists (idempotent)
    aws ecr describe-repositories --region "$REGION" --repository-names "$ECR_REPO" >/dev/null 2>&1 \
        || aws ecr create-repository --region "$REGION" --repository-name "$ECR_REPO" >/dev/null \
        && ok "ECR repository '$ECR_REPO' ready"
else
    command -v python3 >/dev/null 2>&1 || fail "Neither aws CLI nor python3 found — install one of them."
    python3 -c "import boto3" 2>/dev/null || {
        info "Installing boto3 (one-time) ..."
        python3 -m pip install --quiet boto3 || fail "pip install boto3 failed."
    }
    PASSWORD="$(python3 - "$REGION" "$ECR_REPO" <<'PYEOF'
import base64, sys, boto3
region, repo = sys.argv[1], sys.argv[2]
ecr = boto3.client("ecr", region_name=region)   # creds come from the exported env vars
try:
    ecr.create_repository(repositoryName=repo)
except ecr.exceptions.RepositoryAlreadyExistsException:
    pass
tok = ecr.get_authorization_token()["authorizationData"][0]["authorizationToken"]
print(base64.b64decode(tok).decode().split(":", 1)[1])
PYEOF
)" || fail "Could not obtain ECR token — check the credentials and region."
    printf '%s' "$PASSWORD" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null \
        || fail "docker login failed."
    unset PASSWORD
    ok "ECR repository '$ECR_REPO' ready"
fi
ok "Logged in"

# ── Build ─────────────────────────────────────────────────────────────────────
cd "$REPO_ROOT"
echo ""
info "Building images (this can take several minutes on first run) ..."
docker build -t "$REGISTRY/$ECR_REPO:postgres-$IMAGE_VERSION" -f postgres/Dockerfile .  || fail "postgres image build failed."
ok "postgres-$IMAGE_VERSION built"
docker build -t "$REGISTRY/$ECR_REPO:backend-$IMAGE_VERSION" backend/                   || fail "backend image build failed."
ok "backend-$IMAGE_VERSION built"
docker build -t "$REGISTRY/$ECR_REPO:frontend-$IMAGE_VERSION" frontend/                 || fail "frontend image build failed."
ok "frontend-$IMAGE_VERSION built"

# ── Push ─────────────────────────────────────────────────────────────────────
echo ""
info "Pushing to $REGISTRY/$ECR_REPO ..."
for svc in postgres backend frontend; do
    tag="$svc-$IMAGE_VERSION"
    docker push "$REGISTRY/$ECR_REPO:$tag" || fail "push failed for tag $tag."
    ok "pushed $tag"
done

echo ""
echo "  =============================================="
printf "   ${G}All images pushed to %s/%s${N}\n" "$REGISTRY" "$ECR_REPO"
echo "  =============================================="
echo ""
echo "  Deploy on the target server (Ubuntu 24.04) with:"
echo "     bash client-deploy/agentiq-install.sh"
echo "  (it prompts for AWS credentials again for the pull)"
echo ""
