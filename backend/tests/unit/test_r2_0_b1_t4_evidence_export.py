"""2.0-B1 T4 — Signed evidence export tests.

Covers this subtask's acceptance criterion:
  AC4 — "Export bundle verifies against its signature; altering any byte fails
        verification."

The altered-byte half is exercised exhaustively, because that is the property
an auditor relies on: a changed scalar, a changed nested value, an added or
removed list element, an added or removed key, a whitespace-only change, a
unicode homoglyph, a tampered integrity record (with and without a
recomputed hash), a swapped signature, and a byte flipped in the serialised
on-the-wire form must ALL fail.

Also covers the supporting disciplines applied before signing: the SecOps
aggregation floor refuses to sign an enumerable bundle, secret redaction runs
over exported content, and the bundle records rather than hides a partial
state (missing evidence ids, report truncation).

DB-free throughout — the run/KV/trace/pointer/license seams are monkeypatched.
"""
from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional

import pytest

from app import evidence_export as ee

REPORT_KEY = "rk-test-secret"


# ── fixtures / helpers ───────────────────────────────────────────────────────


def _run(run_id: str = "run_1") -> Dict[str, Any]:
    return {
        "id": run_id,
        "org_id": "org_a",
        "startedAt": "2026-07-01T10:00:00+00:00",
        "completedAt": "2026-07-01T10:05:00+00:00",
        "mode": "offline",
        "packId": "cloud_ops",
        "packName": "Cloud Operations Discovery",
        "packVersion": "1.2.0",
        "packIds": ["cloud_ops", "service_cloud"],
        "packVersions": {"cloud_ops": "1.2.0", "service_cloud": "3.1.0"},
        "packs": [{"packId": "cloud_ops", "packVersion": "1.2.0"}],
        "executedDetectorIds": ["RECURRING_RESOLUTION_LOOP"],
        "packExecutedAt": "2026-07-01T10:04:00+00:00",
    }


def _opp(opp_id: str = "opp_001", evidence_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "id": opp_id,
        "title": "Repetitive approval routing",
        "tier": "Quick Win",
        "impact": 4,
        "effort": 2,
        "confidence": "HIGH",
        "decision": "UNREVIEWED",
        "aiRationale": "Recurring manual routing across 3 queues.",
        "evidenceIds": evidence_ids if evidence_ids is not None else ["ev_sn_a1"],
        "packId": "cloud_ops",
        "packVersion": "1.2.0",
    }


def _evidence(ev_id: str = "ev_sn_a1") -> Dict[str, Any]:
    return {
        "id": ev_id,
        "tsLabel": "01 Jul 2026, 10:00",
        "source": "ServiceNow",
        "evidenceType": "Metric",
        "title": "Recurring resolution loop",
        "snippet": "42 incidents resolved the same way in 30 days.",
        "entities": ["queue_network_ops"],
        "confidence": "HIGH",
        "decision": "UNREVIEWED",
        "detectorId": "RECURRING_RESOLUTION_LOOP",
    }


_TRACE = {
    "opportunity_id": "opp_001",
    "run_id": "run_1",
    "hops": [
        {
            "hop_id": "finding:opp_001", "hop_type": "finding", "label": "Repetitive approval routing",
            "origin": "observed", "connector": "servicenow", "run_id": "run_1",
            "timestamp": "2026-07-01T10:05:00+00:00", "from_hop_id": None, "detail": {},
        }
    ],
    "joins": [],
    "hop_count": 1,
    "join_count": 0,
    "complete": True,
    "truncated": False,
    "retrieval_candidates": [],
    "retrieval_candidates_used_count": 0,
    "retrieval_candidates_unused_count": 0,
}

_POINTERS = [
    {
        "source_system": "servicenow", "source_artifact": "ev_sn_a1",
        "source_timestamp": "01 Jul 2026, 10:00", "origin": "observed",
        "extraction_job_id": None, "chunk_id": None, "retrieval_result_id": None,
        "detector_evidence_id": "ev_sn_a1", "confidence": None,
    }
]


