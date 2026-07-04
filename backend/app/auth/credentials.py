"""R17-D3 Addendum A T9 (AT-510) — the single connector-credential resolution path.

A thin resolution layer over the existing per-org encrypted vault. This becomes
the ONLY way any ingestion or health-check code obtains connector credentials.
Every ingestor changes from reading a process-global env var:

    token = os.getenv('JIRA_TOKEN', '')            # single-tenant, wrong

to resolving from the per-org vault via this module:

    creds = get_connector_credentials(run_ctx.org_id, 'jira')   # per-org, correct

Why this layer exists (R17-D3 Addendum A, §1 / AC11):
    Environment variables are process-global by nature and can never be per-org —
    two orgs sharing one instance would read the same Salesforce/Jira token. The
    vault already keys credentials per (org_id, connector_id) with Fernet
    encryption and decrypt-at-use; the ingestion layer was simply never wired to
    it. This module retires the parallel env-based credential set.

No silent fallback to ``os.getenv`` — this is deliberate. A missing credential is
a configuration state to SURFACE (``CredentialsNotConfigured``), never to paper
over with an env fallback: a fallback would quietly preserve the single-tenant
behaviour and defeat the entire fix.
"""
from __future__ import annotations

from typing import Optional, Union

from app.auth import vault
from app.auth.models import StaticCredentialRecord, TokenRecord

ConnectorCredential = Union[TokenRecord, StaticCredentialRecord]


class CredentialsNotConfigured(Exception):
    """Raised when an org has no vault credential for the requested connector.

    Surfaces a clear 'connector not configured' state (R17-D3 Addendum A, AC11).
    Callers should present this as a connector-not-configured condition (e.g. a
    degraded/needs-configuration status at the route or ingest layer) — never
    treat it as a transient error and never fall back to environment variables.
    """

    def __init__(self, org_id: str, connector_id: str) -> None:
        self.org_id = org_id
        self.connector_id = connector_id
        super().__init__(
            f"Connector '{connector_id}' is not configured for org '{org_id}'. "
            "No credential exists in the vault; enter it through the Integration Hub."
        )


def get_connector_credentials(org_id: str, connector_id: str) -> ConnectorCredential:
    """THE single path to connector credentials, resolved from the per-org vault.

    Resolves the credential for ``connector_id`` scoped to ``org_id`` from the
    Fernet-encrypted vault, decrypted at use and never cached to disk or env.
    Returns whichever record type the connector holds: a :class:`TokenRecord`
    for an OAuth connector or a :class:`StaticCredentialRecord` for a static one
    (Jira API token, ServiceNow user/password, native DB credentials).

    Raises :class:`CredentialsNotConfigured` when the org has no credential for
    this connector — callers surface that as a clear 'connector not configured'
    state, never a silent fallback to environment variables (AC11).
    """
    record = vault.get_credential(org_id, connector_id)
    if record is None:
        raise CredentialsNotConfigured(org_id, connector_id)
    return record  # decrypted at use; never cached to disk or env


def get_connector_secret(org_id: str, connector_id: str) -> str:
    """Return the single secret string a connector authenticates with.

    Convenience over :func:`get_connector_credentials` for the common ingestion
    case that just needs one bearer value: the OAuth ``access_token`` for a
    :class:`TokenRecord`, or the ``secret`` for a :class:`StaticCredentialRecord`
    (the API token / password). Raises :class:`CredentialsNotConfigured` exactly
    as :func:`get_connector_credentials` does — never falls back to env (AC8/AC11).
    """
    record = get_connector_credentials(org_id, connector_id)
    if isinstance(record, StaticCredentialRecord):
        return record.secret
    return record.access_token


def try_get_connector_credentials(
    org_id: str, connector_id: str
) -> Optional[ConnectorCredential]:
    """Like :func:`get_connector_credentials` but returns ``None`` instead of
    raising when the connector is not configured.

    For ingestion/health call sites that already model 'not configured' as a
    degraded/skipped state and prefer a ``None`` check to a try/except. Still
    resolves exclusively from the per-org vault — never from env (AC8/AC11).
    """
    try:
        return get_connector_credentials(org_id, connector_id)
    except CredentialsNotConfigured:
        return None
