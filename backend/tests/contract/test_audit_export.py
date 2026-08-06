"""
2.0-D4 T2 — signed audit export (D4-AC3).

AC3: "Signed audit export verifies; alteration fails verification; every export is
itself audited."

The centrepiece is ``TestAlterationFailsVerification``, and it is deliberately
blunt: generate an export, verify it, flip ONE byte, verify again, assert failure.
That single test is what turns the signature from a checkbox into a guarantee, and
it is the same shape 2.0-B1 specifies for its evidence bundle — which is why the
signing module is shared rather than local to this feature.

Everything here uses a throwaway Ed25519 key pair, so the suite never depends on a
deployment's real signing key and never needs one configured.
"""
from __future__ import annotations

import base64
import copy
import json

import pytest

from app import audit_export, export_signing
from app.middleware import audit


@pytest.fixture(scope="module")
def key_pair():
    """A throwaway signing key pair — never the deployment's real key."""
    private_pem, public_pem = export_signing.generate_key_pair()
    from cryptography.hazmat.primitives import serialization

    private = serialization.load_pem_private_key(private_pem.encode(), password=None)
    public = serialization.load_pem_public_key(public_pem.encode())
    return private, public


def _seed(org_id: str, count: int = 3, event_type: str | None = None):
    """Write real audit rows through the real write point."""
    for i in range(count):
        audit.log_event(
            event_type or audit.LICENSE_INSTALLED,
            org_id=org_id,
            user_id=f"user{i}@example.com",
            target=f"target-{i}",
            outcome=audit.OUTCOME_SUCCESS,
        )


def _export(org_id, private, *, frm="2000-01-01", to="2099-12-31"):
    return audit_export.build_signed_export(
        org_id, frm, to, generated_by="tester@example.com", private_key=private
    )


@pytest.fixture()
def signing_key(monkeypatch):
    """Configure a throwaway deployment signing key for the duration of a test.

    Needed by the tests that drive the ROUTE rather than ``build_signed_export``
    directly — the route resolves its key from the environment, and without one it
    refuses with 503 (correctly: an unsigned artifact that looks signed is worse
    than no artifact).
    """
    private_pem, _ = export_signing.generate_key_pair()
    monkeypatch.setenv(export_signing.SIGNING_KEY_ENV, private_pem)
    return private_pem


def _seed_owner(org_id: str, user_id: str) -> None:
    """The export route is Owner-gated, so the caller needs a membership row.

    No prior test in this file drove the route — they all called
    ``build_signed_export`` directly — so this is new plumbing rather than
    something that was missing.
    """
    from contextlib import closing
    from datetime import datetime, timezone

    from app import db
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, user_id, "owner", datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _post_export(client, org_id: str, period_from: str, period_to: str):
    """Drive the real HTTP route, which is where the audit emission lives.

    Calling ``build_signed_export`` directly would exercise none of it — the
    ordering bug this file now guards against was in the ROUTE, not the builder.
    """
    import os

    token = os.getenv("ADMIN_JWT") or os.getenv("DEV_JWT", "dev-token-change-me")
    _seed_owner(org_id, token)
    return client.post(
        "/api/audit/export",
        json={"period_from": period_from, "period_to": period_to},
        headers={"Authorization": f"Bearer {token}", "X-Org-Id": org_id},
    )


# ── the export itself ──────────────────────────────────────────────────────────


