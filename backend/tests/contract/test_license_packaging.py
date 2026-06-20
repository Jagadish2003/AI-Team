"""LIC-1 / T1 / AC10 — the signing CLI must not reach the customer image.

AC10 ("the signing CLI `license/` is NOT present in the customer Docker image or
build artifact"). The tooling lives at ``backend/license/`` for organisation, so
it now sits INSIDE the Docker build context (``backend/``). It is kept out of the
image by ``backend/.dockerignore`` excluding ``license/`` — the standard
"in the context dir but excluded from the image" mechanism.

These checks lock that down so a future change that would smuggle the private-key
tooling into the image (deleting the .dockerignore entry, or a ``COPY`` that
force-adds it) fails CI instead of shipping silently. They run in the contract
suite (the PR gate) and need no Docker daemon.
"""
from __future__ import annotations

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_BACKEND = os.path.join(_REPO_ROOT, "backend")
_LICENSE = os.path.join(_BACKEND, "license")
_DOCKERFILE = os.path.join(_BACKEND, "Dockerfile")
_BACKEND_DOCKERIGNORE = os.path.join(_BACKEND, ".dockerignore")
_ROOT_DOCKERIGNORE = os.path.join(_REPO_ROOT, ".dockerignore")


def _dockerignore_patterns(path: str) -> set[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return {
            ln.strip().rstrip("/")
            for ln in fh
            if ln.strip() and not ln.strip().startswith("#")
        }


def test_license_tooling_lives_under_backend():
    """The signing tooling is at backend/license/ (inside the build context)."""
    assert os.path.isdir(_LICENSE), "license/ tooling should exist at backend/license/"
    # The generator CLI is the sensitive piece — confirm it's the one we guard.
    assert os.path.isfile(os.path.join(_LICENSE, "generate_license.py"))


def test_backend_dockerignore_excludes_license_tooling():
    """backend/.dockerignore must exclude license/ so `COPY . .` skips it (AC10)."""
    assert os.path.isfile(_BACKEND_DOCKERIGNORE), "backend/.dockerignore must exist"
    patterns = _dockerignore_patterns(_BACKEND_DOCKERIGNORE)
    assert "license" in patterns, (
        "backend/.dockerignore must exclude the license/ tooling — otherwise the "
        "private-key signing CLI would be copied into the customer image (AC10)."
    )
    assert "*.pem" in patterns, "backend/.dockerignore must exclude private key PEMs"


def test_backend_dockerfile_does_not_force_copy_license_tooling():
    """No COPY/ADD line force-adds the license/ tooling past the .dockerignore."""
    with open(_DOCKERFILE, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    offenders = [
        ln
        for ln in lines
        if ln.strip().upper().startswith(("COPY", "ADD")) and "license" in ln.lower()
    ]
    assert not offenders, f"Dockerfile must not copy license/ tooling: {offenders}"


def test_root_dockerignore_excludes_license_tooling():
    """A root-context build (`docker build -f backend/Dockerfile .`) is covered by
    a root .dockerignore that excludes the tooling and key material."""
    assert os.path.isfile(_ROOT_DOCKERIGNORE), "a root .dockerignore must exist (AC10 fail-closed)"
    patterns = _dockerignore_patterns(_ROOT_DOCKERIGNORE)
    assert "backend/license" in patterns, ".dockerignore must exclude backend/license/ tooling"
    assert "*.pem" in patterns, ".dockerignore must exclude private key PEMs"


def test_no_private_key_committed_in_license_dir():
    """Defence in depth: no private key material sits directly in the tooling dir
    (it is git-ignored, but a stray file here would be a leak risk)."""
    leaked = [name for name in os.listdir(_LICENSE) if name.endswith((".pem", ".key"))]
    assert not leaked, f"private key material must not sit in backend/license/: {leaked}"
