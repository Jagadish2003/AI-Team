#!/usr/bin/env python3
"""
deploy-dockerhub.py — Pull AgentIQ images from Docker Hub and start the stack.

No extra dependencies — uses only Python stdlib + Docker CLI.
Suitable for client on-premise deployment.

Prerequisites (cannot be auto-installed):
    - Python 3.8+
    - Docker + docker compose running
    - docker-compose.yml present at the repo root (parent of scripts/)
    - /opt/aiqstore/backend/.env populated
      (copy from backend/.env.example and fill in values)
"""

import getpass
import json
import os
import pathlib
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# !! CONFIGURE THIS before distributing to clients !!
# ---------------------------------------------------------------------------

DOCKERHUB_USER = "REPLACE_WITH_YOUR_DOCKERHUB_USERNAME"   # e.g. "mycompany"

# Set to True if your Docker Hub repository is private (requires login)
PRIVATE_REPO = True

# ---------------------------------------------------------------------------
# Image map: Docker Hub tag  ->  local name expected by docker-compose.yml
# ---------------------------------------------------------------------------

IMAGES = [
    (f"{DOCKERHUB_USER}/agentiq-postgres:1.0",   "agentiq-postgres:1.0"),
    (f"{DOCKERHUB_USER}/agentiq-backend:latest",  "agentiq-backend:latest"),
    (f"{DOCKERHUB_USER}/agentiq-frontend:1.0",    "agentiq-frontend:1.0"),
]

STORE_DIR    = pathlib.Path("/opt/aiqstore")
ENV_FILE     = STORE_DIR / "backend" / ".env"
SCRIPT_DIR   = pathlib.Path(__file__).resolve().parent
COMPOSE_FILE = SCRIPT_DIR.parent / "docker-compose.yml"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def banner(msg: str) -> None:
    print(f"\n{'=' * 60}\n  {msg}\n{'=' * 60}")


def step(label: str, msg: str) -> None:
    print(f"\n==> [{label}] {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def ok(msg: str) -> None:
    print(f"    [OK] {msg}")


def fail(msg: str) -> None:
    print(f"\n[ERROR] {msg}")


def run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check)

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def check_config() -> None:
    if DOCKERHUB_USER == "REPLACE_WITH_YOUR_DOCKERHUB_USERNAME":
        fail("Set DOCKERHUB_USER at the top of this script before distributing.")
        sys.exit(1)


def setup_dirs() -> None:
    for sub in ("backend", "postgres", "logs", "ssl"):
        (STORE_DIR / sub).mkdir(parents=True, exist_ok=True)
    info(f"Storage root : {STORE_DIR}")


def check_docker() -> None:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        fail("Docker is not running. Start Docker Desktop and retry.")
        sys.exit(1)
    ok("Docker is running.")


def check_compose_file() -> None:
    if not COMPOSE_FILE.exists():
        fail(f"docker-compose.yml not found at {COMPOSE_FILE}")
        sys.exit(1)
    ok(f"Compose file : {COMPOSE_FILE}")


def read_env_var(key: str) -> str:
    try:
        for line in ENV_FILE.read_text(errors="replace").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def check_env() -> tuple:
    if not ENV_FILE.exists():
        fail(f"{ENV_FILE} not found.")
        print(f"       Copy backend/.env.example to {ENV_FILE} and fill in values.")
        sys.exit(1)

    prod_url = read_env_var("PROD_DATABASE_URL")
    if not prod_url:
        fail(f"PROD_DATABASE_URL is empty in {ENV_FILE}")
        sys.exit(1)

    pg_user = read_env_var("POSTGRES_USER")
    pg_pass = read_env_var("POSTGRES_PASSWORD")
    pg_db   = read_env_var("POSTGRES_DB")

    if not pg_user:
        m = re.match(r"[^:]+://([^:@/]+)", prod_url)
        pg_user = m.group(1) if m else "agentiq"
    if not pg_pass:
        m = re.match(r"[^:]+://[^:]+:([^@]+)@", prod_url)
        pg_pass = m.group(1) if m else "agentiq_secret"
    if not pg_db:
        pg_db = "agentiqprod"

    ok(f"ENV file   : {ENV_FILE}")
    info(f"DB user    : {pg_user}  /  database : {pg_db}")
    return pg_user, pg_pass, pg_db, prod_url

