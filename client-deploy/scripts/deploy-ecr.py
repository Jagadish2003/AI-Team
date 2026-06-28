#!/usr/bin/env python3
"""
deploy-ecr.py — Single entry point for AgentIQ on-premise deployment.

What it does (in order):
  1. Runs Configfile-create.sh  — collects FQDN/SMTP/keys, writes /opt/aiqstore/.env
  2. Pulls images from AWS ECR  — no AWS CLI required; credentials cleared after pull
  3. Starts PostgreSQL, Backend, Frontend sequentially with health-check polling
  4. Prints the application URL

Prerequisites (cannot be auto-installed):
  - Python 3.8+
  - Docker running
  - bash (standard on Linux/macOS)
"""

import os
import subprocess
import sys

# ---------------------------------------------------------------------------
# Root check — must run before anything touches /opt/
# Re-execs the script under sudo automatically if the user is not root.
# ---------------------------------------------------------------------------

def _ensure_root() -> None:
    if os.getuid() != 0:
        print("[setup] Root privileges required to write to /opt/aiqstore/.")
        print("[setup] Re-running with sudo — you may be prompted for your password.")
        os.execvp("sudo", ["sudo", sys.executable] + sys.argv)
        # execvp replaces this process; the line below is never reached.

_ensure_root()

# ---------------------------------------------------------------------------
# Self-install dependencies BEFORE any other imports
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = ["boto3"]


def _pip_install(packages: list) -> None:
    print(f"[setup] Installing missing packages: {', '.join(packages)} ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--disable-pip-version-check", "--no-warn-script-location", *packages],
    )
    if result.returncode != 0:
        print(f"\n[ERROR] pip install failed for: {packages}")
        print("Ensure you have internet access and pip is available.")
        sys.exit(1)
    print(f"[setup] Installed: {', '.join(packages)}")


def _ensure_packages() -> None:
    missing = [p for p in REQUIRED_PACKAGES if not _importable(p)]
    if missing:
        _pip_install(missing)


def _importable(pkg: str) -> bool:
    try:
        __import__(pkg)
        return True
    except ImportError:
        return False


_ensure_packages()

# ---------------------------------------------------------------------------
# Safe to import third-party packages now
# ---------------------------------------------------------------------------

import base64      # noqa: E402
import ctypes      # noqa: E402
import getpass     # noqa: E402
import json        # noqa: E402
import pathlib     # noqa: E402
import re          # noqa: E402
import time        # noqa: E402

import boto3                                          # noqa: E402
from botocore.exceptions import ClientError, NoCredentialsError  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AWS_ACCOUNT_ID = "070206924228"
AWS_REGION     = "us-east-1"
ECR_REPO       = "agentiq"
ECR_REGISTRY   = f"{AWS_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com"

# (ecr_tag, local_image_name) — local names must match docker-compose.yml
IMAGES = [
    ("postgres-1.0",   "agentiq-postgres:1.0"),
    ("backend-latest", "agentiq-backend:latest"),
    ("frontend-1.0",   "agentiq-frontend:1.0"),
]

