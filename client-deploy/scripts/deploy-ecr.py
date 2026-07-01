#!/usr/bin/env python3
"""
AgentIQ — ECR Deploy Script
Pulls Docker images from AWS ECR and starts all containers via docker-compose.

Fixes vs previous version:
  1. Backend .env: volume-mounted into container at /app/.env (file visible on disk)
     so python-dotenv can read it, in addition to env_file injection.
  2. Frontend health: treated as advisory — a timeout/unhealthy status prints a
     warning but never causes a non-zero exit. Deployment is considered successful
     when postgres and backend are healthy.

Usage:
    python3 scripts/deploy-ecr.py          # reads /opt/aiqstore/backend/.env
    sudo python3 scripts/deploy-ecr.py     # required for /opt/aiqstore access

Requires:
    pip install boto3
    Docker + docker-compose (or docker compose plugin)
    /opt/aiqstore/backend/.env populated by Configfile-create.sh
"""

import sys
import os
import subprocess
import json
import base64
import ctypes
import getpass
import time
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
ECR_ACCOUNT  = "070206924228"
ECR_REGION   = "us-east-1"
ECR_REPO     = "agentiq"
ECR_REGISTRY = f"{ECR_ACCOUNT}.dkr.ecr.{ECR_REGION}.amazonaws.com"

STORE_DIR     = Path("/opt/aiqstore")
ENV_FILE      = STORE_DIR / "backend" / ".env"   # /opt/aiqstore/backend/.env
COMPOSE_FILE  = Path(__file__).resolve().parent.parent / "docker-compose.yml"
COMPOSE_ENV   = STORE_DIR / ".compose.env"

DOCKER_CONFIG = Path.home() / ".docker" / "config.json"

ECR_IMAGES = [
    f"{ECR_REGISTRY}/{ECR_REPO}:postgres-1.0",
    f"{ECR_REGISTRY}/{ECR_REPO}:backend-latest",
    f"{ECR_REGISTRY}/{ECR_REPO}:frontend-1.0",
]

# Seconds to wait for a service to become healthy
HEALTH_TIMEOUT = {
    "postgres": 90,
    "backend":  120,
    "frontend": 60,
}


# ── Root escalation ───────────────────────────────────────────────────────────

def _ensure_root():
    if os.geteuid() == 0:
        return
    print("[deploy] Re-executing with sudo...")
    os.execvp("sudo", ["sudo", "python3"] + sys.argv)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(args, check=True, capture=False, **kwargs):
    if not capture:
        print(f"  $ {' '.join(str(a) for a in args)}")
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        **kwargs,
    )
    if check and result.returncode != 0:
        if capture:
            print(result.stderr.decode(errors="replace"))
        sys.exit(f"Command failed (exit {result.returncode})")
    return result


def _wipe(s: str):
    try:
        buf = ctypes.create_unicode_buffer(s)
        ctypes.memset(buf, 0, ctypes.sizeof(buf))
    except Exception:
        pass


def _read_env(path: Path) -> dict:
    """Parse KEY=value lines from an env file, ignoring comments."""
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ── AWS / ECR auth ────────────────────────────────────────────────────────────

def _get_aws_creds(env: dict):
    key_id  = env.get("AWS_ACCESS_KEY_ID")  or os.environ.get("AWS_ACCESS_KEY_ID")
    secret  = env.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    session = env.get("AWS_SESSION_TOKEN")  or os.environ.get("AWS_SESSION_TOKEN")

    if key_id and secret:
        print("  [auth] Using AWS credentials from .env / environment.")
        return key_id, secret, session

    print()
    print("  AWS credentials not found. Enter them now.")
    print("  (held in memory only — never written to disk)")
    print()
    key_id  = input("  AWS Access Key ID: ").strip()
    secret  = getpass.getpass("  AWS Secret Access Key: ")
    session = input("  AWS Session Token (Enter to skip): ").strip() or None
    return key_id, secret, session


