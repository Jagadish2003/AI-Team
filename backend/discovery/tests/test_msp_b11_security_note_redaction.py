"""MSP-B11 T5 / AT-700 (AC5) — security-note redaction-before-indexing.

Seeded IOCs and credentials in security work notes must never reach retrievable
content; the note stays reachable only via an access-controlled evidence pointer;
org scoping is preserved; and B11 reuses the single R18-A2 redaction foundation,
extended with the security IOC/artefact pattern set (no second mechanism).

Offline; no ServiceNow credentials required.
"""
from __future__ import annotations

import json
import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from discovery.ingest.secret_redaction import (  # noqa: E402
    SECURITY_PATTERN_TYPES,
    scan_and_redact,
    scan_and_redact_security,
)
from discovery.ingest.servicenow_security_notes_handoff import (  # noqa: E402
    SECURITY_NOTE_CONNECTOR_ID,
    SECURITY_NOTE_CONTENT_TYPE,
    SECURITY_NOTE_SOURCE_SYSTEM,
    SECURITY_NOTE_SOURCE_TYPE,
    build_security_note_artifact,
    ingest_security_notes,
)

# Representative seeded material of every kind the ticket names: credentials,
# access tokens, private-key material, IOC formats, and artefact fragments.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_GH_TOKEN = "ghp_" + "b" * 36
_PASSWORD_ASSIGN = "password=Sup3rSecretValue"
_CONN_STRING = "Server=db;Uid=admin;Password=Hunter2Hunter2;"
_BEARER = "Authorization: Bearer abcdef0123456789ABCDEFxyz"
_IPV4 = "203.0.113.9"
_DEFANGED_IP = "10[.]0[.]0[.]5"
_DEFANGED_URL = "hxxps://evil.example.com/beacon"
_DEFANGED_DOMAIN = "malware(.)example(.)net"
_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_MD5 = "44d88612fea8a8f36de82e1278abb02f"
_MAC = "00:1A:2B:3C:4D:5E"
_IPV6 = "2001:0db8:85a3:0000:0000:8a2e:0370:7334"
_EMAIL_IOC = "phisher@bad-domain.example"
_URL_CREDS = "https://svc:hunter2@internal.host/api"
_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKxxxxSEEDEDxxxx\n"
    "-----END RSA PRIVATE KEY-----"
)

# Distinctive fragments that MUST NOT survive into any retrievable output.
_LEAK_FRAGMENTS = (
    "AKIAIOSFODNN7EXAMPLE", "ghp_bbb", "Sup3rSecretValue", "Hunter2Hunter2",
    "abcdef0123456789", "203.0.113.9", "10[.]0[.]0[.]5", "evil.example.com",
    "malware(.)example", "e3b0c44298fc", "44d88612fea8", "00:1A:2B:3C:4D:5E",
    "2001:0db8:85a3", "phisher@bad-domain", "svc:hunter2@internal",
    "MIIBOgIBAAJBAKxxxxSEEDEDxxxx",
)

_ALL_SEEDED_NOTE = (
    f"Triage: beacon to {_IPV4} (defanged {_DEFANGED_IP}); C2 {_DEFANGED_URL}; "
    f"lookalike {_DEFANGED_DOMAIN}. Sample sha256 {_SHA256}, md5 {_MD5}. "
    f"Host {_MAC} / {_IPV6}. Phishing sender {_EMAIL_IOC}. "
    f"Pulled creds {_PASSWORD_ASSIGN}; conn {_CONN_STRING}; {_BEARER}. "
    f"Backup URL {_URL_CREDS}. Cloud key {_AWS_KEY}, token {_GH_TOKEN}. "
    f"Recovered key {_PRIVATE_KEY}. Followed runbook KB0010234."
)


def _incident(note, *, sys_id="sir-0001", number="SIR0010001",
              category="Malicious code activity", note_field="work_notes"):
    return {
        "sys_id": sys_id,
        "number": number,
        "category": category,
        "state": "Contain",
        "sys_updated_on": "2026-07-01 09:30:00",
        "source_url": f"https://acme.service-now.com/sn_si_incident.do?sys_id={sys_id}",
        note_field: note,
        "evidence": {
            "source_system": "servicenow",
            "source_artifact": sys_id,
            "origin": "observed",
            "source_artifact_type": "record_id",
            "source_url": f"https://acme.service-now.com/sn_si_incident.do?sys_id={sys_id}",
        },
    }


class _CapturingIngest:
    """Fake substrate entry point — records exactly what it was handed."""

    def __init__(self):
        self.calls = []

    def __call__(self, org_id, artifacts):
        self.calls.append((org_id, list(artifacts)))
        return {"org_id": org_id, "artifacts": len(artifacts)}


# ─────────────────────────────────────────────────────────────────────────────
# AC5 — seeded IOCs and credentials never reach retrievable content
# ─────────────────────────────────────────────────────────────────────────────