STORE_DIR    = pathlib.Path("/opt/aiqstore")
ENV_FILE     = STORE_DIR / ".env"                    # written by Configfile-create.sh
SCRIPT_DIR   = pathlib.Path(__file__).resolve().parent
COMPOSE_FILE = SCRIPT_DIR.parent / "docker-compose.yml"
CONFIG_SCRIPT = SCRIPT_DIR.parent / "Configfile-create.sh"

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def banner(msg: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {msg}")
    print(f"{'=' * 60}")


def step(label: str, msg: str) -> None:
    print(f"\n==> [{label}] {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def ok(msg: str) -> None:
    print(f"    [OK] {msg}")


def fail(msg: str) -> None:
    print(f"\n[ERROR] {msg}")


def run(cmd: list, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check,
                          capture_output=capture, text=capture)

# ---------------------------------------------------------------------------
# Step 1: configuration (.env creation)
# ---------------------------------------------------------------------------

def run_config() -> None:
    """Invoke Configfile-create.sh to collect inputs and write /opt/aiqstore/.env."""
    if not CONFIG_SCRIPT.exists():
        fail(f"Configfile-create.sh not found at {CONFIG_SCRIPT}")
        sys.exit(1)
    result = subprocess.run(["bash", str(CONFIG_SCRIPT)])
    if result.returncode != 0:
        fail("Configuration step failed. Fix the errors above and re-run.")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def setup_dirs() -> None:
    # Create the root store directory and set ownership to root, readable by all.
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE_DIR, 0o755)

    # postgres/ — writable; standard postgres image entrypoint chowns it to
    # UID 999 (postgres) internally before initialising the data directory.
    (STORE_DIR / "postgres").mkdir(parents=True, exist_ok=True)
    os.chmod(STORE_DIR / "postgres", 0o755)

    # logs/ — world-readable so container processes can write logs.
    (STORE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    os.chmod(STORE_DIR / "logs", 0o755)

    # ssl/ — 700: only root may read TLS private keys placed here.
    (STORE_DIR / "ssl").mkdir(parents=True, exist_ok=True)
    os.chmod(STORE_DIR / "ssl", 0o700)

    info(f"Storage root : {STORE_DIR}")
    info("  postgres/ 755  |  logs/ 755  |  ssl/ 700")


def read_env_var(key: str) -> str:
    """Read a single value from ENV_FILE without sourcing the file."""
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
    """Validate .env and return (pg_user, pg_pass, pg_db, prod_url, public_url)."""
    if not ENV_FILE.exists():
        fail(f"{ENV_FILE} not found — configuration step should have created it.")
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

    # Read the public URL set by Configfile-create.sh
    public_url = (
        read_env_var("PUBLIC_HOSTNAME")
        or read_env_var("OAUTH_FRONTEND_BASE_URL")
        or ""
    )

    ok(f"ENV file   : {ENV_FILE}")
    info(f"DB user    : {pg_user}  /  database : {pg_db}")
    if public_url:
        info(f"Public URL : {public_url}")
    return pg_user, pg_pass, pg_db, prod_url, public_url


def check_docker() -> None:
    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        fail("Docker is not running or not installed. Start Docker and retry.")
        sys.exit(1)
    ok("Docker is running.")


def check_compose_file() -> None:
    if not COMPOSE_FILE.exists():
        fail(f"docker-compose.yml not found at {COMPOSE_FILE}")
        sys.exit(1)
    ok(f"Compose file : {COMPOSE_FILE}")

# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _zero_string(s: str) -> None:
    try:
        enc = s.encode("utf-8")
        buf = (ctypes.c_char * len(enc)).from_address(id(enc) + 24)
        ctypes.memset(buf, 0, len(enc))
    except Exception:
        pass


def _write_docker_config(auth_data: str) -> None:
    cfg_path = pathlib.Path.home() / ".docker" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
        except json.JSONDecodeError:
            cfg = {}
    cfg.setdefault("auths", {})[ECR_REGISTRY] = {"auth": auth_data}
    cfg_path.write_text(json.dumps(cfg, indent=2))


def _clear_docker_config() -> None:
    cfg_path = pathlib.Path.home() / ".docker" / "config.json"
    try:
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text())
            cfg.get("auths", {}).pop(ECR_REGISTRY, None)
            cfg_path.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass

# ---------------------------------------------------------------------------
# ECR pull
# ---------------------------------------------------------------------------

def pull_images(access_key: str, secret_key: str) -> None:
    docker_password = None
    try:
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=AWS_REGION,
        )
        ecr = session.client("ecr")

        token_resp = ecr.get_authorization_token()
        auth_data  = token_resp["authorizationData"][0]["authorizationToken"]
        decoded    = base64.b64decode(auth_data).decode("utf-8")
        _, docker_password = decoded.split(":", 1)

        _write_docker_config(auth_data)
        ok("ECR credentials written to Docker config.")

        for ecr_tag, local_name in IMAGES:
            remote = f"{ECR_REGISTRY}/{ECR_REPO}:{ecr_tag}"
            print(f"\n  Pulling  {remote}")
            run(["docker", "pull", remote])
            run(["docker", "tag",  remote, local_name])
            ok(f"Tagged   {remote}  ->  {local_name}")

    except (ClientError, NoCredentialsError) as e:
        fail(f"AWS error: {e}")
        sys.exit(1)
    finally:
        _clear_docker_config()
        if docker_password:
            _zero_string(docker_password)
        del docker_password

# ---------------------------------------------------------------------------
# Compose env generation
# ---------------------------------------------------------------------------