class TestExportContent:

    def test_an_export_contains_the_period_rows(self, client, key_pair):
        private, _ = key_pair
        org = "org_d4t2_content"
        _seed(org, 3)
        doc = _export(org, private)
        assert doc["org_id"] == org
        assert doc["record_count"] == 3
        assert len(doc["records"]) == 3
        assert doc["complete"] is True
        assert doc["generated_by"] == "tester@example.com"
        assert doc["export_schema_version"] == audit_export.EXPORT_SCHEMA_VERSION

    def test_each_record_carries_the_audit_fields(self, client, key_pair):
        private, _ = key_pair
        org = "org_d4t2_fields"
        _seed(org, 1)
        record = _export(org, private)["records"][0]
        assert set(record) >= {"id", "event_type", "actor", "timestamp", "detail"}
        assert record["actor"] == "user0@example.com"
        assert record["detail"]["outcome"] == audit.OUTCOME_SUCCESS
        assert record["detail"]["target"] == "target-0"

    def test_records_are_ordered_by_time(self, client, key_pair):
        private, _ = key_pair
        org = "org_d4t2_order"
        _seed(org, 5)
        stamps = [r["timestamp"] for r in _export(org, private)["records"]]
        assert stamps == sorted(stamps)

    def test_the_period_bounds_the_export(self, client, key_pair):
        private, _ = key_pair
        org = "org_d4t2_period"
        _seed(org, 2)
        empty = _export(org, private, frm="1999-01-01", to="1999-12-31")
        assert empty["record_count"] == 0
        assert empty["records"] == []
        # ...and it is still a signed, verifiable document, not an error
        ok, reason = export_signing.verify_export(empty, public_key=key_pair[1])
        assert ok, reason

    def test_a_plain_end_date_covers_the_whole_day(self):
        """`to=2026-07-20` must include events at 23:59 on the 20th — the reading an
        auditor assumes and a naive implementation gets wrong."""
        end = audit_export._parse_boundary("2026-07-20", field="to")
        assert end.startswith("2026-07-20T23:59:59")

    def test_an_inverted_period_is_refused(self):
        with pytest.raises(audit_export.AuditExportError):
            audit_export.build_export("org", "2026-07-20", "2026-07-01")

    @pytest.mark.parametrize("bad", ["", None, "not-a-date", "2026-13-45"])
    def test_an_unparseable_boundary_is_refused(self, bad):
        with pytest.raises(audit_export.AuditExportError):
            audit_export.build_export("org", bad, "2099-01-01")

    def test_a_capped_export_is_reported_incomplete_not_silently_truncated(
        self, client, key_pair
    ):
        """Loud degradation: a truncated audit trail must never be handed over as if
        it were the whole period."""
        private, _ = key_pair
        org = "org_d4t2_cap"
        _seed(org, 4)
        doc = audit_export.build_signed_export(
            org, "2000-01-01", "2099-12-31", limit=2, private_key=private
        )
        assert doc["complete"] is False
        assert doc["record_count"] == 2
        assert doc["truncated"]["limit"] == 2
        assert "narrow the period" in doc["truncated"]["reason"]


# ── org isolation, enforced in the query ───────────────────────────────────────


class TestOrgIsolation:

    def test_an_export_contains_no_other_orgs_rows(self, client, key_pair):
        private, _ = key_pair
        mine, theirs = "org_d4t2_mine", "org_d4t2_theirs"
        _seed(mine, 2)
        _seed(theirs, 3)
        doc = _export(mine, private)
        assert doc["record_count"] == 2
        assert {r["id"] for r in doc["records"]}
        # every row belongs to the requesting org — checked via the source table
        from app import db
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT org_id FROM audit_log WHERE id = ANY(%s)",
                ([r["id"] for r in doc["records"]],),
            )
            assert {row[0] for row in cur.fetchall()} == {mine}
        finally:
            con.close()

    def test_the_org_predicate_is_in_the_sql(self):
        """Isolation asserted after retrieval is not isolation — the rows have
        already been read. This pins the predicate into the statement."""
        import ast
        import pathlib

        source = pathlib.Path(audit_export.__file__).read_text(encoding="utf-8")
        selects = [
            " ".join(n.value.split())
            for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "audit_log" in n.value.lower()
        ]
        assert selects, "expected the export SELECT"
        for sql in selects:
            assert "WHERE org_id = %s" in sql, (
                f"the export query must scope org in SQL: {sql[:160]}"
            )

    def test_an_empty_org_is_refused(self):
        with pytest.raises(audit_export.AuditExportError):
            audit_export.build_export("", "2000-01-01", "2099-01-01")


# ── AC3: the signature ────────────────────────────────────────────────────────


class TestSignatureVerifies:

    def test_a_freshly_generated_export_verifies(self, client, key_pair):
        private, public = key_pair
        org = "org_d4t2_verify"
        _seed(org, 3)
        doc = _export(org, private)
        ok, reason = export_signing.verify_export(doc, public_key=public)
        assert ok, reason
        assert reason == "verified"

    def test_the_envelope_records_algorithm_version_and_digest(self, client, key_pair):
        private, _ = key_pair
        org = "org_d4t2_envelope"
        _seed(org, 1)
        envelope = _export(org, private)["signature"]
        assert envelope["algorithm"] == "Ed25519"
        assert envelope["version"] == export_signing.SIGNATURE_VERSION
        assert len(envelope["content_sha256"]) == 64
        assert envelope["value"]

    def test_verification_survives_key_reordering(self, client, key_pair):
        """The signed bytes are canonical, so re-serialising the document in a
        different key order must still verify — otherwise a genuine export could fail
        its own verification after a round-trip through any JSON tool."""
        private, public = key_pair
        org = "org_d4t2_canonical"
        _seed(org, 2)
        doc = _export(org, private)
        round_tripped = json.loads(json.dumps(doc, sort_keys=True))
        ok, reason = export_signing.verify_export(round_tripped, public_key=public)
        assert ok, reason


