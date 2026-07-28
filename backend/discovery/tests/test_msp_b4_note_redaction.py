"""MSP-B4 T6 — redaction-before-indexing for ServiceNow resolution notes (AC6).

Seeded credentials in resolution notes must never reach retrievable content;
the note stays traceable through its evidence pointer; org scoping is preserved;
and B4 reuses the single R18-A2 redaction path (no second behaviour).

Offline; no ServiceNow credentials required.
"""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from discovery.ingest.servicenow_notes_handoff import (  # noqa: E402
    RESOLUTION_NOTE_CONNECTOR_ID,
    RESOLUTION_NOTE_CONTENT_TYPE,
    RESOLUTION_NOTE_SOURCE_SYSTEM,
    build_resolution_note_artifact,
    ingest_resolution_notes,
)

# A spread of real secret signatures the shared R18-A2 scanner recognises.
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_GH_TOKEN = "ghp_" + "a" * 36
_PASSWORD_ASSIGN = "password=Sup3rSecretValue"
_CONN_STRING = "Server=db;Uid=admin;Password=Hunter2Hunter2;"


def _incident(note, *, sys_id="incident-sys-0009", number="INC0000009",
              category="Software"):
    return {
        "sys_id": sys_id,
        "number": number,
        "category": category,
        "source_url": f"https://acme.service-now.com/incident.do?sys_id={sys_id}",
        "close_notes": note,
        "resolution": {
            "incident_sys_id": sys_id,
            "resolved_at": "2026-06-01 12:00:00",
            "evidence": {
                "source_system": "servicenow",
                "source_artifact": sys_id,
                "origin": "observed",
                "source_artifact_type": "record_id",
                "source_url": f"https://acme.service-now.com/incident.do?sys_id={sys_id}",
            },
            "notes_evidence": {
                "source_system": "servicenow",
                "source_artifact": sys_id,
                "origin": "observed",
            },
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
# AC6 — seeded credentials never reach retrievable content
# ─────────────────────────────────────────────────────────────────────────────


class TestSeededSecretsNeverIndexed:
    @pytest.mark.parametrize(
        "secret",
        [_AWS_KEY, _GH_TOKEN, _PASSWORD_ASSIGN, _CONN_STRING],
    )
    def test_secret_absent_from_handed_off_content(self, secret):
        ingest = _CapturingIngest()
        note = f"Resolved the outage. {secret} was rotated. See runbook KB0010234."
        ingest_resolution_notes(
            "org-acme", [_incident(note)], ingest_fn=ingest, record_event_fn=lambda *a: None
        )
        (_org, artifacts) = ingest.calls[0]
        content = artifacts[0].content
        # The distinctive secret value must be gone from the retrievable text.
        for fragment in ("AKIAIOSFODNN7EXAMPLE", "ghp_", "Sup3rSecretValue", "Hunter2Hunter2"):
            assert fragment not in content, f"secret leaked into retrieval content: {fragment}"
        assert "[REDACTED:" in content

    def test_useful_content_survives_redaction(self):
        ingest = _CapturingIngest()
        note = f"Restarted service; {_AWS_KEY} rotated. Fixed per runbook KB0010234."
        ingest_resolution_notes("org-acme", [_incident(note)], ingest_fn=ingest)
        content = ingest.calls[0][1][0].content
        assert "Restarted service" in content
        assert "KB0010234" in content  # deterministic runbook ref preserved

    def test_retrieval_chunks_are_clean_end_to_end(self):
        """The sanitized text is what actually gets chunked for the index."""
        from app.retrieval.ingest import build_records

        built = build_resolution_note_artifact(
            _incident(f"leak {_AWS_KEY} and {_PASSWORD_ASSIGN} end")
        )
        assert built is not None
        artifact, outcome, _prov = built
        assert outcome.redacted
        records = build_records("org-acme", artifact)
        for rec in records:
            assert "AKIAIOSFODNN7EXAMPLE" not in rec.content
            assert "Sup3rSecretValue" not in rec.content


# ─────────────────────────────────────────────────────────────────────────────
# Redaction happens BEFORE the substrate ever sees the note
# ─────────────────────────────────────────────────────────────────────────────


class TestRedactBeforeSubstrate:
    def test_substrate_only_ever_receives_sanitized_text(self):
        ingest = _CapturingIngest()
        ingest_resolution_notes(
            "org-acme", [_incident(f"key {_AWS_KEY} here")], ingest_fn=ingest
        )
        # Whatever reached the substrate is already redacted — there is no code
        # path that hands raw text and redacts afterward.
        assert "AKIAIOSFODNN7EXAMPLE" not in ingest.calls[0][1][0].content

    def test_artifact_content_type_and_source_system(self):
        built = build_resolution_note_artifact(_incident("plain note, no secrets"))
        artifact = built[0]
        assert artifact.source_system == RESOLUTION_NOTE_SOURCE_SYSTEM
        assert artifact.content_type == RESOLUTION_NOTE_CONTENT_TYPE
        assert artifact.content_type in {"prose", "conversation", "code"}

    def test_note_without_secret_passes_through_unchanged(self):
        ingest = _CapturingIngest()
        note = "Cleared the cache and confirmed with the requester."
        r = ingest_resolution_notes("org-acme", [_incident(note)], ingest_fn=ingest)
        assert r.redacted == 0
        assert ingest.calls[0][1][0].content == note

    def test_incident_without_note_is_not_handed_off(self):
        ingest = _CapturingIngest()
        inc = _incident("x")
        inc["close_notes"] = None
        r = ingest_resolution_notes("org-acme", [inc], ingest_fn=ingest)
        assert r.notes_seen == 0
        assert r.artifacts_handed_off == 0
        assert ingest.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# Evidence pointer remains available for authorized trace-back
# ─────────────────────────────────────────────────────────────────────────────


class TestEvidencePointerAvailable:
    def test_evidence_pointer_travels_with_the_artifact(self):
        built = build_resolution_note_artifact(_incident(f"secret {_AWS_KEY}"))
        artifact = built[0]
        pointer = artifact.provenance["evidence_pointer"]
        assert pointer["source_system"] == "servicenow"
        assert pointer["source_artifact"] == "incident-sys-0009"
        assert pointer["origin"] == "observed"

    def test_source_url_preserved_for_access_controlled_trace_back(self):
        built = build_resolution_note_artifact(_incident(f"secret {_AWS_KEY}"))
        prov = built[0].provenance
        assert "service-now.com" in prov["source_url"]

    def test_pointer_matches_the_incident_it_summarises(self):
        built = build_resolution_note_artifact(
            _incident(f"x {_AWS_KEY}", sys_id="incident-sys-1234")
        )
        artifact = built[0]
        assert artifact.source_artifact == "incident-sys-1234"
        assert artifact.provenance["evidence_pointer"]["source_artifact"] == "incident-sys-1234"


# ─────────────────────────────────────────────────────────────────────────────
# Org scoping preserved (AC7)
# ─────────────────────────────────────────────────────────────────────────────


class TestOrgScoping:
    def test_org_id_flows_to_substrate_write(self):
        ingest = _CapturingIngest()
        ingest_resolution_notes("org-globex", [_incident(f"k {_AWS_KEY}")], ingest_fn=ingest)
        assert ingest.calls[0][0] == "org-globex"

    def test_two_orgs_isolated(self):
        ingest = _CapturingIngest()
        ingest_resolution_notes("org-a", [_incident(f"k {_AWS_KEY}", sys_id="a-1")], ingest_fn=ingest)
        ingest_resolution_notes("org-b", [_incident(f"k {_AWS_KEY}", sys_id="b-1")], ingest_fn=ingest)
        assert ingest.calls[0][0] == "org-a"
        assert ingest.calls[1][0] == "org-b"
        assert ingest.calls[0][1][0].source_artifact == "a-1"
        assert ingest.calls[1][1][0].source_artifact == "b-1"

    def test_blank_org_rejected(self):
        with pytest.raises(ValueError):
            ingest_resolution_notes("", [_incident("x")], ingest_fn=_CapturingIngest())


# ─────────────────────────────────────────────────────────────────────────────
# One redaction path + safe telemetry (reuse, not a second behaviour)
# ─────────────────────────────────────────────────────────────────────────────


class TestSingleRedactionPathAndTelemetry:
    def test_reuses_r18a2_scanner(self):
        # The module must not define its own patterns — it imports scan_and_redact.
        import discovery.ingest.servicenow_notes_handoff as mod

        assert mod.scan_and_redact.__module__ == "discovery.ingest.secret_redaction"

    def test_emits_ingestion_secret_redacted_event_without_leaking_value(self):
        events = []
        ingest_resolution_notes(
            "org-acme",
            [_incident(f"leak {_AWS_KEY} and {_PASSWORD_ASSIGN}")],
            ingest_fn=_CapturingIngest(),
            record_event_fn=lambda t, p: events.append((t, p)),
        )
        assert len(events) == 1
        event_type, payload = events[0]
        assert event_type == "ingestion.secret_redacted"
        assert payload["connector_id"] == RESOLUTION_NOTE_CONNECTOR_ID
        assert payload["redaction_count"] >= 2
        assert set(payload["pattern_types"]) <= {"aws_access_key_id", "secret_assignment"}
        # The secret value is NEVER carried on the event.
        blob = str(payload)
        assert "AKIAIOSFODNN7EXAMPLE" not in blob
        assert "Sup3rSecretValue" not in blob

    def test_no_telemetry_when_nothing_redacted(self):
        events = []
        ingest_resolution_notes(
            "org-acme",
            [_incident("clean note, cleared cache")],
            ingest_fn=_CapturingIngest(),
            record_event_fn=lambda t, p: events.append((t, p)),
        )
        assert events == []

    def test_event_type_is_registered(self):
        from app.telemetry import REGISTERED_EVENT_TYPES

        assert "ingestion.secret_redacted" in REGISTERED_EVENT_TYPES
