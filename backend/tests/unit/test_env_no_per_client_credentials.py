"""R17-D3 Addendum A (T13) - AC12: no per-client credentials in tracked env files.

`backend/.env.example` and `backend/.env.template` must contain only instance
configuration (DATABASE_URL, CREDENTIAL_VAULT_KEY, CORS_ORIGINS, OAuth app
registrations, feature flags, ...). Per-client connector credentials live only
in the per-org encrypted vault, entered through the Integration Hub.

This guards the *documents that seed every deployment's .env*: reintroducing a
per-client variable here would steer operators straight back into the
replace-.env-per-client model the addendum retires. The broader code-level
enforcement (no os.getenv credential reads outside the vault layer) is T14's
test; this one owns the AC12 file-content half.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]

TRACKED_ENV_FILES = [
    BACKEND_DIR / ".env.example",
    BACKEND_DIR / ".env.template",
]

# Section 3 of the addendum, verbatim: every per-client secret purged in T13.
# Exact variable names — instance-config lookalikes (SALESFORCE_CLIENT_ID,
# SERVICENOW_INSTANCE, JIRA_PROJECT_KEY, NCINO_CLIENT_SECRET = OAuth app
# registration) are deliberately NOT in this list.
FORBIDDEN_PER_CLIENT_VARS = [
    "SF_ACCESS_TOKEN",
    "SF_CLIENT_ID",
    "SF_USER",
    "SF_INSTANCE_URL",
    "JIRA_URL",
    "JIRA_USER",
    "JIRA_TOKEN",
    "SERVICENOW_URL",
    "SERVICENOW_USER",
    "SERVICENOW_PASS",
    "SERVICENOW_TOKEN",
    "NCINO_INSTANCE_URL",
    "NCINO_ACCESS_TOKEN",
    "STRS_INSTANCE_URL",
    "STRS_ACCESS_TOKEN",
    # Native DB connector service-account credentials (R17-D3 Addendum A §2 —
    # databases). Host/port/database are instance config and are NOT listed here;
    # only the username/password secrets belong solely in the per-org vault.
    "ORACLE_DB_USERNAME",
    "ORACLE_DB_PASSWORD",
    "POSTGRESQL_USERNAME",
    "POSTGRESQL_PASSWORD",
]

# The addendum's "Keeps" list — instance configuration that must survive the
# purge so AC12's second half ("only instance configuration remains") is not
# trivially satisfied by an empty file.
REQUIRED_INSTANCE_VARS = [
    "DATABASE_URL",
    "CREDENTIAL_VAULT_KEY",
    "CORS_ORIGINS",
    "OAUTH_REDIRECT_URI",
]


def _defined_var_names(path: Path) -> set[str]:
    """Variable names DEFINED in an env file (uncommented NAME=... lines)."""
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            names.add(match.group(1))
    return names


@pytest.mark.parametrize("env_file", TRACKED_ENV_FILES, ids=lambda p: p.name)
def test_no_per_client_credentials_defined(env_file: Path):
    assert env_file.exists(), f"{env_file.name} missing — update TRACKED_ENV_FILES"
    defined = _defined_var_names(env_file)
    offenders = sorted(defined.intersection(FORBIDDEN_PER_CLIENT_VARS))
    assert not offenders, (
        f"{env_file.name} defines per-client credential vars {offenders} (AC12). "
        f"Per-client connector credentials live only in the per-org encrypted "
        f"vault via the Integration Hub — never in .env."
    )


@pytest.mark.parametrize("env_file", TRACKED_ENV_FILES, ids=lambda p: p.name)
def test_retained_instance_configuration_present(env_file: Path):
    defined = _defined_var_names(env_file)
    missing = sorted(v for v in REQUIRED_INSTANCE_VARS if v not in defined)
    assert not missing, (
        f"{env_file.name} lost retained instance-only vars {missing} — "
        f"the T13 purge removes per-client credentials, not instance config."
    )