@pytest.fixture
def seeded(monkeypatch):
    """Seed one run with one opportunity, one evidence record, a trace, pointers,
    plus report artifacts — and a license that yields a report_key."""
    kv: Dict[str, Any] = {
        "opps": [_opp()],
        "evidence": [_evidence()],
        "executive_report": {"confidence": "HIGH", "topQuickWins": ["opp_001"]},
        "roadmap": {"phases": [{"name": "Phase 1", "opportunityIds": ["opp_001"]}]},
        "audit": [{"id": "a1", "action": "APPROVED", "by": "analyst@example.com"}],
    }
    monkeypatch.setattr(ee.db, "get_run", lambda run_id: _run(run_id) if run_id == "run_1" else None)
    monkeypatch.setattr(
        ee.db, "run_kv_get", lambda key, run_id, default=None: kv.get(key, default)
    )
    monkeypatch.setattr("app.trace_graph.load_finding_trace", lambda r, o: _FakeTrace(_TRACE))
    monkeypatch.setattr(
        "app.evidence_pointers.get_evidence_pointers_for_opportunity",
        lambda r, o: [dict(p) for p in _POINTERS],
    )
    monkeypatch.setattr(
        "app.usage_report._resolve_license_signing",
        lambda org_id: (REPORT_KEY, "cf-2026-1", "org_a"),
    )
    return kv


class _FakeTrace:
    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._payload)


# ── bundle assembly ─────────────────────────────────────────────────────────


def test_finding_bundle_carries_trace_evidence_and_pack_versions(seeded):
    body = ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")

    assert body["scope"] == "finding"
    assert body["run_id"] == "run_1"
    assert body["opportunity_id"] == "opp_001"
    assert body["finding_count"] == 1

    prov = body["run_provenance"]
    assert prov["pack_id"] == "cloud_ops"
    assert prov["pack_version"] == "1.2.0"
    assert prov["pack_versions"] == {"cloud_ops": "1.2.0", "service_cloud": "3.1.0"}
    assert prov["executed_detector_ids"] == ["RECURRING_RESOLUTION_LOOP"]

    section = body["findings"][0]
    assert section["opportunity"]["id"] == "opp_001"
    assert section["trace"]["hop_count"] == 1
    assert [e["id"] for e in section["evidence"]] == ["ev_sn_a1"]
    assert section["evidence_pointers"][0]["source_artifact"] == "ev_sn_a1"
    assert section["missing_evidence_ids"] == []

    integrity = body["integrity"]
    assert integrity["algorithm"] == ee.INTEGRITY_ALGORITHM
    assert integrity["record_count"] == len(integrity["records"]) > 0
    assert integrity["content_root"]


def test_report_bundle_includes_run_level_artifacts(seeded):
    body = ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_REPORT)
    assert body["scope"] == "report"
    assert body["opportunity_id"] is None
    artifacts = body["report_artifacts"]
    assert artifacts["executive_report"]["confidence"] == "HIGH"
    assert artifacts["roadmap"]["phases"][0]["name"] == "Phase 1"
    assert artifacts["audit"][0]["action"] == "APPROVED"
    kinds = {r["kind"] for r in body["integrity"]["records"]}
    assert {"executive_report", "roadmap", "audit"} <= kinds


def test_missing_evidence_ids_are_reported_not_hidden(seeded):
    seeded["opps"] = [_opp(evidence_ids=["ev_sn_a1", "ev_gone"])]
    body = ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    section = body["findings"][0]
    assert [e["id"] for e in section["evidence"]] == ["ev_sn_a1"]
    assert section["missing_evidence_ids"] == ["ev_gone"]


def test_report_scope_truncation_is_reported(seeded, monkeypatch):
    monkeypatch.setattr(ee, "MAX_REPORT_FINDINGS", 2)
    seeded["opps"] = [_opp(f"opp_{i:03d}") for i in range(5)]
    body = ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_REPORT)
    assert body["finding_count"] == 2
    assert body["truncated"] is True


def test_unknown_run_and_unknown_opportunity_raise(seeded):
    with pytest.raises(ee.EvidenceExportError, match="not found"):
        ee.build_export_bundle("org_a", "run_missing", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    with pytest.raises(ee.EvidenceExportError, match="not found"):
        ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_nope")


def test_invalid_scope_and_missing_opp_id_raise(seeded):
    with pytest.raises(ee.EvidenceExportError, match="scope must be"):
        ee.build_export_bundle("org_a", "run_1", scope="everything")
    with pytest.raises(ee.EvidenceExportError, match="requires an opportunity id"):
        ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id=None)


def test_bundle_is_reproducible_for_fixed_generated_at(seeded):
    kwargs = dict(scope=ee.SCOPE_FINDING, opp_id="opp_001", generated_at="2026-07-02T00:00:00+00:00")
    a = ee.build_export_bundle("org_a", "run_1", **kwargs)
    b = ee.build_export_bundle("org_a", "run_1", **kwargs)
    assert ee.canonical_bytes(a) == ee.canonical_bytes(b)