class TestSeededMaterialNeverIndexed:
    def test_every_seeded_value_absent_from_handed_off_content(self):
        ingest = _CapturingIngest()
        ingest_security_notes(
            "org-a", [_incident(_ALL_SEEDED_NOTE)],
            ingest_fn=ingest, record_event_fn=lambda *a: None,
        )
        content = ingest.calls[0][1][0].content
        for fragment in _LEAK_FRAGMENTS:
            assert fragment not in content, f"seeded value leaked into retrieval: {fragment}"
        assert "[REDACTED:" in content

    @pytest.mark.parametrize(
        "note_field", ["work_notes", "comments", "additional_comments", "close_notes"]
    )
    def test_secret_redacted_from_every_note_field(self, note_field):
        ingest = _CapturingIngest()
        ingest_security_notes(
            "org-a",
            [_incident(f"IP {_IPV4} key {_AWS_KEY}", note_field=note_field)],
            ingest_fn=ingest,
        )
        content = ingest.calls[0][1][0].content
        assert _IPV4 not in content
        assert _AWS_KEY not in content

    def test_retrieval_chunks_are_clean_end_to_end(self):
        """The sanitized text is what actually gets chunked for the index."""
        from app.retrieval.ingest import build_records

        built = build_security_note_artifact(_incident(_ALL_SEEDED_NOTE))
        assert built is not None
        artifact, outcome, _prov = built
        assert outcome.redacted
        records = build_records("org-a", artifact)
        blob = json.dumps([r.content for r in records])
        for fragment in _LEAK_FRAGMENTS:
            assert fragment not in blob, f"leaked into chunk: {fragment}"

    def test_useful_prose_survives_redaction(self):
        ingest = _CapturingIngest()
        note = f"Contained host, rotated {_AWS_KEY}. Followed runbook KB0010234."
        ingest_security_notes("org-a", [_incident(note)], ingest_fn=ingest)
        content = ingest.calls[0][1][0].content
        assert "Contained host" in content
        assert "KB0010234" in content

    def test_security_pattern_set_covers_ioc_and_credential_kinds(self):
        outcome = scan_and_redact_security(_ALL_SEEDED_NOTE)
        fired = set(outcome.pattern_types)
        for expected in (
            "ipv4_address", "defanged_indicator", "sha256_hash", "md5_hash",
            "mac_address", "ipv6_address", "email_address", "url_credentials",
            "aws_access_key_id", "github_token", "private_key", "secret_assignment",
        ):
            assert expected in fired, f"pattern did not fire: {expected}"
        # Every emitted type is a declared security pattern type.
        assert fired <= set(SECURITY_PATTERN_TYPES)


# ─────────────────────────────────────────────────────────────────────────────
# Redaction happens BEFORE the substrate ever sees the note
# ─────────────────────────────────────────────────────────────────────────────


class TestRedactBeforeSubstrate:
    def test_substrate_only_ever_receives_sanitized_text(self):
        ingest = _CapturingIngest()
        ingest_security_notes(
            "org-a", [_incident(f"beacon {_IPV4} key {_AWS_KEY}")], ingest_fn=ingest
        )
        content = ingest.calls[0][1][0].content
        assert _IPV4 not in content
        assert _AWS_KEY not in content

    def test_artifact_content_type_and_source_markers(self):
        artifact = build_security_note_artifact(_incident("plain note, no secrets"))[0]
        assert artifact.source_system == SECURITY_NOTE_SOURCE_SYSTEM
        assert artifact.content_type == SECURITY_NOTE_CONTENT_TYPE
        assert artifact.content_type in {"prose", "conversation", "code"}
        assert artifact.provenance["source_type"] == SECURITY_NOTE_SOURCE_TYPE

    def test_note_without_secret_passes_through_unchanged(self):
        ingest = _CapturingIngest()
        note = "Confirmed benign; closed after user verification."
        r = ingest_security_notes("org-a", [_incident(note)], ingest_fn=ingest)
        assert r.redacted == 0
        assert ingest.calls[0][1][0].content == note

    def test_incident_without_note_is_not_handed_off(self):
        ingest = _CapturingIngest()
        inc = _incident("x")
        for f in ("work_notes", "comments", "additional_comments", "close_notes"):
            inc.pop(f, None)
        r = ingest_security_notes("org-a", [inc], ingest_fn=ingest)
        assert r.notes_seen == 0
        assert r.artifacts_handed_off == 0
        assert ingest.calls == []

    def test_multiple_note_fields_are_combined_and_all_redacted(self):
        ingest = _CapturingIngest()
        inc = _incident(f"work {_IPV4}", note_field="work_notes")
        inc["comments"] = f"comment {_AWS_KEY}"
        ingest_security_notes("org-a", [inc], ingest_fn=ingest)
        content = ingest.calls[0][1][0].content
        assert _IPV4 not in content and _AWS_KEY not in content
        assert content.count("[REDACTED:") >= 2


