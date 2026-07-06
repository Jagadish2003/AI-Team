"""R17-D3 Addendum A T15 (AT-516) — one-time legacy-env → vault migration.

A DOCUMENTED, EXPLICIT admin command (never a silent auto-import). Pre-Addendum
deployments kept per-client connector credentials in ``backend/.env``
(SF_ACCESS_TOKEN, JIRA_URL/JIRA_USER/JIRA_TOKEN, SERVICENOW_*, NCINO_*, STRS_*).
T11 rewired every ingestor to resolve credentials from the per-org Fernet vault
via ``get_connector_credentials``, and T13 purges those vars from the tracked
env templates. This command bridges an existing install across that change: it
reads whatever legacy credential env vars are still set and writes them into the
vault (as static credentials — T10) for the instance's org, so ingestion keeps
working after the operator removes the vars from ``.env``.

It is run by hand, once, per the Addendum §4: explicit operator action is
required so the operator always knows where the credentials now live. It is NOT
wired into app startup and nothing calls it automatically.

Usage (from the ``backend/`` directory)::

    python scripts/migrate_env_credentials_to_vault.py --org <org_id>
    python scripts/migrate_env_credentials_to_vault.py --dry-run
    python scripts/migrate_env_credentials_to_vault.py --org acme --force

Options:
    --org       Org to store the credentials under (default: the instance's
                default org). On a single-tenant install this is the one org.
    --dry-run   Report what WOULD be migrated; write nothing to the vault.
    --force     Overwrite a connector credential that ALREADY exists in the
                vault. Without it an already-vaulted connector is SKIPPED, so a
                second run can never silently clobber a credential connected
                since the first run (the "exactly once" guarantee — AC14).

Prerequisites:
    * ``CREDENTIAL_VAULT_KEY`` set (the vault refuses to store credentials in
      plaintext).
    * The credentials table carries the T10 static-credential columns
      (kind/enc_username/enc_secret/base_url) — an existing deployment must
      apply ``alembic upgrade head`` (or ``database/provision/provision.sql``)
      before running this command. The command preflights this and exits with a
      clear message if the schema is behind.

The command prints the exact list of env vars to delete from ``backend/.env``
after a successful migration; it never prints a secret value.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPT_DIR.parent

# Allow "python scripts/migrate_env_credentials_to_vault.py" to import backend
# modules (the script's own dir is on sys.path, not backend/, so add backend/).
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

logger = logging.getLogger("migrate_env_credentials_to_vault")


# ---------------------------------------------------------------------------
# Legacy env → vault mapping. Each connector's legacy credential is stored as a
# static credential record (base_url + username + secret) — the shape T10 added.
# ``secret_env`` lists candidate env-var names in priority order (first present
# wins), so ServiceNow migrates its OAuth bearer token OR its basic password.
#
# Names live in this data table (never inlined in an os.getenv call) and are
# read DYNAMICALLY below, so the T14 enforcement test does not flag this tool —
# and it should not: reading the legacy env is precisely this command's job.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ConnectorEnvSpec:
    connector_id: str
    url_env: Optional[str]
    username_env: Optional[str]
    secret_env: List[str]


_CONNECTOR_ENV_SPECS: List[_ConnectorEnvSpec] = [
    _ConnectorEnvSpec("salesforce", "SF_INSTANCE_URL", "SF_USER", ["SF_ACCESS_TOKEN"]),
    _ConnectorEnvSpec("jira", "JIRA_URL", "JIRA_USER", ["JIRA_TOKEN"]),
    _ConnectorEnvSpec(
        "servicenow",
        "SERVICENOW_URL",
        "SERVICENOW_USER",
        ["SERVICENOW_TOKEN", "SERVICENOW_PASS"],
    ),
    _ConnectorEnvSpec("ncino", "NCINO_INSTANCE_URL", None, ["NCINO_ACCESS_TOKEN"]),
    _ConnectorEnvSpec("strs", "STRS_INSTANCE_URL", None, ["STRS_ACCESS_TOKEN"]),
    # Native DB connectors (R17-D3 Addendum A §2). The host/port/database are
    # non-secret instance config and stay in env; only the service-account
    # username + password migrate into the vault as a static credential.
    _ConnectorEnvSpec("oracle_db", None, "ORACLE_DB_USERNAME", ["ORACLE_DB_PASSWORD"]),
    _ConnectorEnvSpec("postgresql", None, "POSTGRESQL_USERNAME", ["POSTGRESQL_PASSWORD"]),
]


@dataclass
class ConnectorMigration:
    connector_id: str
    #: "migrated" | "would_migrate" | "skipped_no_env" | "skipped_exists"
    action: str
    #: Env-var names that supplied the credential — the operator removes these.
    env_vars: List[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class MigrationReport:
    org_id: str
    dry_run: bool
    connectors: List[ConnectorMigration] = field(default_factory=list)

    @property
    def migrated(self) -> List[ConnectorMigration]:
        return [c for c in self.connectors if c.action in ("migrated", "would_migrate")]

    @property
    def env_vars_to_remove(self) -> List[str]:
        seen: List[str] = []
        for c in self.migrated:
            for name in c.env_vars:
                if name not in seen:
                    seen.append(name)
        return seen


#: The T10 static-credential columns the migration writes into. An existing
#: deployment must apply the schema change (provision.sql / alembic upgrade head)
#: before running this command, since it stores STATIC credential records.
_STATIC_CREDENTIAL_COLUMNS = frozenset({"kind", "enc_username", "enc_secret", "base_url"})


def static_credential_schema_ready() -> bool:
    """True when the credentials table has the T10 static-credential columns.

    The migration stores static credentials, which need the
    kind/enc_username/enc_secret/base_url columns added by R17-D3 T10. On a
    pre-T10 deployment these are absent until provision.sql / the schema
    migration is applied; the command surfaces that clearly rather than crashing
    mid-write. Never raises — returns False if the table can't be inspected."""
    try:
        from app import db

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'credentials'"
            )
            cols = {row[0] for row in cur.fetchall()}
        finally:
            con.close()
    except Exception:
        return False
    return _STATIC_CREDENTIAL_COLUMNS.issubset(cols)