# ---------------------------------------------------------------------------
# Docker Hub pull
# ---------------------------------------------------------------------------

def pull_images(dh_user: str, dh_password: str) -> None:
    """Login (if private), pull each image, re-tag to local name, logout."""
    logged_in = False
    try:
        if PRIVATE_REPO:
            info("Logging in to Docker Hub...")
            login = subprocess.run(
                ["docker", "login", "--username", dh_user, "--password-stdin"],
                input=dh_password, text=True, capture_output=True,
            )
            if login.returncode != 0:
                fail(f"Docker Hub login failed:\n{login.stderr}")
                sys.exit(1)
            logged_in = True
            ok("Logged in to Docker Hub.")

        for remote_img, local_name in IMAGES:
            print(f"\n  Pulling  {remote_img}")
            run(["docker", "pull", remote_img])
            run(["docker", "tag",  remote_img, local_name])
            ok(f"Tagged   {remote_img}  ->  {local_name}")

    finally:
        if logged_in:
            subprocess.run(["docker", "logout"], capture_output=True)
            ok("Logged out from Docker Hub.")

# ---------------------------------------------------------------------------
# Compose env generation
# ---------------------------------------------------------------------------

def generate_compose_env(pg_user: str, pg_pass: str,
                         pg_db: str, prod_url: str) -> pathlib.Path:
    compose_env      = STORE_DIR / ".compose.env"
    backend_safe_env = STORE_DIR / ".backend_safe.env"

    raw = ENV_FILE.read_text(errors="replace")
    safe_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            safe_lines.append(line)
            continue
        k, _, v = line.partition("=")
        v = v.replace("$$", "\x00DD\x00")
        v = v.replace("$",  "$$")
        v = v.replace("\x00DD\x00", "$$$$")
        safe_lines.append(f"{k}={v}")
    backend_safe_env.write_text("\n".join(safe_lines))
    os.chmod(backend_safe_env, 0o600)

    compose_env.write_text(
        f"PROD_DATABASE_URL={prod_url}\n"
        f"POSTGRES_USER={pg_user}\n"
        f"POSTGRES_PASSWORD={pg_pass}\n"
        f"POSTGRES_DB={pg_db}\n"
        f"BACKEND_ENV_FILE={backend_safe_env}\n"
    )
    os.chmod(compose_env, 0o600)
    return compose_env

# ---------------------------------------------------------------------------
# Service startup
# ---------------------------------------------------------------------------

def compose_up(service: str, compose_env: pathlib.Path) -> None:
    run([
        "docker", "compose",
        "--file",     str(COMPOSE_FILE),
        "--env-file", str(compose_env),
        "up", "--detach", "--no-build", service,
    ])


