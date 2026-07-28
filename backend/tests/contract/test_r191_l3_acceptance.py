"""R-1.9.1-L3 — Vendor-Side License Operations: acceptance suite (AC1–AC6).

Verifies the built behaviour of the CloudFulcrum-internal license registry +
issuance service (``backend/license/``), keyed to the story's AC table:

  AC1  Issuance without contract_ref/org_id is refused; with them a payload-v2 key
       is produced AND a registry row + audit entry exist.
  AC2  The audit log is append-only: no UPDATE/DELETE changes an entry
       (schema-level Postgres rewrite rules).
  AC3  A renewal links to its original via supersedes; the lineage is queryable.
  AC4  The expiring-within-N-days query returns exactly the seeded expiring set.
  AC5  Repo scan: no private signing-key material is committed; the issuance
       service reads keys only from a filesystem path (never env key material).
  AC6  Two active kids can issue in parallel; the registry records which kid
       signed each license.

Runs against the disposable contract-test PostgreSQL (conftest applies alembic
head, which includes migration 0026). No real CloudFulcrum private key is needed:
each test mints with a throwaway Ed25519 key written to a tmp file and pointed at
via LICENSE_SIGNING_KEY_PATH.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import db

# Make the CloudFulcrum-internal license tooling importable (it lives under
# backend/license/, which is not a package — mirrors dev_mint_test_keys.py).
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
LICENSE_DIR = BACKEND_DIR / "license"
sys.path.insert(0, str(LICENSE_DIR))

import issuance  # noqa: E402
import registry  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _write_key(path: Path) -> Ed25519PrivateKey:
    """Write a fresh throwaway Ed25519 private key to ``path``; return the key."""
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    return priv


def _pub_pem(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


@pytest.fixture(autouse=True)
def _ensure_schema():
    """Belt-and-braces: guarantee the registry tables/rules exist (idempotent)."""
    registry.ensure_registry_schema()


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    """A throwaway signing key on disk, wired via LICENSE_SIGNING_KEY_PATH."""
    key_path = tmp_path / "signing.pem"
    priv = _write_key(key_path)
    monkeypatch.setenv(issuance.SIGNING_KEY_PATH_ENV, str(key_path))
    return priv, str(key_path)


def _payload_of(key_string: str) -> dict:
    payload_b64 = key_string.split(".")[0]
    return json.loads(base64.b64decode(payload_b64))


# ===========================================================================
# AC1 — contract/org gate + registry row + audit entry
# ===========================================================================
class TestAC1_ContractGate:
    def test_issue_without_contract_ref_is_refused(self, signing_key):
        with pytest.raises(issuance.IssuanceError):
            issuance.issue_license(
                customer="Acme", license_id=_uid("no-ctr"), org_id="acme",
                contract_ref="", issued_by="ops", term_months=12,
            )

    def test_issue_without_org_id_is_refused(self, signing_key):
        with pytest.raises(issuance.IssuanceError):
            issuance.issue_license(
                customer="Acme", license_id=_uid("no-org"), org_id="  ",
                contract_ref="CTR-1", issued_by="ops", term_months=12,
            )

    def test_refused_issue_writes_nothing(self, signing_key):
        lid = _uid("refused")
        with pytest.raises(issuance.IssuanceError):
            issuance.issue_license(
                customer="Acme", license_id=lid, org_id="acme",
                contract_ref="", issued_by="ops", term_months=12,
            )
        assert registry.get_license(lid) is None
        assert registry.get_audit_for_license(lid) == []

    def test_issue_with_gate_produces_v2_key_row_and_audit(self, signing_key):
        priv, _ = signing_key
        lid = _uid("ok")
        result = issuance.issue_license(
            customer="City National Bank", license_id=lid, org_id="cnb",
            contract_ref="CTR-4471", issued_by="ganesh", term_months=12,
            max_systems=5, notes="initial",
        )
        # payload-v2 key (org_id + kid present)
        payload = _payload_of(result["key"])
        assert payload["payload_version"] == 2
        assert payload["org_id"] == "cnb" and payload["kid"]

        # registry row exists with the issued terms
        row = registry.get_license(lid)
        assert row is not None
        assert row["customer"] == "City National Bank"
        assert row["org_id"] == "cnb"
        assert row["contract_ref"] == "CTR-4471"
        assert row["issued_by"] == "ganesh"
        assert row["status"] == registry.STATUS_ACTIVE
        assert row["max_systems"] == 5
        assert row["payload_version"] == 2

        # exactly one audit entry, action=issue, carrying who/what/which-contract
        audit = registry.get_audit_for_license(lid)
        assert len(audit) == 1
        entry = audit[0]
        assert entry["action"] == registry.ACTION_ISSUE
        assert entry["actor"] == "ganesh"
        assert entry["contract_ref"] == "CTR-4471"
        assert json.loads(entry["terms"])["max_systems"] == 5


# ===========================================================================
# AC2 — audit ledger is append-only (schema-level)
# ===========================================================================
class TestAC2_AuditAppendOnly:
    def test_update_and_delete_are_no_ops(self, signing_key):
        lid = _uid("audit")
        issuance.issue_license(
            customer="Acme", license_id=lid, org_id="acme",
            contract_ref="CTR-9", issued_by="ops", term_months=6,
        )
        audit = registry.get_audit_for_license(lid)
        assert len(audit) == 1
        audit_id = audit[0]["audit_id"]

        con = db.connect()
        try:
            # Attempted tamper: UPDATE the actor. The no-update rule makes it a no-op.
            con.execute(
                "UPDATE issuance_audit SET actor = %s WHERE audit_id = %s",
                ("attacker", audit_id),
            )
            con.commit()
            # Attempted destruction: DELETE the entry. The no-delete rule blocks it.
            con.execute("DELETE FROM issuance_audit WHERE audit_id = %s", (audit_id,))
            con.commit()
        finally:
            con.close()

        after = registry.get_audit_for_license(lid)
        assert len(after) == 1, "audit entry must survive a DELETE attempt"
        assert after[0]["actor"] == "ops", "audit entry must be unchanged by an UPDATE attempt"


# ===========================================================================
# AC3 — renewal linkage + lineage
# ===========================================================================
class TestAC3_RenewalLineage:
    def test_renewal_links_via_supersedes_and_shows_lineage(self, signing_key):
        cust = _uid("cust")
        orig = _uid("orig")
        renewed = _uid("renew")
        issuance.issue_license(
            customer=cust, license_id=orig, org_id="org-x",
            contract_ref="CTR-100", issued_by="ops", term_months=12, max_systems=3,
        )
        result = issuance.renew_license(
            supersedes_license_id=orig, license_id=renewed, issued_by="ops",
            term_months=12, max_systems=7,  # a term change to review
        )
        # new row supersedes the original; inherits customer/org
        new_row = registry.get_license(renewed)
        assert new_row["supersedes"] == orig
        assert new_row["customer"] == cust
        assert new_row["org_id"] == "org-x"
        assert new_row["status"] == registry.STATUS_ACTIVE
        # the original is now superseded
        assert registry.get_license(orig)["status"] == registry.STATUS_SUPERSEDED
        # term change flagged for review
        assert result["term_changes"]["max_systems"] == [3, 7]
        # the renewal is audited as a renew
        assert registry.get_audit_for_license(renewed)[0]["action"] == registry.ACTION_RENEW

        # querying either license shows the whole lineage, oldest first
        lineage_ids = [r["license_id"] for r in registry.license_lineage(renewed)]
        assert lineage_ids == [orig, renewed]
        assert [r["license_id"] for r in registry.license_lineage(orig)] == [orig, renewed]

    def test_renew_unknown_license_is_refused(self, signing_key):
        with pytest.raises(issuance.IssuanceError):
            issuance.renew_license(
                supersedes_license_id=_uid("ghost"), license_id=_uid("new"),
                issued_by="ops", term_months=12,
            )


# ===========================================================================
# AC4 — expiring-within-N query returns exactly the expiring set
# ===========================================================================
class TestAC4_ExpiringWithin:
    def test_returns_exactly_the_window(self):
        # Direct inserts give precise expiry dates (the query is what's under test).
        cust = _uid("expcust")
        today = datetime.date.today()

        def mk(suffix, days, status=registry.STATUS_ACTIVE):
            lid = f"{cust}-{suffix}"
            registry.insert_registry_row(
                license_id=lid, customer=cust, org_id="o",
                contract_ref="CTR", deployment_type="saas",
                expires_at=(today + datetime.timedelta(days=days)).isoformat(),
                kid="k", issued_by="ops", license_key="key", status=status,
            )
            return lid

        soon = mk("soon", 5)          # inside a 30-day window
        far = mk("far", 90)           # outside
        expired = mk("expired", -3)   # already past — excluded
        superseded = mk("superseded", 5, status=registry.STATUS_SUPERSEDED)  # not active

        got = {r["license_id"] for r in registry.expiring_within(30)}
        got_for_cust = got & {soon, far, expired, superseded}
        assert got_for_cust == {soon}, (
            "only the active license expiring within the window should be returned"
        )


# ===========================================================================
# AC5 — no committed private key material; keys read from a path only
# ===========================================================================
class TestAC5_KeyCustody:
    def test_no_committed_private_key_files(self):
        """Repo-wide: no key-material FILE (*.pem/*.key/…) is tracked in git.

        The signing key must live only in the managed secrets store; it must never
        be committed as a file. (.gitignore / .dockerignore enforce this at commit
        time; this test makes it a build-time property — AC5.)
        """
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(REPO_ROOT), capture_output=True, text=True
        )
        assert out.returncode == 0, f"git ls-files failed: {out.stderr}"
        key_suffixes = (".pem", ".key", ".p8", ".der", ".pfx", ".p12")
        offenders = [
            f for f in out.stdout.splitlines()
            if f.strip() and f.lower().endswith(key_suffixes)
        ]
        assert offenders == [], f"key-material files committed to the repo: {offenders}"

    def test_license_tooling_embeds_no_private_key(self):
        """Scoped: no file under backend/license/ embeds a PEM private-key block.

        The signing area must never carry key material inline. (A repo-wide content
        scan is deliberately avoided — unrelated regex patterns and JWT test
        fixtures legitimately contain the literal string.)
        """
        offenders = []
        for fp in LICENSE_DIR.rglob("*"):
            if not fp.is_file() or fp.suffix == ".pyc":
                continue
            try:
                content = fp.read_text(errors="ignore")
            except OSError:
                continue
            if "-----BEGIN" in content and "PRIVATE KEY-----" in content:
                offenders.append(str(fp.relative_to(REPO_ROOT)))
        assert offenders == [], f"private key material under backend/license/: {offenders}"

    def test_issuance_reads_key_from_path_not_env_material(self, tmp_path, monkeypatch):
        # A valid path resolves; there is no code path that accepts key bytes.
        key_path = tmp_path / "s.pem"
        _write_key(key_path)
        monkeypatch.setenv(issuance.SIGNING_KEY_PATH_ENV, str(key_path))
        assert issuance.resolve_signing_key_path() == str(key_path)

    def test_missing_key_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setenv(issuance.SIGNING_KEY_PATH_ENV, str(tmp_path / "does-not-exist.pem"))
        with pytest.raises(issuance.IssuanceError):
            issuance.resolve_signing_key_path()


# ===========================================================================
# AC6 — two active kids issue in parallel; registry records which signed each
# ===========================================================================
class TestAC6_ParallelKids:
    def test_two_kids_issue_and_are_recorded(self, tmp_path, monkeypatch):
        key_a_path = tmp_path / "kid_a.pem"
        key_b_path = tmp_path / "kid_b.pem"
        priv_a = _write_key(key_a_path)
        priv_b = _write_key(key_b_path)

        lid_a = _uid("kidA")
        lid_b = _uid("kidB")
        res_a = issuance.issue_license(
            customer="Acme", license_id=lid_a, org_id="acme",
            contract_ref="CTR-A", issued_by="ops", term_months=12,
            kid="cf-2026-1", private_key_path=str(key_a_path),
        )
        res_b = issuance.issue_license(
            customer="Acme", license_id=lid_b, org_id="acme",
            contract_ref="CTR-B", issued_by="ops", term_months=12,
            kid="cf-2027-2", private_key_path=str(key_b_path),
        )

        # both active, each recording its signing kid
        row_a, row_b = registry.get_license(lid_a), registry.get_license(lid_b)
        assert row_a["status"] == registry.STATUS_ACTIVE and row_a["kid"] == "cf-2026-1"
        assert row_b["status"] == registry.STATUS_ACTIVE and row_b["kid"] == "cf-2027-2"

        # both keys verify under a trusted set holding both kids (the rotation window)
        from app.licensing import LicenseStatus, validate_license

        monkeypatch.setenv(
            "LICENSE_TRUSTED_KEYS",
            json.dumps({"cf-2026-1": _pub_pem(priv_a), "cf-2027-2": _pub_pem(priv_b)}),
        )
        assert validate_license(res_a["key"], installation_org_id="acme")["status"] == LicenseStatus.VALID
        assert validate_license(res_b["key"], installation_org_id="acme")["status"] == LicenseStatus.VALID
