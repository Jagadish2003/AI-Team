"""R191-H1 follow-up — a Salesforce CredentialRecordError degrades the run, never crashes it.

Regression lock for the review finding: ``salesforce.py`` raises
``operational_config.CredentialRecordError`` (a sibling of ``salesforce.IngestError``,
not a subclass) when the credential record exists but lacks its instance URL. The
discovery runner's Salesforce ingest block must catch it alongside ``IngestError``
(``except (SFError, CredentialRecordError)``) so a single misconfigured connector
degrades to a per-system failure — it must never propagate uncaught and terminate
the whole run.

This test fails if someone drops ``CredentialRecordError`` from that except clause
(the exact regression the finding describes).
"""
from __future__ import annotations

import uuid

from discovery import runner
from discovery.ingest import salesforce
from discovery.ingest.operational_config import CredentialRecordError


def test_salesforce_credential_record_error_degrades_not_crashes(monkeypatch):
    # Salesforce raises the (non-IngestError) CredentialRecordError, exactly as it
    # does live when the credential record is missing its instance URL.
    def _raise_credential_error():
        raise CredentialRecordError(
            org_id="o",
            connector_id="salesforce",
            missing_field="url",
            message="Salesforce credential record is missing its instance URL.",
        )

    monkeypatch.setattr(salesforce, "ingest", _raise_credential_error)

    # ServiceNow / Jira still return their offline fixtures, so the run has data and
    # proceeds — the point is that the Salesforce failure is contained.
    result = runner.run(
        mode="offline",
        org_id=f"org_credfail_{uuid.uuid4().hex[:8]}",
        run_id=f"run_{uuid.uuid4().hex[:8]}",
    )

    # The run completed (did not raise) and Salesforce is recorded as a per-system
    # failure with its actionable reason — never an uncaught crash.
    assert result["perSystem"].get("salesforce") == "failed"
    assert "salesforce" in result["ingestErrors"]
    assert "instance URL" in result["ingestErrors"]["salesforce"]
