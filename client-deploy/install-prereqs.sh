#!/usr/bin/env bash
# =============================================================================
# AgentIQ — Pre-requisite Installer
#
# Installs Python 3, pip, Docker Engine, Docker Compose, curl, and tar.
# Skips anything already present at the required version.
# Logs full output to /opt/aiqstore/logs/prereq-<timestamp>.log
# Saves a plain-text checklist summary alongside the log.
#
# Usage:
#   bash install-prereqs.sh        # will sudo-escalate automatically if needed
#   sudo bash install-prereqs.sh   # run directly as root
# =============================================================================
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Constants ─────────────────────────────────────────────────────────────────
STORE_DIR="/opt/aiqstore"
LOG_DIR="$STORE_DIR/logs"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="$LOG_DIR/prereq-${TIMESTAMP}.log"
SUMMARY_FILE="$LOG_DIR/prereq-${TIMESTAMP}-summary.txt"

MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=8

# ── Colors (only when writing to a real terminal) ─────────────────────────────
if [[ -t 1 ]]; then
  G='\033[0;32m'; R='\033[0;31m'; Y='\033[1;33m'
  C='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; N='\033[0m'
else
  G=''; R=''; Y=''; C=''; BOLD=''; DIM=''; N=''
fi

# ── Root check — re-exec under sudo if needed ─────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "Root privileges required. Re-running with sudo..."
  exec sudo bash "$0" "$@"
fi

# ── Create log directory BEFORE redirecting output ───────────────────────────
mkdir -p "$LOG_DIR"
chmod 755 "$LOG_DIR"

# ── Tee all output (stdout + stderr) to the log file ─────────────────────────
# The log file starts capturing from this point onward.
exec > >(tee -a "$LOG_FILE") 2>&1

# ── Error trap ────────────────────────────────────────────────────────────────
trap '_ec=$?; printf "\n${R}[ERROR]${N} Script failed (exit %d, line %d).\n" \
  "$_ec" "${LINENO}"; printf "  Full log: %s\n\n" "$LOG_FILE"; exit $_ec' ERR

# ── Checklist storage (parallel arrays — bash 3.2 compatible) ─────────────────
CHK_NAMES=()
CHK_STATUS=()    # ok | installed | skipped | failed
CHK_VERSIONS=()
CHK_NOTES=()

record() {
  CHK_NAMES+=("$1")
  CHK_STATUS+=("$2")
  CHK_VERSIONS+=("$3")
  CHK_NOTES+=("$4")
}

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
printf "${C}${BOLD}"
echo   "  ╔══════════════════════════════════════════════════════╗"
echo   "  ║     AgentIQ — Pre-requisite Installer               ║"
echo   "  ╚══════════════════════════════════════════════════════╝"
printf "${N}"
printf "  Log : ${C}%s${N}\n\n" "$LOG_FILE"
printf "  Started : %s\n\n" "$(date)"

# ── OS detection ──────────────────────────────────────────────────────────────
if [[ -f /etc/os-release ]]; then
  # shellcheck source=/dev/null
  . /etc/os-release
  OS_ID="${ID:-unknown}"
  OS_CODENAME="${VERSION_CODENAME:-}"
  OS_PRETTY="${PRETTY_NAME:-$OS_ID}"
else
  OS_ID="unknown"
  OS_CODENAME=""
  OS_PRETTY="Unknown OS"
fi

printf "  ${BOLD}System${N} : %s\n\n" "$OS_PRETTY"

if [[ "$OS_ID" != "ubuntu" && "$OS_ID" != "debian" ]]; then
  printf "  ${Y}WARNING: Tested on Ubuntu/Debian. Other distros may need manual steps.${N}\n\n"
fi

# ── apt helpers ───────────────────────────────────────────────────────────────
_APT_UPDATED=0

apt_update_once() {
  if [[ $_APT_UPDATED -eq 0 ]]; then
    echo "    [apt] Updating package index..."
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    _APT_UPDATED=1
  fi
}

apt_install() {
  apt_update_once
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@"
}

# ── String helpers ────────────────────────────────────────────────────────────
# Right-pad or truncate $1 to $2 characters
pad() {
  local s="${1:0:$2}"
  printf "%-${2}s" "$s"
}

# Build a box-drawing separator line
sep_line() {
  local left="$1" mid="$2" right="$3"
  local widths=("$4" "$5" "$6" "$7")
  local line="  $left"
  for i in 0 1 2 3; do
    local dashes=""
    for ((j=0; j<${widths[$i]}+2; j++)); do dashes+="─"; done
    line+="$dashes"
    [[ $i -lt 3 ]] && line+="$mid"
  done
  line+="$right"
  echo "$line"
}

# =============================================================================
# Individual check / install functions
# =============================================================================

section() { printf "\n  ${BOLD}── %s${N}\n" "$1"; }

# ── curl ──────────────────────────────────────────────────────────────────────
check_curl() {
  printf "  %-26s" "curl ..."
  if command -v curl &>/dev/null; then
    local ver; ver=$(curl --version 2>/dev/null | head -n1 | awk '{print $2}')
    printf "${G}already installed${N}  (%s)\n" "$ver"
    record "curl" "ok" "$ver" ""
  else
    printf "installing ... "
    apt_install curl
    local ver; ver=$(curl --version 2>/dev/null | head -n1 | awk '{print $2}')
    printf "${G}done${N}  (%s)\n" "$ver"
    record "curl" "installed" "$ver" ""
  fi
}

# ── tar ───────────────────────────────────────────────────────────────────────
check_tar() {
  printf "  %-26s" "tar ..."
  if command -v tar &>/dev/null; then
    local ver; ver=$(tar --version 2>/dev/null | head -n1 | awk '{print $NF}')
    printf "${G}already installed${N}  (%s)\n" "$ver"
    record "tar" "ok" "$ver" ""
  else
    printf "installing ... "
    apt_install tar
    local ver; ver=$(tar --version 2>/dev/null | head -n1 | awk '{print $NF}')
    printf "${G}done${N}  (%s)\n" "$ver"
    record "tar" "installed" "$ver" ""
  fi
}

# ── Python 3 ──────────────────────────────────────────────────────────────────
PYTHON_BIN=""

check_python3() {
  printf "  %-26s" "Python 3 (>= ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}) ..."

  # Look for an acceptable Python binary, newest first
  local bin maj min
  for bin in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
    if command -v "$bin" &>/dev/null; then
      maj=$("$bin" -c "import sys; print(sys.version_info.major)" 2>/dev/null) || continue
      min=$("$bin" -c "import sys; print(sys.version_info.minor)" 2>/dev/null) || continue
      if (( maj > MIN_PYTHON_MAJOR || (maj == MIN_PYTHON_MAJOR && min >= MIN_PYTHON_MINOR) )); then
        PYTHON_BIN="$bin"
        break
      fi
    fi
  done

  if [[ -n "$PYTHON_BIN" ]]; then
    local ver; ver=$("$PYTHON_BIN" --version 2>&1 | awk '{print $2}')
    printf "${G}already installed${N}  (%s via %s)\n" "$ver" "$PYTHON_BIN"
    record "Python 3" "ok" "$ver" "$PYTHON_BIN"
  else
    printf "installing python3.11 ... \n"
    if [[ "$OS_ID" == "ubuntu" ]]; then
      apt_install software-properties-common
      add-apt-repository -y ppa:deadsnakes/ppa
      _APT_UPDATED=0   # force index refresh after new PPA
      apt_install python3.11 python3.11-distutils python3.11-venv
      PYTHON_BIN="python3.11"
    else
      apt_install python3
      PYTHON_BIN="python3"
    fi
    # Ensure python3 symlink works
    if ! command -v python3 &>/dev/null; then
      ln -sf "$(command -v "$PYTHON_BIN")" /usr/local/bin/python3
    fi
    local ver; ver=$("$PYTHON_BIN" --version 2>&1 | awk '{print $2}')
    printf "  %-26s${G}done${N}  (%s)\n" "" "$ver"
    record "Python 3" "installed" "$ver" "$PYTHON_BIN"
  fi
}

# ── pip3 ──────────────────────────────────────────────────────────────────────
check_pip3() {
  printf "  %-26s" "pip3 ..."
  local EFFECTIVE_PYTHON="${PYTHON_BIN:-python3}"

  if "$EFFECTIVE_PYTHON" -m pip --version &>/dev/null 2>&1; then
    local ver; ver=$("$EFFECTIVE_PYTHON" -m pip --version 2>/dev/null | awk '{print $2}')
    printf "${G}already installed${N}  (%s)\n" "$ver"
    record "pip3" "ok" "$ver" ""
    return
  fi

  printf "installing ... "
  if apt_install python3-pip 2>/dev/null; then
    : # success
  else
    # apt package not available for this Python version; use get-pip
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$EFFECTIVE_PYTHON"
  fi
  local ver; ver=$("$EFFECTIVE_PYTHON" -m pip --version 2>/dev/null | awk '{print $2}')
  printf "${G}done${N}  (%s)\n" "$ver"
  record "pip3" "installed" "$ver" ""
}

# ── Docker Engine ─────────────────────────────────────────────────────────────
check_docker_engine() {
  printf "  %-26s" "Docker Engine ..."
  if command -v docker &>/dev/null; then
    local ver; ver=$(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -n1)
    printf "${G}already installed${N}  (%s)\n" "$ver"
    record "Docker Engine" "ok" "$ver" ""
    return
  fi

  printf "installing from official repo ...\n"

  # Remove any old / conflicting packages
  local old_pkgs=(docker.io docker-doc docker-compose docker-compose-v2
                  podman-docker containerd runc)
  for pkg in "${old_pkgs[@]}"; do
    apt-get remove -y "$pkg" 2>/dev/null || true
  done

  # Add Docker's official GPG key
  apt_install ca-certificates gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg

  # Determine the codename (Debian may not set VERSION_CODENAME)
  local codename="${OS_CODENAME}"
  if [[ -z "$codename" ]]; then
    codename=$(lsb_release -cs 2>/dev/null || echo "jammy")
  fi

  # Add Docker apt repository
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/${OS_ID} ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list

  _APT_UPDATED=0   # force re-update after new repo
  apt_install docker-ce docker-ce-cli containerd.io \
              docker-buildx-plugin docker-compose-plugin

  local ver; ver=$(docker --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -n1)
  printf "  %-26s${G}done${N}  (%s)\n" "" "$ver"
  record "Docker Engine" "installed" "$ver" ""
}

# ── Docker Compose plugin ─────────────────────────────────────────────────────
check_docker_compose() {
  printf "  %-26s" "Docker Compose v2 ..."
  if docker compose version &>/dev/null 2>&1; then
    local ver; ver=$(docker compose version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -n1)
    printf "${G}already installed${N}  (%s)\n" "$ver"
    record "Docker Compose v2" "ok" "$ver" ""
  else
    printf "installing docker-compose-plugin ... "
    apt_install docker-compose-plugin
    local ver; ver=$(docker compose version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -n1)
    printf "${G}done${N}  (%s)\n" "$ver"
    record "Docker Compose v2" "installed" "$ver" ""
  fi
}

# ── Docker daemon ─────────────────────────────────────────────────────────────
check_docker_daemon() {
  printf "  %-26s" "Docker daemon ..."
  if docker info &>/dev/null 2>&1; then
    printf "${G}running${N}\n"
    record "Docker daemon" "ok" "running" ""
  else
    printf "starting ... "
    systemctl enable docker --quiet 2>/dev/null || true
    systemctl start  docker
    # Give daemon a moment to fully start
    local i
    for i in 1 2 3 4 5; do
      sleep 2
      if docker info &>/dev/null 2>&1; then
        printf "${G}started${N}\n"
        record "Docker daemon" "installed" "started" ""
        return
      fi
    done
    printf "${R}FAILED${N}\n"
    record "Docker daemon" "failed" "not running" "run: systemctl start docker"
  fi
}

# ── Docker group ──────────────────────────────────────────────────────────────
NEED_RELOGIN=0

check_docker_group() {
  # Only relevant when the original user (before sudo) is not root
  local original_user="${SUDO_USER:-}"
  if [[ -z "$original_user" || "$original_user" == "root" ]]; then
    record "Docker group" "skipped" "—" "running as root; no change needed"
    return
  fi

  printf "  %-26s" "Docker group ($original_user) ..."
  if id -nG "$original_user" 2>/dev/null | grep -qw docker; then
    printf "${G}already a member${N}\n"
    record "Docker group" "ok" "member" "$original_user in docker group"
  else
    usermod -aG docker "$original_user"
    printf "${G}added${N}\n"
    record "Docker group" "installed" "added" \
           "log out & in, or run: newgrp docker"
    NEED_RELOGIN=1
  fi
}

# ── /opt/aiqstore directories ─────────────────────────────────────────────────
check_store_dirs() {
  printf "  %-26s" "/opt/aiqstore/ dirs ..."
  local all_existed=1
  local dirs=("$STORE_DIR" "$LOG_DIR"
               "$STORE_DIR/postgres" "$STORE_DIR/ssl")
  local created=()
  for d in "${dirs[@]}"; do
    if [[ ! -d "$d" ]]; then
      mkdir -p "$d"
      all_existed=0
      created+=("$d")
    fi
  done
  # Permissions
  chmod 755 "$STORE_DIR" "$LOG_DIR" "$STORE_DIR/postgres" 2>/dev/null || true
  chmod 700 "$STORE_DIR/ssl" 2>/dev/null || true

  if [[ $all_existed -eq 1 ]]; then
    printf "${G}already exist${N}\n"
    record "/opt/aiqstore/ dirs" "ok" "exist" "ssl/ is 700"
  else
    printf "${G}created${N}  (${#created[@]} new)\n"
    record "/opt/aiqstore/ dirs" "installed" "created" "ssl/ is 700"
  fi
}

# =============================================================================
# Run all checks
# =============================================================================

section "Utilities"
check_curl
check_tar

section "Python"
check_python3
check_pip3

section "Docker"
check_docker_engine
check_docker_compose
check_docker_daemon
check_docker_group

section "Storage"
check_store_dirs

# =============================================================================
# Checklist table
# =============================================================================

# Column widths (content only, excludes padding)
C1=24   # Component
C2=14   # Status
C3=18   # Version / Detail
C4=34   # Notes

print_table() {
  local plain="${1:-0}"   # 1 = strip ANSI (for file output)
  local _G="$G" _R="$R" _Y="$Y" _BOLD="$BOLD" _DIM="$DIM" _N="$N"
  if [[ $plain -eq 1 ]]; then _G=''; _R=''; _Y=''; _BOLD=''; _DIM=''; _N=''; fi

  # Separator builders
  local top="  ┌$(printf '─%.0s' $(seq 1 $((C1+2))))┬$(printf '─%.0s' $(seq 1 $((C2+2))))┬$(printf '─%.0s' $(seq 1 $((C3+2))))┬$(printf '─%.0s' $(seq 1 $((C4+2))))┐"
  local mid="  ├$(printf '─%.0s' $(seq 1 $((C1+2))))┼$(printf '─%.0s' $(seq 1 $((C2+2))))┼$(printf '─%.0s' $(seq 1 $((C3+2))))┼$(printf '─%.0s' $(seq 1 $((C4+2))))┤"
  local bot="  └$(printf '─%.0s' $(seq 1 $((C1+2))))┴$(printf '─%.0s' $(seq 1 $((C2+2))))┴$(printf '─%.0s' $(seq 1 $((C3+2))))┴$(printf '─%.0s' $(seq 1 $((C4+2))))┘"

  printf "\n"
  echo "$top"
  printf "  │ ${_BOLD}%-${C1}s${_N} │ ${_BOLD}%-${C2}s${_N} │ ${_BOLD}%-${C3}s${_N} │ ${_BOLD}%-${C4}s${_N} │\n" \
    "Component" "Status" "Version / Detail" "Notes"
  echo "$mid"

  local overall_ok=1
  local i
  for i in "${!CHK_NAMES[@]}"; do
    local name="${CHK_NAMES[$i]}"
    local st="${CHK_STATUS[$i]}"
    local ver="${CHK_VERSIONS[$i]}"
    local note="${CHK_NOTES[$i]}"

    # Truncate to column width
    name="${name:0:$C1}"; ver="${ver:0:$C3}"; note="${note:0:$C4}"

    local label color
    case "$st" in
      ok)        label="✓ OK";          color="$_G" ;;
      installed) label="✓ Installed";   color="$_G" ;;
      skipped)   label="— Skipped";     color="$_DIM" ;;
      failed)    label="✗ FAILED";      color="$_R"; overall_ok=0 ;;
      *)         label="? $st";         color="$_Y" ;;
    esac

    printf "  │ %-${C1}s │ ${color}%-${C2}s${_N} │ %-${C3}s │ %-${C4}s │\n" \
      "$name" "$label" "$ver" "$note"
  done

  echo "$bot"
  printf "\n"

  # Return 1 if any item failed (used outside to branch)
  [[ $overall_ok -eq 1 ]]
}

