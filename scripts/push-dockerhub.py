#!/usr/bin/env python3
"""
push-dockerhub.py — Tag and push AgentIQ images to Docker Hub.

No extra dependencies — uses only Python stdlib + Docker CLI.

Prerequisites:
    - Docker running
    - Local images already built:
        agentiq-postgres:1.0
        agentiq-backend:latest
        agentiq-frontend:1.0
    - Docker Hub account with push access to DOCKERHUB_USER
"""

import getpass
import subprocess
import sys

# ---------------------------------------------------------------------------
# !! CONFIGURE THIS before running !!
# ---------------------------------------------------------------------------

DOCKERHUB_USER = "REPLACE_WITH_YOUR_DOCKERHUB_USERNAME"   # e.g. "mycompany"

# ---------------------------------------------------------------------------
# Image map: local name  ->  Docker Hub tag
# ---------------------------------------------------------------------------

IMAGES = [
    ("agentiq-postgres:1.0",   f"{DOCKERHUB_USER}/agentiq-postgres:1.0"),
    ("agentiq-backend:latest", f"{DOCKERHUB_USER}/agentiq-backend:latest"),
    ("agentiq-frontend:1.0",   f"{DOCKERHUB_USER}/agentiq-frontend:1.0"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def banner(msg: str) -> None:
    print(f"\n{'=' * 55}\n  {msg}\n{'=' * 55}")


def step(msg: str) -> None:
    print(f"\n[>>] {msg}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def run(cmd: list, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check)


def check_docker() -> None:
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("\n[ERROR] Docker is not running. Start Docker and retry.")
        sys.exit(1)
    ok("Docker is running.")


def check_local_images() -> None:
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True, check=True,
    )
    available = set(result.stdout.strip().splitlines())
    missing = [local for local, _ in IMAGES if local not in available]
    if missing:
        print("\n[ERROR] Missing local images — build them first:")
        for m in missing:
            print(f"  {m}")
        sys.exit(1)
    ok("All local images found.")


def check_config() -> None:
    if DOCKERHUB_USER == "REPLACE_WITH_YOUR_DOCKERHUB_USERNAME":
        print("\n[ERROR] Set DOCKERHUB_USER at the top of this script before running.")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    check_config()

    banner("AgentIQ — Docker Hub Push")
    print(f"  Docker Hub user : {DOCKERHUB_USER}")
    print(f"  Images          : {len(IMAGES)}")

    step("Checking prerequisites...")
    check_docker()
    check_local_images()

    # Prompt for Docker Hub credentials
    step("Docker Hub credentials (not stored — logged out after push):")
    dh_user     = input(f"  Username [{DOCKERHUB_USER}] : ").strip() or DOCKERHUB_USER
    dh_password = getpass.getpass("  Password / Access Token  : ")

    if not dh_password:
        print("\n[ERROR] Password / access token is required.")
        sys.exit(1)

    try:
        # Login
        step("Logging in to Docker Hub...")
        login = subprocess.run(
            ["docker", "login", "--username", dh_user, "--password-stdin"],
            input=dh_password, text=True, capture_output=True,
        )
        if login.returncode != 0:
            print(f"\n[ERROR] Docker Hub login failed:\n{login.stderr}")
            sys.exit(1)
        ok("Logged in to Docker Hub.")

        # Tag and push
        step("Tagging and pushing images...")
        for local_img, remote_img in IMAGES:
            print(f"\n  {local_img}  =>  {remote_img}")
            run(["docker", "tag",  local_img, remote_img])
            run(["docker", "push", remote_img])
            ok(f"Pushed {remote_img}")

        banner("All images pushed to Docker Hub")
        print("\nImages available at:")
        for _, remote_img in IMAGES:
            print(f"  docker pull {remote_img}")

    finally:
        subprocess.run(["docker", "logout"], capture_output=True)
        # Zero-out password in memory (best-effort)
        try:
            import ctypes
            enc = dh_password.encode()
            buf = (ctypes.c_char * len(enc)).from_address(id(enc) + 24)
            ctypes.memset(buf, 0, len(enc))
        except Exception:
            pass
        del dh_password
        print("\n[--] Docker Hub credentials cleared. Logged out.")


if __name__ == "__main__":
    main()