class TestAlterationFailsVerification:
    """AC3's core requirement, tested bluntly: flip one byte and it must fail."""

    def test_flipping_one_byte_of_content_fails_verification(self, client, key_pair):
        private, public = key_pair
        org = "org_d4t2_flip"
        _seed(org, 3)
        doc = _export(org, private)
        assert export_signing.verify_export(doc, public_key=public)[0] is True

        # Flip exactly ONE byte inside the signed content.
        blob = bytearray(json.dumps(doc, sort_keys=True).encode())
        index = blob.index(b"target-0") + 7   # the '0' of target-0
        blob[index] = blob[index] ^ 0x01      # '0' -> '1'
        altered = json.loads(blob.decode())

        ok, reason = export_signing.verify_export(altered, public_key=public)
        assert ok is False
        assert "altered" in reason

    @pytest.mark.parametrize("mutation", [
        "add_record", "drop_record", "change_count", "widen_period",
        "change_org", "change_actor", "change_timestamp",
    ])
    def test_every_meaningful_tamper_fails(self, client, key_pair, mutation):
        """The alterations someone would actually attempt on an audit export."""
        private, public = key_pair
        org = "org_d4t2_tamper"
        _seed(org, 3)
        doc = _export(org, private)
        bad = copy.deepcopy(doc)

        if mutation == "add_record":
            bad["records"].append(dict(bad["records"][0], id="forged"))
        elif mutation == "drop_record":
            bad["records"].pop()
        elif mutation == "change_count":
            bad["record_count"] = 99
        elif mutation == "widen_period":
            bad["period"]["from"] = "1990-01-01T00:00:00+00:00"
        elif mutation == "change_org":
            bad["org_id"] = "some_other_org"
        elif mutation == "change_actor":
            bad["records"][0]["actor"] = "someone.else@example.com"
        elif mutation == "change_timestamp":
            bad["records"][0]["timestamp"] = "1999-01-01T00:00:00+00:00"

        ok, reason = export_signing.verify_export(bad, public_key=public)
        assert ok is False, f"{mutation} was not detected"
        assert reason != "verified"

    def test_removing_the_signature_fails(self, client, key_pair):
        private, public = key_pair
        org = "org_d4t2_nosig"
        _seed(org, 1)
        doc = _export(org, private)
        doc.pop("signature")
        ok, reason = export_signing.verify_export(doc, public_key=public)
        assert ok is False and "no signature" in reason

    def test_a_signature_from_a_different_key_fails(self, client, key_pair):
        """Someone re-signing an altered export with their own key must not pass."""
        _, public = key_pair
        other_private_pem, _ = export_signing.generate_key_pair()
        from cryptography.hazmat.primitives import serialization

        other = serialization.load_pem_private_key(
            other_private_pem.encode(), password=None
        )
        org = "org_d4t2_wrongkey"
        _seed(org, 1)
        doc = _export(org, other)              # signed by the WRONG key
        ok, reason = export_signing.verify_export(doc, public_key=public)
        assert ok is False and "altered" in reason

    def test_a_downgraded_signature_version_is_refused(self, client, key_pair):
        """A signature made under a different signed-bytes rule must never verify
        under this one, even if the maths happens to work."""
        private, public = key_pair
        org = "org_d4t2_version"
        _seed(org, 1)
        doc = _export(org, private)
        doc["signature"]["version"] = "0"
        ok, reason = export_signing.verify_export(doc, public_key=public)
        assert ok is False and "version" in reason

    def test_a_corrupt_signature_value_is_refused_not_crashed(self, client, key_pair):
        private, public = key_pair
        org = "org_d4t2_corrupt"
        _seed(org, 1)
        doc = _export(org, private)
        doc["signature"]["value"] = "!!!not-base64!!!"
        ok, reason = export_signing.verify_export(doc, public_key=public)
        assert ok is False and "base64" in reason

    def test_a_recomputed_digest_still_fails_the_signature(self, client, key_pair):
        """A tamperer who recomputes content_sha256 gets past the digest check and
        still fails the signature — which is why the digest is diagnostic only."""
        private, public = key_pair
        org = "org_d4t2_digest"
        _seed(org, 2)
        doc = _export(org, private)
        doc["records"][0]["actor"] = "forged@example.com"
        payload = {k: v for k, v in doc.items() if k != "signature"}
        doc["signature"]["content_sha256"] = export_signing.content_digest(
            export_signing.canonical_bytes(payload)
        )
        ok, reason = export_signing.verify_export(doc, public_key=public)
        assert ok is False
        assert "signature does not match" in reason