# ── Print to console (with colors) ───────────────────────────────────────────
printf "\n${BOLD}  ═══ Pre-requisite Checklist ══════════════════════════════${N}\n"
CHECKLIST_OK=0
print_table 0 || CHECKLIST_OK=1

# ── Write plain-text summary to file ─────────────────────────────────────────
{
  echo "AgentIQ Pre-requisite Check — $(date)"
  echo "System : $OS_PRETTY"
  echo ""
  print_table 1
  echo "Log    : $LOG_FILE"
  echo "Summary: $SUMMARY_FILE"
} > "$SUMMARY_FILE" 2>&1

printf "  Summary saved : ${C}%s${N}\n" "$SUMMARY_FILE"
printf "  Full log      : ${C}%s${N}\n\n" "$LOG_FILE"

# ── Strip ANSI codes from the main log file so it's clean to read ─────────────
# (sed in-place without -i backup — GNU sed on Linux)
sed -i 's/\x1b\[[0-9;]*m//g; s/\x1b\[[?][0-9]*[lh]//g' "$LOG_FILE" 2>/dev/null || true

# =============================================================================
# Relogin notice
# =============================================================================
if [[ $NEED_RELOGIN -eq 1 ]]; then
  printf "  ${Y}⚠  '${SUDO_USER}' was added to the docker group.${N}\n"
  printf "  ${Y}   Log out and back in (or run: newgrp docker) before deploying.${N}\n\n"
fi

# =============================================================================
# Final status and next step
# =============================================================================
if [[ $CHECKLIST_OK -eq 0 ]]; then
  printf "  ${G}${BOLD}✓ All requirements satisfied.${N}\n\n"
  printf "  ${BOLD}Next step — run the deployment script:${N}\n"
  printf "    ${C}python3 %s/scripts/deploy-ecr.py${N}\n\n" "$SCRIPT_DIR"
else
  printf "  ${R}${BOLD}✗ One or more requirements failed.${N}\n"
  printf "  Review the log: %s\n\n" "$LOG_FILE"
  exit 1
fi
