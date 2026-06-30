#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# AgentIQ — install-prereqs.sh
# Installs Docker, Python 3, pip, and boto3 if not already present.
# Runs as root (called by agentiq-install.sh).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

STORE_DIR="/opt/aiqstore"
LOG_DIR="$STORE_DIR/logs"
LOG_FILE="$LOG_DIR/prereqs-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "$LOG_DIR"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${GREEN}[prereqs]${NC} $*" | tee -a "$LOG_FILE"; }
warn()    { echo -e "${YELLOW}[prereqs]${NC} $*" | tee -a "$LOG_FILE"; }
error()   { echo -e "${RED}[prereqs]${NC} $*" | tee -a "$LOG_FILE"; }
section() { echo -e "\n${CYAN}  ── $* ──${NC}" | tee -a "$LOG_FILE"; }

info "Log: $LOG_FILE"

# ── Detect OS / package manager ───────────────────────────────────────────────
if command -v apt-get &>/dev/null; then
  PKG_MGR="apt"
elif command -v yum &>/dev/null; then
  PKG_MGR="yum"
elif command -v dnf &>/dev/null; then
  PKG_MGR="dnf"
else
  error "Unsupported package manager. Install Docker and Python 3 manually."
  exit 1
fi
info "Package manager: $PKG_MGR"

# ── Checklist state ───────────────────────────────────────────────────────────
declare -A STATUS
STATUS[docker]="MISSING"
STATUS[python3]="MISSING"
STATUS[pip3]="MISSING"
STATUS[boto3]="MISSING"
STATUS[docker_compose]="MISSING"

print_checklist() {
  echo
  echo "  ┌─────────────────────────────┬──────────────┐"
  echo "  │ Component                   │ Status       │"
  echo "  ├─────────────────────────────┼──────────────┤"
  for key in docker docker_compose python3 pip3 boto3; do
    st="${STATUS[$key]}"
    if [[ "$st" == "OK" ]];       then col="${GREEN}";
    elif [[ "$st" == "INSTALLED" ]]; then col="${GREEN}";
    elif [[ "$st" == "SKIPPED" ]];   then col="${YELLOW}";
    else col="${RED}"; fi
    printf "  │ %-27s │ %b%-12s\033[0m │\n" "$key" "$col" "$st"
  done
  echo "  └─────────────────────────────┴──────────────┘"
  echo
}

# ── Docker ────────────────────────────────────────────────────────────────────
section "Docker"
if command -v docker &>/dev/null; then
  info "Docker already installed: $(docker --version)"
  STATUS[docker]="OK"
else
  info "Installing Docker..."
  if [[ "$PKG_MGR" == "apt" ]]; then
    apt-get update -qq
    apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  elif [[ "$PKG_MGR" == "yum" || "$PKG_MGR" == "dnf" ]]; then
    $PKG_MGR install -y yum-utils
    yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    $PKG_MGR install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  fi
  systemctl enable docker
  systemctl start docker
  STATUS[docker]="INSTALLED"
  info "Docker installed: $(docker --version)"
fi

# ── Docker Compose ────────────────────────────────────────────────────────────
section "Docker Compose"
if docker compose version &>/dev/null 2>&1; then
  info "docker compose plugin available: $(docker compose version)"
  STATUS[docker_compose]="OK"
elif command -v docker-compose &>/dev/null; then
  info "docker-compose available: $(docker-compose --version)"
  STATUS[docker_compose]="OK"
else
  info "Installing docker-compose standalone..."
  COMPOSE_VERSION="v2.27.0"
  ARCH=$(uname -m)
  curl -SL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
    -o /usr/local/bin/docker-compose
  chmod +x /usr/local/bin/docker-compose
  STATUS[docker_compose]="INSTALLED"
  info "docker-compose installed: $(docker-compose --version)"
fi

# ── Python 3 ─────────────────────────────────────────────────────────────────
section "Python 3"
if command -v python3 &>/dev/null; then
  info "Python 3 already installed: $(python3 --version)"
  STATUS[python3]="OK"
else
  info "Installing Python 3..."
  if [[ "$PKG_MGR" == "apt" ]]; then
    apt-get install -y python3 python3-venv
  else
    $PKG_MGR install -y python3
  fi
  STATUS[python3]="INSTALLED"
  info "Python 3 installed: $(python3 --version)"
fi

# ── pip ───────────────────────────────────────────────────────────────────────
section "pip"
if command -v pip3 &>/dev/null; then
  info "pip3 already installed: $(pip3 --version)"
  STATUS[pip3]="OK"
else
  info "Installing pip..."
  if [[ "$PKG_MGR" == "apt" ]]; then
    apt-get install -y python3-pip
  else
    $PKG_MGR install -y python3-pip
  fi
  STATUS[pip3]="INSTALLED"
  info "pip3 installed: $(pip3 --version)"
fi

# ── boto3 ─────────────────────────────────────────────────────────────────────
section "boto3"
if python3 -c "import boto3" &>/dev/null 2>&1; then
  info "boto3 already installed."
  STATUS[boto3]="OK"
else
  info "Installing boto3..."
  pip3 install --quiet boto3
  STATUS[boto3]="INSTALLED"
  info "boto3 installed."
fi

# ── Final checklist ───────────────────────────────────────────────────────────
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Prerequisite Checklist"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
print_checklist | tee -a "$LOG_FILE"
info "Full log saved to $LOG_FILE"