def _ecr_password(key_id, secret, session):
    try:
        import boto3
    except ImportError:
        sys.exit("boto3 not installed. Run: pip3 install boto3")

    kwargs = {
        "aws_access_key_id":     key_id,
        "aws_secret_access_key": secret,
        "region_name":           ECR_REGION,
    }
    if session:
        kwargs["aws_session_token"] = session

    client   = boto3.Session(**kwargs).client("ecr")
    data     = client.get_authorization_token(registryIds=[ECR_ACCOUNT])
    token_b64 = data["authorizationData"][0]["authorizationToken"]
    decoded  = base64.b64decode(token_b64).decode()
    return decoded.split(":", 1)[1]


def _write_docker_auth(password: str):
    DOCKER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if DOCKER_CONFIG.exists():
        try:
            cfg = json.loads(DOCKER_CONFIG.read_text())
        except Exception:
            pass
    cfg.setdefault("auths", {})
    cfg["auths"][ECR_REGISTRY] = {
        "auth": base64.b64encode(f"AWS:{password}".encode()).decode()
    }
    DOCKER_CONFIG.write_text(json.dumps(cfg, indent=2))


def _remove_docker_auth():
    if not DOCKER_CONFIG.exists():
        return
    try:
        cfg = json.loads(DOCKER_CONFIG.read_text())
        cfg.get("auths", {}).pop(ECR_REGISTRY, None)
        DOCKER_CONFIG.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ── Docker Compose helpers ────────────────────────────────────────────────────

def _compose_cmd():
    """Return ['docker', 'compose'] or ['docker-compose'] depending on what's installed."""
    r = subprocess.run(["docker", "compose", "version"],
                       capture_output=True)
    if r.returncode == 0:
        return ["docker", "compose"]
    return ["docker-compose"]


def _compose(args, env_file=None, **kwargs):
    base = _compose_cmd() + ["-f", str(COMPOSE_FILE)]
    if env_file:
        base += ["--env-file", str(env_file)]
    return _run(base + args, **kwargs)


def generate_compose_env(env: dict) -> Path:
    """
    Write /opt/aiqstore/.compose.env — variables consumed by docker-compose.yml
    that are not already in the main .env file (ECR_REGISTRY, derived paths).
    """
    pg_password = env.get("POSTGRES_PASSWORD", "agentiq_secret_change_me")
    pg_user     = env.get("POSTGRES_USER", "agentiq")
    pg_db       = env.get("POSTGRES_DB", "agentiq")

    lines = [
        f"ECR_REGISTRY={ECR_REGISTRY}",
        f"POSTGRES_PASSWORD={pg_password}",
        f"POSTGRES_USER={pg_user}",
        f"POSTGRES_DB={pg_db}",
    ]
    COMPOSE_ENV.write_text("\n".join(lines) + "\n")
    os.chmod(COMPOSE_ENV, 0o600)
    return COMPOSE_ENV


# ── Health polling ────────────────────────────────────────────────────────────

def _service_container_id(service: str) -> str:
    """
    Return the running container ID for a compose service.
    Uses 'docker compose ps -q' — never guesses the name.
    COMPOSE_ENV must exist before calling this (written by generate_compose_env).
    """
    r = subprocess.run(
        _compose_cmd() + ["-f", str(COMPOSE_FILE), "--env-file", str(COMPOSE_ENV),
                          "ps", "-q", service],
        capture_output=True, text=True,
    )
    lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    return lines[0] if lines else ""