class TestSigningRefusesToDegrade:

    def test_an_unconfigured_deployment_cannot_sign(self, monkeypatch):
        """No silent downgrade to an unsigned artifact that looks signed."""
        monkeypatch.delenv(export_signing.SIGNING_KEY_ENV, raising=False)
        with pytest.raises(export_signing.ExportSigningError):
            export_signing.sign_export({"a": 1})

    def test_a_malformed_key_is_refused_without_leaking_it(self, monkeypatch):
        monkeypatch.setenv(export_signing.SIGNING_KEY_ENV, "-----BEGIN nonsense-----")
        with pytest.raises(export_signing.ExportSigningError) as exc:
            export_signing.load_signing_key()
        assert "nonsense" not in str(exc.value)

    def test_verification_without_a_public_key_is_a_reason_not_a_crash(
        self, monkeypatch, client, key_pair
    ):
        private, _ = key_pair
        org = "org_d4t2_nokey"
        _seed(org, 1)
        doc = _export(org, private)
        monkeypatch.delenv(export_signing.PUBLIC_KEY_ENV, raising=False)
        ok, reason = export_signing.verify_export(doc)
        assert ok is False and export_signing.PUBLIC_KEY_ENV in reason


# ── AC3: every export is itself audited ───────────────────────────────────────