# ── AC4: signature verifies, and ANY altered byte fails ─────────────────────


def test_ac4_signed_envelope_verifies(seeded):
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    assert envelope["algorithm"] == ee.SIGNATURE_ALGORITHM
    assert envelope["signature"]
    verdict = ee.verify_export_envelope(envelope, REPORT_KEY)
    assert verdict["verified"] is True, verdict
    assert verdict["signature_valid"] is True
    assert verdict["integrity_consistent"] is True
    assert verdict["content_root_matches"] is True


@pytest.mark.parametrize(
    "mutate,label",
    [
        (lambda b: b.update({"run_id": "run_2"}), "top-level scalar changed"),
        (lambda b: b["findings"][0]["opportunity"].update({"impact": 5}), "nested scalar changed"),
        (lambda b: b["findings"][0]["evidence"][0].update({"snippet": "43 incidents"}), "evidence text changed"),
        (lambda b: b["findings"][0]["evidence"].append(_evidence("ev_added")), "list element added"),
        (lambda b: b["findings"][0]["evidence"].clear(), "list element removed"),
        (lambda b: b["findings"][0]["opportunity"].pop("tier"), "key removed"),
        (lambda b: b["findings"][0]["opportunity"].update({"injected": True}), "key added"),
        (lambda b: b["run_provenance"].update({"pack_version": "1.2.1"}), "pack version changed"),
        (lambda b: b.update({"generated_at": "2099-01-01T00:00:00+00:00"}), "timestamp changed"),
        (lambda b: b["findings"][0]["opportunity"].update({"title": "Repetitive approval routing "}), "trailing whitespace added"),
        (lambda b: b["findings"][0]["opportunity"].update({"title": "Repetitive approvaI routing"}), "unicode homoglyph swap"),
        (lambda b: b["findings"][0]["trace"].update({"complete": False}), "trace flag flipped"),
        (lambda b: b["findings"][0]["evidence_pointers"][0].update({"origin": "inferred"}), "pointer origin changed"),
    ],
)
def test_ac4_any_altered_byte_fails_verification(seeded, mutate, label):
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    tampered = copy.deepcopy(envelope)
    mutate(tampered["bundle"])
    assert ee.canonical_bytes(tampered["bundle"]) != ee.canonical_bytes(envelope["bundle"]), (
        f"test bug: mutation '{label}' did not change the bundle bytes"
    )
    verdict = ee.verify_export_envelope(tampered, REPORT_KEY)
    assert verdict["verified"] is False, f"{label} must fail verification"
    assert verdict["signature_valid"] is False, f"{label} must fail the signature"


def test_ac4_wrong_report_key_fails_verification(seeded):
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    assert ee.verify_export_envelope(envelope, "rk-wrong")["verified"] is False


def test_ac4_stripped_or_swapped_signature_fails(seeded):
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")

    missing = copy.deepcopy(envelope)
    missing.pop("signature")
    assert ee.verify_export_envelope(missing, REPORT_KEY)["verified"] is False

    swapped = copy.deepcopy(envelope)
    swapped["signature"] = "0" * len(envelope["signature"])
    assert ee.verify_export_envelope(swapped, REPORT_KEY)["verified"] is False

    wrong_algo = copy.deepcopy(envelope)
    wrong_algo["algorithm"] = "MD5"
    verdict = ee.verify_export_envelope(wrong_algo, REPORT_KEY)
    assert verdict["verified"] is False
    assert "algorithm" in verdict["reason"]


def test_ac4_recomputing_the_integrity_hash_does_not_help_a_forger(seeded):
    """Defence in depth: the integrity block is INSIDE the signed body, so an
    attacker who edits a record AND fixes its content_hash/root still fails the
    signature. The integrity block localises tampering; it is not the only gate."""
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    forged = copy.deepcopy(envelope)
    forged["bundle"]["findings"][0]["evidence"][0]["snippet"] = "1 incident (understated)"

    records = [
        {"kind": "run_provenance", "record_id": "run_1", "content": forged["bundle"]["run_provenance"]},
    ]
    section = forged["bundle"]["findings"][0]
    records.append({"kind": "opportunity", "record_id": "opp_001", "content": section["opportunity"]})
    records.append({"kind": "trace", "record_id": "opp_001", "content": section["trace"]})
    records.append({"kind": "evidence_pointers", "record_id": "opp_001", "content": section["evidence_pointers"]})
    for ev in section["evidence"]:
        records.append({"kind": "evidence", "record_id": ev["id"], "content": ev})
    forged["bundle"]["integrity"] = ee.build_integrity_block(records)

    verdict = ee.verify_export_envelope(forged, REPORT_KEY)
    # The forger made the block self-consistent, but the signature still fails.
    assert verdict["integrity_consistent"] is True
    assert verdict["signature_valid"] is False
    assert verdict["verified"] is False