def wait_healthy(container: str, timeout_secs: int,
                 fail_on_unhealthy: bool = True) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "inspect",
             "--format={{.State.Health.Status}}", container],
            capture_output=True, text=True,
        )
        status = r.stdout.strip() if r.returncode == 0 else "starting"

        if status == "healthy":
            return True
        if status == "unhealthy" and fail_on_unhealthy:
            print()
            fail(f"{container} is unhealthy. Last 40 log lines:")
            subprocess.run(["docker", "logs", "--tail", "40", container])
            sys.exit(1)

        print(".", end="", flush=True)
        time.sleep(3)

    print()
    return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    check_config()

    banner("AgentIQ — On-Premise Deploy (Docker Hub)")
    print(f"  Docker Hub user : {DOCKERHUB_USER}")
    print(f"  Private repo    : {PRIVATE_REPO}")

    # ── 1. Pre-flight ──────────────────────────────────────────────────────
    step("1/7", "Setting up storage directories...")
    setup_dirs()

    step("2/7", "Checking prerequisites...")
    check_docker()
    check_compose_file()
    pg_user, pg_pass, pg_db, prod_url = check_env()

    # ── 2. Credentials (only needed for private repos) ─────────────────────
    dh_user     = DOCKERHUB_USER
    dh_password = ""

    if PRIVATE_REPO:
        step("3/7", "Docker Hub credentials (not stored — logged out after pull):")
        dh_user     = input(f"  Username [{DOCKERHUB_USER}] : ").strip() or DOCKERHUB_USER
        dh_password = getpass.getpass("  Password / Access Token  : ")
        if not dh_password:
            fail("Password / access token is required for a private repository.")
            sys.exit(1)
    else:
        step("3/7", "Public repository — no credentials required.")
        ok("Skipping Docker Hub login.")

    # ── 3. Pull images ─────────────────────────────────────────────────────
    step("4/7", "Pulling images from Docker Hub...")
    try:
        pull_images(dh_user, dh_password)
    finally:
        # Clear password from memory
        try:
            import ctypes
            enc = dh_password.encode()
            buf = (ctypes.c_char * len(enc)).from_address(id(enc) + 24)
            ctypes.memset(buf, 0, len(enc))
        except Exception:
            pass
        del dh_password
    info("Docker Hub credentials cleared from memory.")

    # ── 4. Compose env ─────────────────────────────────────────────────────
    step("5/7", "Generating compose environment...")
    compose_env = generate_compose_env(pg_user, pg_pass, pg_db, prod_url)
    ok(f"Compose env : {compose_env}")

    # ── 5. PostgreSQL ──────────────────────────────────────────────────────
    step("6/7", "[1/3] Starting PostgreSQL...")
    compose_up("postgres", compose_env)
    info("Waiting for PostgreSQL to be healthy...")
    if not wait_healthy("agentiq-postgres", timeout_secs=90):
        fail("PostgreSQL did not become healthy in time.")
        subprocess.run(["docker", "logs", "--tail", "30", "agentiq-postgres"])
        sys.exit(1)
    ok("PostgreSQL is healthy.")

    # ── 6. Backend ─────────────────────────────────────────────────────────
    step("6/7", "[2/3] Starting Backend...")
    compose_up("backend", compose_env)
    info("Waiting for Backend to be healthy (up to 2 min)...")
    if not wait_healthy("agentiq-backend", timeout_secs=120):
        fail("Backend did not become healthy in time.")
        subprocess.run(["docker", "logs", "--tail", "20", "agentiq-backend"])
        sys.exit(1)
    ok("Backend is healthy.")

    # ── 7. Frontend ────────────────────────────────────────────────────────
    step("7/7", "[3/3] Starting Frontend...")
    compose_up("frontend", compose_env)
    info("Waiting for Frontend to be healthy...")
    frontend_ok = wait_healthy("agentiq-frontend", timeout_secs=60,
                               fail_on_unhealthy=False)
    if frontend_ok:
        ok("Frontend is healthy.")
    else:
        print("\n    WARN: Frontend health check timed out (nginx may still be starting).")

    # ── 8. Summary ─────────────────────────────────────────────────────────
    try:
        server_ip = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True
        ).stdout.split()[0]
    except Exception:
        server_ip = "localhost"

    ssl_cert = STORE_DIR / "ssl" / "fullchain.pem"

    banner("AgentIQ stack is up")
    subprocess.run([
        "docker", "compose",
        "--file", str(COMPOSE_FILE),
        "ps", "--format", "table {{.Name}}\t{{.Status}}\t{{.Ports}}",
    ])
    print()
    if ssl_cert.exists():
        print(f"  Frontend  : http://{server_ip}   (HTTP)")
        print(f"  Frontend  : https://{server_ip}  (HTTPS)")
    else:
        print(f"  Frontend  : http://{server_ip}")
        print(f"  (Place certs in {STORE_DIR}/ssl/ and restart for HTTPS)")
    print(f"  API docs  : http://{server_ip}:8000/docs")
    print(f"  API base  : http://{server_ip}:8000/api")
    print()
    print(f"  Logs : docker compose --file {COMPOSE_FILE} logs -f")
    print(f"  Stop : docker compose --file {COMPOSE_FILE} down")
    print(f"  Store: {STORE_DIR}")
    print("=" * 60)

    if not frontend_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