# ─────────────────────────────────────────────────────────────────────────────
# Evidence pointer remains available for authorized trace-back
# ─────────────────────────────────────────────────────────────────────────────


class TestEvidencePointerAvailable:
    def test_evidence_pointer_travels_with_the_artifact(self):
        artifact = build_security_note_artifact(_incident(f"ioc {_IPV4}"))[0]
        pointer = artifact.provenance["evidence_pointer"]
        assert pointer["source_system"] == "servicenow"
        assert pointer["source_artifact"] == "sir-0001"
        assert pointer["origin"] == "observed"

    def test_evidence_pointer_carries_no_note_content(self):
        artifact = build_security_note_artifact(_incident(_ALL_SEEDED_NOTE))[0]
        pointer_blob = json.dumps(artifact.provenance["evidence_pointer"])
        for fragment in _LEAK_FRAGMENTS:
            assert fragment not in pointer_blob

    def test_source_url_preserved_for_access_controlled_trace_back(self):
        prov = build_security_note_artifact(_incident(f"ioc {_IPV4}"))[0].provenance
        assert "service-now.com" in prov["source_url"]

    def test_pointer_matches_the_incident_it_summarises(self):
        artifact = build_security_note_artifact(
            _incident(f"x {_AWS_KEY}", sys_id="sir-9999")
        )[0]
        assert artifact.source_artifact == "sir-9999"
        assert artifact.provenance["evidence_pointer"]["source_artifact"] == "sir-9999"


# ─────────────────────────────────────────────────────────────────────────────
# One redaction path, extended — and safe, value-free telemetry
# ─────────────────────────────────────────────────────────────────────────────


class TestSingleRedactionPathAndTelemetry:
    def test_reuses_the_shared_r18a2_scanner_module(self):
        import discovery.ingest.servicenow_security_notes_handoff as mod

        assert mod.scan_and_redact_security.__module__ == "discovery.ingest.secret_redaction"

    def test_base_scan_is_unchanged_iocs_only_redacted_on_security_path(self):
        # The extension must not aggressively IOC-redact ordinary content: the
        # base scanner leaves IPs and hashes intact.
        base = scan_and_redact(f"deploy note: host {_IPV4} build {_SHA256}")
        assert _IPV4 in base.text
        assert _SHA256 in base.text
        assert base.redacted is False

    def test_emits_ingestion_secret_redacted_event_without_leaking_value(self):
        events = []
        ingest_security_notes(
            "org-a", [_incident(_ALL_SEEDED_NOTE)],
            ingest_fn=_CapturingIngest(),
            record_event_fn=lambda t, p: events.append((t, p)),
        )
        assert len(events) == 1
        event_type, payload = events[0]
        assert event_type == "ingestion.secret_redacted"
        assert payload["connector_id"] == SECURITY_NOTE_CONNECTOR_ID
        assert payload["redaction_count"] >= 10
        blob = str(payload)
        for fragment in _LEAK_FRAGMENTS:
            assert fragment not in blob, f"telemetry leaked value: {fragment}"

    def test_no_telemetry_when_nothing_redacted(self):
        events = []
        ingest_security_notes(
            "org-a", [_incident("benign, closed")],
            ingest_fn=_CapturingIngest(),
            record_event_fn=lambda t, p: events.append((t, p)),
        )
        assert events == []

    def test_event_type_is_registered(self):
        from app.telemetry import REGISTERED_EVENT_TYPES

        assert "ingestion.secret_redacted" in REGISTERED_EVENT_TYPES


# ─────────────────────────────────────────────────────────────────────────────
# Org scoping (AC7 alignment) — two orgs isolated, blank org rejected
# ─────────────────────────────────────────────────────────────────────────────


class TestOrgScoping:
    def test_org_id_flows_to_substrate_write(self):
        ingest = _CapturingIngest()
        ingest_security_notes("org-globex", [_incident(f"k {_AWS_KEY}")], ingest_fn=ingest)
        assert ingest.calls[0][0] == "org-globex"

    def test_two_orgs_isolated(self):
        ingest = _CapturingIngest()
        ingest_security_notes(
            "org-a", [_incident(f"k {_AWS_KEY}", sys_id="a-1")], ingest_fn=ingest
        )
        ingest_security_notes(
            "org-b", [_incident(f"k {_AWS_KEY}", sys_id="b-1")], ingest_fn=ingest
        )
        assert ingest.calls[0][0] == "org-a"
        assert ingest.calls[1][0] == "org-b"
        assert ingest.calls[0][1][0].source_artifact == "a-1"
        assert ingest.calls[1][1][0].source_artifact == "b-1"

    def test_blank_org_rejected(self):
        with pytest.raises(ValueError):
            ingest_security_notes("", [_incident("x")], ingest_fn=_CapturingIngest())
