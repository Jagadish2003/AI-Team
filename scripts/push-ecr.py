#!/usr/bin/env python3
"""
push-ecr.py — Push AgentIQ Docker images to AWS ECR.

Self-installs all Python dependencies (boto3) automatically.
Does NOT require AWS CLI. Credentials are entered interactively,
held only in-memory, and explicitly cleared after the push completes.

Only prerequisites that cannot be auto-installed:
    - Python 3.8+ (already present if you can run this script)
    - Docker Desktop running locally
    - Local images built:
        agentiq-postgres:1.0
        agentiq-backend:latest
        agentiq-frontend:1.0
"""

import subprocess
import sys

# ---------------------------------------------------------------------------
# Self-install dependencies BEFORE any other imports
# ---------------------------------------------------------------------------

REQUIRED_PACKAGES = ["boto3"]


def _pip_install(packages: list) -> None:
    print(f"[setup] Installing missing packages: {', '.join(packages)} ...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
         "--no-warn-script-location", *packages],
        capture_output=False,
    )
    if result.returncode != 0:
        print(f"\n[ERROR] pip install failed for: {packages}")
        print("Make sure you have an internet connection and pip is available.")
        sys.exit(1)
    print(f"[setup] Packages installed: {', '.join(packages)}")


def _ensure_packages() -> None:
    missing = []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        _pip_install(missing)


_ensure_packages()

# ---------------------------------------------------------------------------
# Safe to import third-party packages now
# ---------------------------------------------------------------------------

import base64  # noqa: E402
import ctypes  # noqa: E402
import getpass  # noqa: E402
import json
import os
import pathlib

import boto3  # noqa: E402
from botocore.exceptions import ClientError, NoCredentialsError  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

AWS_ACCOUNT_ID = "070206924228"
AWS_REGION = "us-east-1"
ECR_REPO = "agentiq"
ECR_REGISTRY = f"{AWS_ACCOUNT_ID}.dkr.ecr.{AWS_REGION}.amazonaws.com"

IMAGES = [
    ("agentiq-postgres:1.0",   "postgres-1.0"),
    ("agentiq-backend:latest", "backend-latest"),
    ("agentiq-frontend:1.0",   "frontend-1.0"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def banner(msg: str) -> None:
    print(f"\n{'='*55}")
    print(f"  {msg}")
    print(f"{'='*55}")


def step(msg: str) -> None:
    print(f"\n[>>] {msg}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, check=check)
    return result


def _zero_string(s: str) -> None:
    """Best-effort overwrite of a Python str's internal buffer with zeros."""
    try:
        encoded = s.encode("utf-8")
        buf = (ctypes.c_char * len(encoded)).from_address(id(encoded) + 24)
        ctypes.memset(buf, 0, len(encoded))
    except Exception:
        pass


def ensure_docker() -> None:
    result = subprocess.run(["docker", "info"], capture_output=True)
    if result.returncode != 0:
        print("\n[ERROR] Docker is not running or not installed.")
        print("Please start Docker Desktop and retry.\n")
        sys.exit(1)
    ok("Docker is running.")


def ensure_local_images() -> None:
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, check=True,
    )
    available = set(result.stdout.strip().splitlines())
    missing = [img for img, _ in IMAGES if img not in available]
    if missing:
        print("\n[ERROR] The following local images are missing — build them first:")
        for m in missing:
            print(f"  {m}")
        sys.exit(1)
    ok("All local images found.")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    banner("AgentIQ — AWS ECR Push")
    print(f"  Registry : {ECR_REGISTRY}")
    print(f"  Repo     : {ECR_REPO}")
    print(f"  Region   : {AWS_REGION}")

    # Pre-flight checks
    step("Checking prerequisites...")
    ensure_docker()
    ensure_local_images()

    # Prompt for credentials (never written to disk or env)
    step("Enter AWS credentials (not stored anywhere — cleared after push):")
    access_key = input("  AWS Access Key ID     : ").strip()
    secret_key  = getpass.getpass("  AWS Secret Access Key : ")

    if not access_key or not secret_key:
        print("\n[ERROR] Both Access Key ID and Secret Access Key are required.")
        sys.exit(1)

    ecr_client = None
    docker_password = None

    try:
        # Create in-memory boto3 session — credentials never touch disk
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=AWS_REGION,
        )
        ecr_client = session.client("ecr")

        # Ensure the ECR repository exists
        step(f"Ensuring ECR repository '{ECR_REPO}' exists...")
        try:
            ecr_client.describe_repositories(repositoryNames=[ECR_REPO])
            ok("Repository already exists.")
        except ClientError as e:
            if e.response["Error"]["Code"] == "RepositoryNotFoundException":
                ecr_client.create_repository(repositoryName=ECR_REPO)
                ok("Repository created.")
            else:
                raise

        # Get short-lived ECR authorization token (valid 12 hours)
        step("Fetching ECR authorization token...")
        token_response = ecr_client.get_authorization_token()
        # auth_data is already base64(AWS:token) — exactly what Docker config expects
        auth_data = token_response["authorizationData"][0]["authorizationToken"]
        decoded = base64.b64decode(auth_data).decode("utf-8")
        _, docker_password = decoded.split(":", 1)

        # Write credentials directly to ~/.docker/config.json — bypasses docker login
        # network verification which fails in Docker-in-container setups.
        step(f"Writing ECR credentials to Docker config (no network call)...")
        docker_config_path = pathlib.Path.home() / ".docker" / "config.json"
        docker_config_path.parent.mkdir(parents=True, exist_ok=True)

        config = {}
        if docker_config_path.exists():
            try:
                config = json.loads(docker_config_path.read_text())
            except json.JSONDecodeError:
                config = {}

        config.setdefault("auths", {})[ECR_REGISTRY] = {"auth": auth_data}
        docker_config_path.write_text(json.dumps(config, indent=2))
        ok("Docker credentials written. Ready to push.")

        # Tag, push, then remove the ECR tag so it doesn't linger locally
        step("Tagging and pushing images...")
        for local_img, ecr_tag in IMAGES:
            remote = f"{ECR_REGISTRY}/{ECR_REPO}:{ecr_tag}"
            print(f"\n  {local_img}  =>  {remote}")
            run(["docker", "tag",  local_img, remote])
            run(["docker", "push", remote])
            run(["docker", "rmi",  remote])
            ok(f"Pushed and cleaned up local ECR tag: {remote}")

        banner("All images pushed successfully")
        print("\nImages now available at:")
        for _, ecr_tag in IMAGES:
            print(f"  {ECR_REGISTRY}/{ECR_REPO}:{ecr_tag}")

    except (ClientError, NoCredentialsError) as e:
        print(f"\nAWS error: {e}")
        sys.exit(1)

    finally:
        # Remove ECR entry from ~/.docker/config.json
        try:
            docker_config_path = pathlib.Path.home() / ".docker" / "config.json"
            if docker_config_path.exists():
                config = json.loads(docker_config_path.read_text())
                config.get("auths", {}).pop(ECR_REGISTRY, None)
                docker_config_path.write_text(json.dumps(config, indent=2))
        except Exception:
            pass

        # Clear credentials from memory
        _zero_string(access_key)
        _zero_string(secret_key)
        if docker_password:
            _zero_string(docker_password)
        del access_key, secret_key, docker_password
        print("\n[--] AWS credentials cleared. ECR entry removed from Docker config.")


if __name__ == "__main__":
    main()
