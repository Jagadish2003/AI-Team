"""R17-D3 Addendum A T15 (AT-516) — legacy-env → vault migration command tests.

Verifies the one-time admin command imports legacy per-client credential env
vars into the per-org vault as static credentials, exactly once, on explicit
invocation (AC14): it is org-scoped, refuses to clobber an existing credential
without --force, does nothing on --dry-run, reports the env vars to remove, and
never falls over when no legacy env is set.

FAKE CREDENTIALS: every value below is a non-real, test-only credential.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.auth import vault
from scripts.migrate_env_credentials_to_vault import (
    migrate_env_credentials_to_vault,
    static_credential_schema_ready,
)

_VAULT_KEY = Fernet.generate_key().decode()

_FAKE_JIRA_TOKEN = "FAKE-jira-api-token-0123456789"
_FAKE_JIRA_USER = "svc-agentiq@example.com"
_FAKE_JIRA_URL = "https://example.atlassian.net"
_FAKE_SN_PASS = "FAKE-servicenow-pass-abcdef"
_FAKE_SN_TOKEN = "FAKE-servicenow-token-987654"
_FAKE_SF_TOKEN = "FAKE-sf-access-token-aaaa"
_FAKE_SF_URL = "https://acme.my.salesforce.com"


def _vault_env() -> dict:
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY}


def _legacy_env() -> dict:
    return {
        "JIRA_URL": _FAKE_JIRA_URL,
        "JIRA_USER": _FAKE_JIRA_USER,
        "JIRA_TOKEN": _FAKE_JIRA_TOKEN,
        "SERVICENOW_URL": "https://acme.service-now.com",
        "SERVICENOW_TOKEN": _FAKE_SN_TOKEN,
        "SF_INSTANCE_URL": _FAKE_SF_URL,
        "SF_ACCESS_TOKEN": _FAKE_SF_TOKEN,
    }


def _cleanup(org: str) -> None:
    for cid in ("salesforce", "jira", "servicenow", "ncino", "strs", "oracle_db", "postgresql"):
        try:
            vault.revoke_static_credential(org, cid)
        except Exception:
            pass


def test_migrates_legacy_env_into_the_vault_as_static_credentials():
    org = "org-migrate-basic"
    with patch.dict(os.environ, _vault_env()):
        try:
            report = migrate_env_credentials_to_vault(org, env=_legacy_env())

            migrated = {c.connector_id for c in report.migrated}
            assert migrated == {"jira", "servicenow", "salesforce"}

            jira = vault.get_static_credential(org, "jira")
            assert jira.username == _FAKE_JIRA_USER
            assert jira.secret == _FAKE_JIRA_TOKEN
            assert jira.base_url == _FAKE_JIRA_URL

            sn = vault.get_static_credential(org, "servicenow")
            assert sn.secret == _FAKE_SN_TOKEN  # TOKEN preferred over PASS

            sf = vault.get_static_credential(org, "salesforce")
            assert sf.secret == _FAKE_SF_TOKEN
            assert sf.base_url == _FAKE_SF_URL

            # Connectors with no legacy env are skipped, not migrated.
            skipped = {c.connector_id for c in report.connectors if c.action == "skipped_no_env"}
            assert skipped == {"ncino", "strs", "oracle_db", "postgresql"}
        finally:
            _cleanup(org)


def test_migrates_legacy_db_credentials_into_the_vault():
    """R17-D3 Addendum A §2 — native DB service-account credentials migrate into
    the per-org vault as static credentials (host/port stay instance config)."""
    org = "org-migrate-db"
    legacy = {
        "ORACLE_DB_USERNAME": "oracle_svc",
        "ORACLE_DB_PASSWORD": "oracle-secret",
        "POSTGRESQL_USERNAME": "pg_svc",
        "POSTGRESQL_PASSWORD": "pg-secret",
    }
    with patch.dict(os.environ, _vault_env()):
        try:
            report = migrate_env_credentials_to_vault(org, env=legacy)
            migrated = {c.connector_id for c in report.migrated}
            assert {"oracle_db", "postgresql"}.issubset(migrated)

            oracle = vault.get_static_credential(org, "oracle_db")
            assert oracle.username == "oracle_svc"
            assert oracle.secret == "oracle-secret"

            pg = vault.get_static_credential(org, "postgresql")
            assert pg.username == "pg_svc"
            assert pg.secret == "pg-secret"
        finally:
            _cleanup(org)


def test_reports_env_vars_to_remove():
    org = "org-migrate-removelist"
    with patch.dict(os.environ, _vault_env()):
        try:
            report = migrate_env_credentials_to_vault(org, env=_legacy_env())
            to_remove = set(report.env_vars_to_remove)
            # Every consumed env var is reported for removal from .env.
            assert {"JIRA_URL", "JIRA_USER", "JIRA_TOKEN",
                    "SERVICENOW_URL", "SERVICENOW_TOKEN",
                    "SF_INSTANCE_URL", "SF_ACCESS_TOKEN"}.issubset(to_remove)
        finally:
            _cleanup(org)


def test_servicenow_falls_back_to_basic_password_when_no_token():
    org = "org-migrate-snpass"
    env = {
        "SERVICENOW_URL": "https://acme.service-now.com",
        "SERVICENOW_USER": "svc-sn",
        "SERVICENOW_PASS": _FAKE_SN_PASS,
    }
    with patch.dict(os.environ, _vault_env()):
        try:
            migrate_env_credentials_to_vault(org, env=env)
            sn = vault.get_static_credential(org, "servicenow")
            assert sn.username == "svc-sn"
            assert sn.secret == _FAKE_SN_PASS
        finally:
            _cleanup(org)


def test_dry_run_writes_nothing():
    org = "org-migrate-dry"
    with patch.dict(os.environ, _vault_env()):
        try:
            report = migrate_env_credentials_to_vault(org, env=_legacy_env(), dry_run=True)
            assert {c.connector_id for c in report.migrated} == {"jira", "servicenow", "salesforce"}
            assert all(c.action != "migrated" for c in report.connectors)
            # Nothing was actually written.
            assert vault.get_static_credential(org, "jira") is None
            assert vault.get_static_credential(org, "servicenow") is None
        finally:
            _cleanup(org)


def test_exactly_once_second_run_skips_without_force():
    """AC14: a repeat run must not silently clobber an existing vault credential."""
    org = "org-migrate-once"
    with patch.dict(os.environ, _vault_env()):
        try:
            migrate_env_credentials_to_vault(org, env=_legacy_env())

            # A newer credential connected after the first migration.
            vault.store_static_credential(
                org, "jira", username="new-user", secret="NEWER-token", base_url=_FAKE_JIRA_URL
            )

            report2 = migrate_env_credentials_to_vault(org, env=_legacy_env())
            actions = {c.connector_id: c.action for c in report2.connectors}
            assert actions["jira"] == "skipped_exists"
            assert actions["servicenow"] == "skipped_exists"

            # The newer credential is intact — not overwritten by the legacy env.
            assert vault.get_static_credential(org, "jira").secret == "NEWER-token"
        finally:
            _cleanup(org)


def test_force_overwrites_existing_credential():
    org = "org-migrate-force"
    with patch.dict(os.environ, _vault_env()):
        try:
            vault.store_static_credential(
                org, "jira", username="old", secret="OLD-token", base_url="https://old.example.com"
            )
            report = migrate_env_credentials_to_vault(org, env=_legacy_env(), force=True)
            assert {c.connector_id for c in report.migrated} >= {"jira"}
            assert vault.get_static_credential(org, "jira").secret == _FAKE_JIRA_TOKEN
        finally:
            _cleanup(org)


def test_no_legacy_env_migrates_nothing():
    org = "org-migrate-empty"
    with patch.dict(os.environ, _vault_env()):
        report = migrate_env_credentials_to_vault(org, env={})
        assert report.migrated == []
        assert all(c.action == "skipped_no_env" for c in report.connectors)


def test_schema_preflight_true_on_provisioned_db():
    """The static-credential columns (T10) are present on a provisioned DB, so
    the command's preflight passes and it proceeds to migrate."""
    assert static_credential_schema_ready() is True


def test_migration_is_org_scoped():
    org_a, org_b = "org-migrate-iso-a", "org-migrate-iso-b"
    with patch.dict(os.environ, _vault_env()):
        try:
            migrate_env_credentials_to_vault(org_a, env=_legacy_env())
            assert vault.get_static_credential(org_a, "jira") is not None
            assert vault.get_static_credential(org_b, "jira") is None
        finally:
            _cleanup(org_a)
            _cleanup(org_b)