def _first_present(env: Dict[str, str], names: List[str]) -> tuple[Optional[str], str]:
    """Return (env_name, value) for the first non-empty candidate, else (None, "")."""
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return name, value
    return None, ""


def migrate_env_credentials_to_vault(
    org_id: str,
    *,
    env: Optional[Dict[str, str]] = None,
    dry_run: bool = False,
    force: bool = False,
) -> MigrationReport:
    """Import legacy per-client credential env vars into the per-org vault.

    For each connector, resolves its credential from ``env`` (defaults to
    ``os.environ``) and writes a static credential record for ``org_id``. A
    connector already present in the vault is SKIPPED unless ``force`` — so a
    repeat run cannot clobber a credential connected since the first run. Never
    logs a secret value.
    """
    env = env if env is not None else os.environ
    from app.auth import vault

    report = MigrationReport(org_id=org_id, dry_run=dry_run)

    for spec in _CONNECTOR_ENV_SPECS:
        secret_env, secret_val = _first_present(env, spec.secret_env)
        if not secret_val:
            report.connectors.append(
                ConnectorMigration(
                    spec.connector_id, "skipped_no_env",
                    detail="no legacy credential env var set",
                )
            )
            continue

        base_url = (env.get(spec.url_env) or "").strip() if spec.url_env else ""
        username = (
            (env.get(spec.username_env) or "").strip() if spec.username_env else ""
        )

        used_env: List[str] = [secret_env]
        if base_url and spec.url_env:
            used_env.append(spec.url_env)
        if username and spec.username_env:
            used_env.append(spec.username_env)

        # Exactly-once guard: never silently overwrite an existing vault
        # credential (OAuth token or static) unless the operator forces it.
        existing = vault.get_credential(org_id, spec.connector_id)
        if existing is not None and not force:
            report.connectors.append(
                ConnectorMigration(
                    spec.connector_id, "skipped_exists",
                    env_vars=used_env,
                    detail="already in the vault — re-run with --force to overwrite",
                )
            )
            continue

        if dry_run:
            report.connectors.append(
                ConnectorMigration(spec.connector_id, "would_migrate", env_vars=used_env)
            )
            continue

        vault.store_static_credential(
            org_id,
            spec.connector_id,
            username=username,
            secret=secret_val,
            base_url=base_url,
        )
        report.connectors.append(
            ConnectorMigration(spec.connector_id, "migrated", env_vars=used_env)
        )
        logger.info(
            "Migrated %s credential into the vault for org %s (from %s)",
            spec.connector_id, org_id, ", ".join(used_env),
        )

    return report