def test_ac4_tampered_integrity_record_is_localised(seeded):
    """Editing a record's content_hash without re-folding breaks the chain, so a
    verifier can see WHICH record is inconsistent, not merely that something is."""
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    tampered = copy.deepcopy(envelope)
    tampered["bundle"]["integrity"]["records"][0]["content_hash"] = "f" * 64
    verdict = ee.verify_export_envelope(tampered, REPORT_KEY)
    assert verdict["integrity_consistent"] is False
    assert verdict["verified"] is False


def test_ac4_altered_byte_in_the_serialised_wire_form_fails(seeded):
    """The on-disk/on-the-wire form is what a third party actually verifies."""
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    raw = ee.envelope_bytes(envelope)
    assert ee.verify_export_bytes(raw, REPORT_KEY)["verified"] is True

    flipped = raw.replace(b"42 incidents", b"41 incidents")
    assert flipped != raw
    assert ee.verify_export_bytes(flipped, REPORT_KEY)["verified"] is False

    assert ee.verify_export_bytes(raw[:-1], REPORT_KEY)["verified"] is False   # truncated
    assert ee.verify_export_bytes(b"not json", REPORT_KEY)["verified"] is False


def test_ac4_verification_never_raises_on_malformed_input():
    for bad in (None, "string", 42, [], {}, {"bundle": "not-an-object"}, {"bundle": {}}):
        verdict = ee.verify_export_envelope(bad, REPORT_KEY)
        assert verdict["verified"] is False
        assert verdict["reason"]


def test_signature_is_deterministic_and_key_order_independent(seeded):
    kwargs = dict(scope=ee.SCOPE_FINDING, opp_id="opp_001", generated_at="2026-07-02T00:00:00+00:00")
    first = ee.generate_signed_export("org_a", "run_1", **kwargs)
    second = ee.generate_signed_export("org_a", "run_1", **kwargs)
    assert first["signature"] == second["signature"]

    # Re-serialising through JSON (which loses dict ordering) must still verify.
    roundtripped = json.loads(json.dumps(first))
    assert ee.verify_export_envelope(roundtripped, REPORT_KEY)["verified"] is True


def test_export_without_a_license_report_key_is_refused_not_unsigned(seeded, monkeypatch):
    from app.usage_report import UsageReportError

    def _no_key(org_id):
        raise UsageReportError("the installed license carries no report_key")

    monkeypatch.setattr("app.usage_report._resolve_license_signing", _no_key)
    with pytest.raises(ee.EvidenceExportError, match="report_key"):
        ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")


# ── integrity block primitives ──────────────────────────────────────────────


def test_integrity_block_is_order_independent():
    records = [
        {"kind": "evidence", "record_id": "b", "content": {"x": 1}},
        {"kind": "evidence", "record_id": "a", "content": {"y": 2}},
    ]
    forward = ee.build_integrity_block(records)
    backward = ee.build_integrity_block(list(reversed(records)))
    assert forward == backward
    assert [r["record_id"] for r in forward["records"]] == ["a", "b"]


def test_record_hash_binds_kind_id_and_content():
    base = ee.record_hash("evidence", "e1", {"a": 1})
    assert base == ee.record_hash("evidence", "e1", {"a": 1})
    assert base != ee.record_hash("opportunity", "e1", {"a": 1})   # kind matters
    assert base != ee.record_hash("evidence", "e2", {"a": 1})      # id matters
    assert base != ee.record_hash("evidence", "e1", {"a": 2})      # content matters


def test_empty_integrity_block_is_well_formed():
    block = ee.build_integrity_block([])
    assert block["record_count"] == 0
    assert block["records"] == []
    assert block["content_root"] == ""


# ── content discipline applied before signing ───────────────────────────────


