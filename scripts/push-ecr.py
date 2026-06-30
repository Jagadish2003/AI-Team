#!/usr/bin/env python3
"""
AgentIQ — ECR Push Script
Builds all three Docker images locally and pushes them to AWS ECR.

Usage:
    python3 scripts/push-ecr.py

Requires:
    pip install boto3
    Docker daemon running
    AWS credentials (env vars or ~/.aws/credentials)

Images built and pushed:
    agentiq-postgres:1.0   → <account>.dkr.ecr.<region>.amazonaws.com/agentiq:postgres-1.0
    agentiq-backend:latest → <account>.dkr.ecr.<region>.amazonaws.com/agentiq:backend-latest
    agentiq-frontend:1.0   → <account>.dkr.ecr.<region>.amazonaws.com/agentiq:frontend-1.0
"""

import sys
import os
import subprocess
import json
import base64
import ctypes
import getpass
from pathlib import Path

# ── ECR Configuration ─────────────────────────────────────────────────────────
ECR_ACCOUNT  = "070206924228"
ECR_REGION   = "us-east-1"
ECR_REPO     = "agentiq"
ECR_REGISTRY = f"{ECR_ACCOUNT}.dkr.ecr.{ECR_REGION}.amazonaws.com"

REPO_ROOT = Path(__file__).resolve().parent.parent

IMAGES = [
    {
        "label":     "postgres",
        "context":   REPO_ROOT / "docker" / "postgres",
        "local_tag": "agentiq-postgres:1.0",
        "ecr_tag":   "postgres-1.0",
    },
    {
        "label":     "backend",
        "context":   REPO_ROOT / "backend",
        "local_tag": "agentiq-backend:latest",
        "ecr_tag":   "backend-latest",
    },
    {
        "label":     "frontend",
        "context":   REPO_ROOT / "frontend",
        "local_tag": "agentiq-frontend:1.0",
        "ecr_tag":   "frontend-1.0",
        "build_args": {"VITE_API_BASE_URL": ""},
    },
]

DOCKER_CONFIG = Path.home() / ".docker" / "config.json"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _run(args, **kwargs):
    """Run a subprocess command, streaming output."""
    print(f"  $ {' '.join(str(a) for a in args)}")
    result = subprocess.run(args, **kwargs)
    if result.returncode != 0:
        sys.exit(f"Command failed with exit code {result.returncode}")


def _wipe_string(s: str):
    """Best-effort in-memory wipe of a credential string."""
    try:
        buf = ctypes.create_unicode_buffer(s)
        ctypes.memset(buf, 0, ctypes.sizeof(buf))
    except Exception:
        pass


def _get_aws_credentials():
    """Read AWS creds from environment, then prompt if missing."""
    key_id     = os.environ.get("AWS_ACCESS_KEY_ID")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    session    = os.environ.get("AWS_SESSION_TOKEN")

    if key_id and secret_key:
        print("  [auth] Using AWS credentials from environment variables.")
        return key_id, secret_key, session

    print()
    print("  AWS credentials not found in environment.")
    print("  Enter credentials (they are held in memory only, never written to disk).")
    print()
    key_id     = input("  AWS Access Key ID: ").strip()
    secret_key = getpass.getpass("  AWS Secret Access Key: ")
    session    = input("  AWS Session Token (press Enter to skip): ").strip() or None
    return key_id, secret_key, session


def _get_ecr_token(key_id, secret_key, session):
    """Fetch an ECR authorization token via boto3."""
    try:
        import boto3
    except ImportError:
        sys.exit("boto3 not installed. Run: pip install boto3")

    session_kwargs = {
        "aws_access_key_id":     key_id,
        "aws_secret_access_key": secret_key,
        "region_name":           ECR_REGION,
    }
    if session:
        session_kwargs["aws_session_token"] = session

    boto_session = boto3.Session(**session_kwargs)
    ecr_client   = boto_session.client("ecr")

    response = ecr_client.get_authorization_token(registryIds=[ECR_ACCOUNT])
    auth_data = response["authorizationData"][0]
    token_b64 = auth_data["authorizationToken"]
    decoded   = base64.b64decode(token_b64).decode()
    _, password = decoded.split(":", 1)
    return password


def _write_docker_auth(ecr_password: str):
    """Write ECR credentials into ~/.docker/config.json for this push session."""
    DOCKER_CONFIG.parent.mkdir(parents=True, exist_ok=True)

    existing = {}
    if DOCKER_CONFIG.exists():
        try:
            existing = json.loads(DOCKER_CONFIG.read_text())
        except Exception:
            pass

    existing.setdefault("auths", {})
    auth_token = base64.b64encode(f"AWS:{ecr_password}".encode()).decode()
    existing["auths"][ECR_REGISTRY] = {"auth": auth_token}

    DOCKER_CONFIG.write_text(json.dumps(existing, indent=2))


def _remove_docker_auth():
    """Remove the ECR entry from ~/.docker/config.json after push."""
    if not DOCKER_CONFIG.exists():
        return
    try:
        cfg = json.loads(DOCKER_CONFIG.read_text())
        cfg.get("auths", {}).pop(ECR_REGISTRY, None)
        DOCKER_CONFIG.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 62)
    print("  AgentIQ — ECR Push")
    print(f"  Registry : {ECR_REGISTRY}")
    print(f"  Repo     : {ECR_REPO}")
    print("=" * 62)

    key_id, secret_key, session = _get_aws_credentials()

    print()
    print("[1/4] Fetching ECR authorization token...")
    ecr_password = _get_ecr_token(key_id, secret_key, session)

    # Wipe plaintext credentials from memory as soon as we have the ECR token
    _wipe_string(secret_key)
    if session:
        _wipe_string(session)

    print("[2/4] Writing Docker auth (temp)...")
    _write_docker_auth(ecr_password)
    _wipe_string(ecr_password)

    errors = []

    try:
        for img in IMAGES:
            label     = img["label"]
            context   = img["context"]
            local_tag = img["local_tag"]
            ecr_full  = f"{ECR_REGISTRY}/{ECR_REPO}:{img['ecr_tag']}"

            print()
            print(f"[3/4] Building {label} → {local_tag}")
            build_cmd = ["docker", "build", "-t", local_tag, str(context)]
            for k, v in img.get("build_args", {}).items():
                build_cmd += ["--build-arg", f"{k}={v}"]
            _run(build_cmd)

            print(f"      Tagging  {local_tag} → {ecr_full}")
            _run(["docker", "tag", local_tag, ecr_full])

            print(f"[4/4] Pushing  {ecr_full}")
            _run(["docker", "push", ecr_full])

            print(f"      Removing local ECR alias {ecr_full}")
            _run(["docker", "rmi", ecr_full])

    except SystemExit as exc:
        errors.append(str(exc))

    finally:
        print()
        print("Removing Docker auth entry...")
        _remove_docker_auth()

    if errors:
        sys.exit(f"Push failed: {errors[0]}")

    print()
    print("=" * 62)
    print("  All images pushed successfully.")
    print()
    for img in IMAGES:
        print(f"  {ECR_REGISTRY}/{ECR_REPO}:{img['ecr_tag']}")
    print("=" * 62)


if __name__ == "__main__":
    main()