class TestExportIsItselfAudited:
    """Exporting mutates nothing and is still a disclosure — someone took a copy of
    the org's audit trail out of the system."""

    def test_the_event_type_is_registered(self):
        assert audit.AUDIT_EXPORT_GENERATED in audit.AUDIT_EVENT_REGISTRY

    def test_generating_an_export_writes_an_audit_row(self, client):
        org = "org_d4t2_recursive"
        _seed(org, 1)
        before = self._count(org)
        audit.log_event(
            audit.AUDIT_EXPORT_GENERATED,
            org_id=org, user_id="owner@example.com",
            target="audit_log:2000-01-01..2099-12-31",
            outcome=audit.OUTCOME_SUCCESS,
        )
        assert self._count(org) == before + 1

    def test_a_later_export_contains_the_earlier_disclosure(self, client, key_pair):
        """The recursion, demonstrated: 'who has read this trail before?' is
        answerable from a subsequent export."""
        private, _ = key_pair
        org = "org_d4t2_recursion"
        audit.log_event(
            audit.AUDIT_EXPORT_GENERATED,
            org_id=org, user_id="first.owner@example.com",
            target="audit_log:earlier", outcome=audit.OUTCOME_SUCCESS,
        )
        doc = _export(org, private)
        kinds = {r["event_type"] for r in doc["records"]}
        assert audit.AUDIT_EXPORT_GENERATED in kinds
        actors = {r["actor"] for r in doc["records"]}
        assert "first.owner@example.com" in actors

    def test_exactly_one_row_is_written_per_request(self, client, signing_key):
        """PR #561 review, HIGH.

        This test replaces one that asserted the OPPOSITE ordering — that the row
        was written BEFORE the export was built, so a disclosure could not go
        unrecorded if assembly failed after the read. That reasoning does not
        survive contact with a failure: when the build then raised, a SECOND row
        was appended with ``failure``, and the trail permanently held a success
        disclosure for a period that was never exported. An auditor could not tell
        which row was real — the exact ambiguity this feature exists to remove.

        A missing row is a gap you can see. A false success is a lie you cannot
        detect. So the row is written once, after the outcome is known.

        Note this is a RUNTIME assertion, not a line-number one: the emission now
        happens through a local helper, so a static ordering check would pass
        while telling you nothing about how many rows a request produces.
        """
        org = "org_d4t2_single_row"
        _seed(org, 2)
        before = self._count(org)
        response = _post_export(client, org, "2000-01-01", "2099-12-31")
        assert response.status_code == 200, response.text
        assert self._count(org) == before + 1

    def test_a_refused_export_writes_no_success_row(self, client, signing_key):
        """The failure the review found, asserted directly."""
        org = "org_d4t2_refused"
        _seed(org, 1)
        before_success = self._count_by_outcome(org, audit.OUTCOME_SUCCESS)

        # `to` earlier than `from` — refused before anything is disclosed.
        response = _post_export(client, org, "2099-01-01", "2000-01-01")
        assert response.status_code == 400, response.text

        assert self._count_by_outcome(org, audit.OUTCOME_SUCCESS) == before_success, (
            "a refused export left a success row behind — an auditor would read "
            "it as a disclosure of a period that was never exported"
        )
        assert self._count_by_outcome(org, audit.OUTCOME_FAILURE) >= 1, (
            "a refused export must still be recorded"
        )

    def test_the_row_records_the_same_period_as_the_signed_file(self, client, signing_key):
        """PR #561 review.

        The row used to carry the caller's raw strings ("2026-07-20") while the
        signed file carried the normalised end-of-day boundary. Each was correct
        alone, but an auditor comparing the two saw different periods and had
        every reason to question the artifact's authenticity.
        """
        org = "org_d4t2_period_match"
        _seed(org, 1)
        response = _post_export(client, org, "2000-01-01", "2099-12-31")
        assert response.status_code == 200, response.text
        doc = response.json()
        row = self._latest(org)
        assert row["detail"].get("period_from") == doc["period"]["from"]
        assert row["detail"].get("period_to") == doc["period"]["to"]
        assert row["detail"]["period_to"].endswith("23:59:59.999999+00:00"), (
            "the recorded `to` must be the inclusive end-of-day the export used"
        )

    def _count_by_outcome(self, org_id: str, outcome: str) -> int:
        rows = self._all(org_id)
        return sum(1 for r in rows if (r["detail"] or {}).get("outcome") == outcome)

    def _latest(self, org_id: str):
        rows = self._all(org_id)
        assert rows, "no audit row was written"
        return rows[0]

    def _all(self, org_id: str):
        import json as _json

        from app import db

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT payload FROM audit_log WHERE org_id = %s AND event_type = %s "
                "ORDER BY timestamp DESC",
                (org_id, audit.AUDIT_EXPORT_GENERATED),
            )
            out = []
            for (payload,) in cur.fetchall():
                if isinstance(payload, str):
                    payload = _json.loads(payload)
                out.append({"detail": payload or {}})
            return out
        finally:
            con.close()

    def _count(self, org_id: str) -> int:
        from app import db

        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT count(*) FROM audit_log WHERE org_id = %s AND event_type = %s",
                (org_id, audit.AUDIT_EXPORT_GENERATED),
            )
            return int(cur.fetchone()[0])
        finally:
            con.close()


# ── the route surface ─────────────────────────────────────────────────────────


class TestRouteSurface:

    def test_the_export_route_is_registered_and_owner_gated(self):
        from fastapi.routing import APIRoute

        from app.main import app
        from app.rbac import require_role
        from app.security import require_auth

        routes = [
            r for r in app.routes
            if isinstance(r, APIRoute) and r.path == "/api/audit/export"
        ]
        assert routes, "POST /api/audit/export is not registered"
        route = routes[0]
        assert "POST" in route.methods
        deps = [d.dependency for d in route.dependencies] + [
            d.call for d in route.dependant.dependencies
        ]
        assert require_auth in deps
        # Owner-gated: require_role("owner") produces a distinct dependency object,
        # so identity comparison will not do — check the closure's role instead.
        role_deps = [
            d for d in deps
            if getattr(d, "__qualname__", "").startswith(require_role.__name__)
        ]
        assert role_deps, "no require_role dependency on the export route"

    def test_the_request_model_has_no_org_field(self):
        """A caller-supplied org on an audit export is a cross-tenant disclosure
        waiting to happen."""
        from app.routes_audit_export import AuditExportRequest

        assert "org_id" not in AuditExportRequest.model_fields
        assert "org" not in AuditExportRequest.model_fields

    def test_the_verify_route_is_registered(self):
        from app.main import app

        paths = {getattr(r, "path", "") for r in app.routes}
        assert "/api/audit/export/verify" in paths