def test_aggregation_floor_violation_refuses_the_export(seeded):
    """A bundle that would enumerate host x vulnerability pairs must never be
    signed — it is refused loudly, not emitted with a caveat."""
    seeded["evidence"] = [
        {**_evidence(), "snippet": "Host 10.1.2.3 is affected by CVE-2026-1234."}
    ]
    with pytest.raises(ee.EvidenceExportError, match="aggregation floor"):
        ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")


def test_audit_actor_identity_does_not_block_the_export(seeded):
    """The decision audit's ``by`` actor is the point of an audit trail and an
    auditor requires it, so it is deliberately outside the aggregation-floor
    sweep — a reviewed run must stay exportable, with its attestation provenance
    intact. (The floor still covers findings/evidence and the narrative
    artifacts, proven by the host x CVE test above.)"""
    seeded["audit"] = [
        {"id": "a1", "action": "APPROVED", "by": "analyst@example.com", "evidenceId": "ev_sn_a1"}
    ]
    body = ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_REPORT)
    assert body["report_artifacts"]["audit"][0]["by"] == "analyst@example.com"

    # ...but the floor still bites on the enumeration-capable surfaces alongside it.
    seeded["evidence"] = [
        {**_evidence(), "snippet": "Host 10.1.2.3 is affected by CVE-2026-1234."}
    ]
    with pytest.raises(ee.EvidenceExportError, match="aggregation floor"):
        ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_REPORT)


def test_secrets_are_redacted_before_signing(seeded):
    seeded["evidence"] = [
        {**_evidence(), "snippet": "42 incidents; token ghp_abcdefghijklmnopqrstuvwxyz0123456789"}
    ]
    body = ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    snippet = body["findings"][0]["evidence"][0]["snippet"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in snippet
    assert "REDACTED" in snippet
    assert body["redacted_pattern_types"]
    # What was signed is what is exported — the redacted form verifies.
    envelope = {
        "bundle": body,
        "signature": ee.sign_report_body(body, REPORT_KEY),
        "algorithm": ee.SIGNATURE_ALGORITHM,
    }
    assert ee.verify_export_envelope(envelope, REPORT_KEY)["verified"] is True


def _load_standalone_verifier():
    """Import the dependency-free auditor CLI by path (it is not a package)."""
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "verify_evidence_export.py"
    spec = importlib.util.spec_from_file_location("verify_evidence_export", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_standalone_auditor_verifier_agrees_with_the_product_signer(seeded):
    """The hand-out CLI reimplements canonicalisation + HMAC with only the stdlib
    so an auditor can read it. This pins it against the product's own signer so
    the two can never drift into disagreeing about a bundle."""
    cli = _load_standalone_verifier()
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")

    # Identical canonical bytes and identical signature.
    assert cli.canonical_bytes(envelope["bundle"]) == ee.canonical_bytes(envelope["bundle"])
    assert cli.expected_signature(envelope["bundle"], REPORT_KEY) == envelope["signature"]

    assert cli.verify(envelope, REPORT_KEY)["verified"] is True
    assert cli.verify(envelope, "rk-wrong")["verified"] is False

    tampered = copy.deepcopy(envelope)
    tampered["bundle"]["findings"][0]["evidence"][0]["snippet"] = "1 incident"
    cli_verdict = cli.verify(tampered, REPORT_KEY)
    product_verdict = ee.verify_export_envelope(tampered, REPORT_KEY)
    assert cli_verdict["verified"] is False
    assert product_verdict["verified"] is False
    assert cli_verdict["verified"] == product_verdict["verified"]


def test_standalone_verifier_localises_a_broken_integrity_chain(seeded):
    cli = _load_standalone_verifier()
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    tampered = copy.deepcopy(envelope)
    tampered["bundle"]["integrity"]["records"][0]["content_hash"] = "e" * 64
    verdict = cli.verify(tampered, REPORT_KEY)
    assert verdict["verified"] is False
    assert any("integrity chain breaks at record" in p for p in verdict["problems"])


def test_bundle_fingerprint_carries_no_content_and_no_full_signature(seeded):
    envelope = ee.generate_signed_export("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_001")
    fp = ee.bundle_fingerprint(envelope)
    assert fp["scope"] == "finding"
    assert fp["run_id"] == "run_1"
    assert fp["content_root"] == envelope["bundle"]["integrity"]["content_root"]
    assert fp["signature_prefix"] == envelope["signature"][:16]
    assert fp["signature_prefix"] != envelope["signature"]
    serialised = json.dumps(fp)
    assert "42 incidents" not in serialised          # no evidence content
    assert envelope["signature"] not in serialised   # never the whole MAC