def generate_compose_env(pg_user: str, pg_pass: str, pg_db: str,
                         prod_url: str) -> pathlib.Path:
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
        v = v.replace("$$", "\x00DOLDOL\x00")
        v = v.replace("$", "$$")
        v = v.replace("\x00DOLDOL\x00", "$$$$")
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
# Service startup with health-check polling
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
            ["docker", "inspect", "--format={{.State.Health.Status}}", container],
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
    banner("AgentIQ — On-Premise Deployment")
    print(f"  Registry : {ECR_REGISTRY}")
    print(f"  Repo     : {ECR_REPO}")
    print(f"  Region   : {AWS_REGION}")

    # ── Step 1: configuration ──────────────────────────────────────────────
    step("1/7", "Application configuration — collecting URL and integration settings...")
    run_config()

    # ── Step 2: pre-flight ─────────────────────────────────────────────────
    step("2/7", "Checking prerequisites...")
    setup_dirs()
    check_docker()
    check_compose_file()
    pg_user, pg_pass, pg_db, prod_url, public_url = check_env()

    # ── Step 3: AWS credentials ────────────────────────────────────────────
    step("3/7", "Enter AWS credentials (not stored — cleared after pull):")
    access_key = input("  AWS Access Key ID     : ").strip()
    secret_key = getpass.getpass("  AWS Secret Access Key : ")
    if not access_key or not secret_key:
        fail("Both Access Key ID and Secret Access Key are required.")
        sys.exit(1)

    # ── Step 4: pull images ────────────────────────────────────────────────
    step("4/7", "Pulling images from ECR...")
    pull_images(access_key, secret_key)
    _zero_string(access_key)
    _zero_string(secret_key)
    del access_key, secret_key
    info("AWS credentials cleared from memory.")

    # ── Step 5: compose env ────────────────────────────────────────────────
    step("5/7", "Generating compose environment...")
    compose_env = generate_compose_env(pg_user, pg_pass, pg_db, prod_url)
    ok(f"Compose env : {compose_env}")

    # ── Step 6: start services ─────────────────────────────────────────────
    step("6/7", "[1/3] Starting PostgreSQL...")
    compose_up("postgres", compose_env)
    info("Waiting for PostgreSQL to be healthy...")
    if not wait_healthy("agentiq-postgres", timeout_secs=90):
        fail("PostgreSQL did not become healthy in time.")
        subprocess.run(["docker", "logs", "--tail", "30", "agentiq-postgres"])
        sys.exit(1)
    ok("PostgreSQL is healthy.")

    step("6/7", "[2/3] Starting Backend...")
    compose_up("backend", compose_env)
    info("Waiting for Backend to be healthy (up to 2 min)...")
    if not wait_healthy("agentiq-backend", timeout_secs=120):
        fail("Backend did not become healthy in time.")
        subprocess.run(["docker", "logs", "--tail", "20", "agentiq-backend"])
        sys.exit(1)
    ok("Backend is healthy.")

    step("7/7", "[3/3] Starting Frontend...")
    compose_up("frontend", compose_env)
    info("Waiting for Frontend to be healthy...")
    frontend_ok = wait_healthy("agentiq-frontend", timeout_secs=60,
                               fail_on_unhealthy=False)
    if frontend_ok:
        ok("Frontend is healthy.")
    else:
        print("\n    WARN: Frontend health check timed out — nginx may still be starting.")

    # ── Summary ────────────────────────────────────────────────────────────
    try:
        server_ip = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True
        ).stdout.split()[0]
    except Exception:
        server_ip = "localhost"

    ssl_cert = STORE_DIR / "ssl" / "fullchain.pem"

    banner("AgentIQ is up and running")
    subprocess.run([
        "docker", "compose",
        "--file", str(COMPOSE_FILE),
        "ps", "--format", "table {{.Name}}\t{{.Status}}\t{{.Ports}}",
    ])
    print()

    # Prefer the configured FQDN over the bare IP
    if public_url:
        print(f"  Application : {public_url}")
        if ssl_cert.exists():
            https_url = public_url.replace("http://", "https://")
            if https_url != public_url:
                print(f"              {https_url}  (HTTPS)")
    elif ssl_cert.exists():
        print(f"  Application : http://{server_ip}   (HTTP)")
        print(f"                https://{server_ip}  (HTTPS)")
    else:
        print(f"  Application : http://{server_ip}")
        print(f"  (Place certs in {STORE_DIR}/ssl/ and restart to enable HTTPS)")

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