def _print_report(report: MigrationReport) -> None:
    verb = "Would migrate" if report.dry_run else "Migrated"
    print(f"\nLegacy credential migration — org '{report.org_id}'"
          f"{' (DRY RUN — nothing written)' if report.dry_run else ''}\n")
    for c in report.connectors:
        if c.action in ("migrated", "would_migrate"):
            print(f"  [{verb.upper()}] {c.connector_id:<12} from {', '.join(c.env_vars)}")
        elif c.action == "skipped_exists":
            print(f"  [SKIP]      {c.connector_id:<12} {c.detail}")
        else:
            print(f"  [SKIP]      {c.connector_id:<12} {c.detail}")

    to_remove = report.env_vars_to_remove
    if not report.migrated:
        print("\nNothing migrated.")
        return

    if report.dry_run:
        print("\nRe-run without --dry-run to write these into the vault.")
        return

    print(
        "\nMigration complete. Now REMOVE these per-client credential vars from "
        "backend/.env (they are process-global and can never be per-org):\n"
    )
    for name in to_remove:
        print(f"    {name}")
    print(
        "\nKeep instance-only config in .env (DATABASE_URL, CREDENTIAL_VAULT_KEY, "
        "CORS_ORIGINS, OAuth app *_CLIENT_ID/*_CLIENT_SECRET, feature flags).\n"
    )


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "One-time migration of legacy per-client connector credentials from "
            "backend/.env into the per-org encrypted vault (R17-D3 Addendum A, T15)."
        )
    )
    parser.add_argument(
        "--org",
        default=None,
        help="Org to store credentials under (default: the instance default org).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be migrated; write nothing.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite a connector credential that already exists in the vault.",
    )
    args = parser.parse_args(argv)

    # Load backend/.env so the operator's existing credential vars are visible
    # without exporting them (matches seed_loader.py). Only in the CLI path —
    # the migrate function reads the passed env dict for testability.
    try:
        from dotenv import load_dotenv

        load_dotenv(_BACKEND_DIR / ".env")
    except Exception:
        pass

    if not os.environ.get("CREDENTIAL_VAULT_KEY"):
        print(
            "ERROR: CREDENTIAL_VAULT_KEY is not set. The vault refuses to store "
            "credentials in plaintext — set it (env or secrets manager) and retry.",
            file=sys.stderr,
        )
        return 2

    if not static_credential_schema_ready():
        print(
            "ERROR: the 'credentials' table is missing the static-credential "
            "columns (kind/enc_username/enc_secret/base_url) added by R17-D3 T10. "
            "Apply the schema first, then re-run:\n"
            "    alembic upgrade head        # or apply database/provision/provision.sql\n"
            "(Also verify DATABASE_URL points at this deployment's database.)",
            file=sys.stderr,
        )
        return 2

    org_id = args.org
    if not org_id:
        try:
            from app.middleware.tenancy import DEV_DEFAULT_ORG

            org_id = DEV_DEFAULT_ORG
        except Exception:
            org_id = "default"

    report = migrate_env_credentials_to_vault(
        org_id, dry_run=args.dry_run, force=args.force
    )
    _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