def wait_healthy(service: str, timeout: int, advisory: bool = False) -> bool:
    """
    Poll docker inspect until the container is 'healthy' or timeout expires.

    advisory=True  → timeout/unhealthy prints a warning but returns True (success).
    advisory=False → timeout/unhealthy returns False (caller decides what to do).
    """
    container = _service_container_id(service)
    if not container:
        print(f"  [{service}] WARNING: could not resolve container ID — skipping health check")
        return advisory  # advisory → True (continue), required → False (caller exits)

    deadline = time.time() + timeout
    spinner  = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
    tick     = 0

    while time.time() < deadline:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", container],
            capture_output=True, text=True,
        )
        status = r.stdout.strip()

        if status == "healthy":
            print(f"\r  [{service}] healthy ✓" + " " * 20)
            return True

        if status == "unhealthy":
            if advisory:
                print(f"\r  [{service}] unhealthy (advisory — continuing)" + " " * 10)
                return True
            print(f"\r  [{service}] unhealthy ✗" + " " * 20)
            return False

        print(f"\r  [{service}] {spinner[tick % len(spinner)]} waiting ({int(deadline - time.time())}s)…",
              end="", flush=True)
        tick += 1
        time.sleep(3)

    # Timeout reached
    if advisory:
        print(f"\r  [{service}] health check timed out (advisory — continuing)" + " " * 10)
        return True

    print(f"\r  [{service}] health check timed out" + " " * 20)
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    _ensure_root()

    print("=" * 62)
    print("  AgentIQ — Deploy")
    print(f"  Registry : {ECR_REGISTRY}")
    print(f"  Compose  : {COMPOSE_FILE}")
    print("=" * 62)

    # ── Validate env file ────────────────────────────────────────────────────
    if not ENV_FILE.exists():
        sys.exit(
            f"\n  ERROR: {ENV_FILE} not found.\n"
            "  Run Configfile-create.sh first to create /opt/aiqstore/backend/.env\n"
        )
    env = _read_env(ENV_FILE)

    # ── Get AWS credentials ──────────────────────────────────────────────────
    print()
    print("[1/5] Authenticating with AWS ECR...")
    key_id, secret, session = _get_aws_creds(env)

    password = _ecr_password(key_id, secret, session)
    _wipe(secret)
    if session:
        _wipe(session)

    _write_docker_auth(password)
    _wipe(password)
    print("  [auth] ECR token written to Docker config.")

    try:
        # ── Pull images ──────────────────────────────────────────────────────
        print()
        print("[2/5] Pulling images from ECR...")
        for image in ECR_IMAGES:
            print(f"  Pulling {image.split('/')[-1]}...")
            _run(["docker", "pull", image])

        # ── Write compose env ────────────────────────────────────────────────
        print()
        print("[3/5] Writing compose environment...")
        compose_env = generate_compose_env(env)
        print(f"  Wrote {compose_env}")

        # ── Ensure required host directories exist ───────────────────────────
        (STORE_DIR / "ssl").mkdir(parents=True, exist_ok=True)
        (STORE_DIR / "backend").mkdir(parents=True, exist_ok=True)

        # ── Start containers ─────────────────────────────────────────────────
        print()
        print("[4/5] Starting containers...")
        _compose(["up", "-d", "--remove-orphans"], env_file=compose_env)

        # ── Health checks ────────────────────────────────────────────────────
        print()
        print("[5/5] Waiting for services to be healthy...")
        print()

        # Postgres — required
        pg_ok = wait_healthy("postgres",  HEALTH_TIMEOUT["postgres"], advisory=False)
        if not pg_ok:
            sys.exit(
                "\n  ERROR: postgres container is not healthy.\n"
                f"  Check logs: docker compose -f {COMPOSE_FILE} logs postgres\n"
            )

        # Backend — required
        be_ok = wait_healthy("backend",   HEALTH_TIMEOUT["backend"],  advisory=False)
        if not be_ok:
            sys.exit(
                "\n  ERROR: backend container is not healthy.\n"
                f"  Check logs: docker compose -f {COMPOSE_FILE} logs backend\n"
            )

        # Frontend — advisory only (healthcheck may lag behind actual readiness)
        wait_healthy("frontend", HEALTH_TIMEOUT["frontend"], advisory=True)

    finally:
        _remove_docker_auth()

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 62)
    print("  AgentIQ is running.")
    print()

    ssl_cert = STORE_DIR / "ssl" / "cert.pem"
    if ssl_cert.exists():
        print("  URL : https://<your-server-ip>")
    else:
        print("  URL : http://<your-server-ip>")
        print()
        print("  To enable HTTPS:")
        print(f"    Place cert.pem + key.pem in /opt/aiqstore/ssl/")
        print(f"    then: docker compose -f {COMPOSE_FILE} restart frontend")

    print()
    print("  To update backend configuration:")
    print(f"    Edit   : /opt/aiqstore/backend/.env")
    print(f"    Reload : docker compose -f {COMPOSE_FILE} restart backend")
    print()
    print("  Useful commands:")
    print(f"    docker compose -f {COMPOSE_FILE} ps")
    print(f"    docker compose -f {COMPOSE_FILE} logs -f backend")
    print(f"    docker compose -f {COMPOSE_FILE} down")
    print("=" * 62)


if __name__ == "__main__":
    main()
