"""
R18-C2 T2 (emission gap-fill) — the document ingestor emits an
``ingestion.artifact_skipped`` telemetry event at ORIGIN for every artifact it
skips-with-reason, so the run-health dashboard's content panel can report
skipped-with-reason volume as an explicit, org-scoped fact instead of inferring
it. Before T2 a skip was visible only on the in-flight record + a WARNING log.

The emission is fire-and-forget: a telemetry write failure must NEVER break
ingestion (R18-C2 T2: "emission must preserve the platform's non-blocking
behavior").

Pure, DB-free: drives the ingestor over an in-memory source (mirrors
test_documents_size_budget.py) and captures the events via a monkeypatched
``record_event``.
"""
from __future__ import annotations

from typing import Dict, List

import app.telemetry as telemetry_mod
from discovery.ingest import extraction
from discovery.ingest.documents import DocumentIngestor
from discovery.ingest.documents_source import DocumentRef, DocumentSource

ORG = "org_skip_tel"


class MemSource(DocumentSource):
    def __init__(self, docs: List[dict]):
        self._docs = docs
        self.reads: List[str] = []

    def list_documents(self, org_id):
        return [
            DocumentRef(
                artifact_id=d["id"],
                filename=d.get("filename", d["id"] + ".txt"),
                location="loc",
                signature=d.get("signature", "v1"),
                source_timestamp=d.get("ts"),
                size_bytes=d.get("size_bytes"),
                content_type=d.get("content_type"),
            )
            for d in self._docs
        ]

    def read(self, org_id, ref):
        self.reads.append(ref.artifact_id)
        for d in self._docs:
            if d["id"] == ref.artifact_id:
                return d["content"]
        raise KeyError(ref.artifact_id)


def _by_id(batches) -> Dict[str, dict]:
    return {r["artifact_id"]: r for b in batches for r in b.records}


def _capture_events(monkeypatch) -> List[tuple]:
    events: List[tuple] = []
    monkeypatch.setattr(
        telemetry_mod,
        "record_event",
        lambda event_type, payload=None: events.append((event_type, payload)),
    )
    return events


def _skips(events) -> List[dict]:
    return [p for (et, p) in events if et == "ingestion.artifact_skipped"]


# ── registration ───────────────────────────────────────────────────────────

def test_artifact_skipped_event_is_registered():
    # record_event() raises ValueError for an unregistered type, so the event
    # must be registered before the ingestor emits it.
    assert "ingestion.artifact_skipped" in telemetry_mod.REGISTERED_EVENT_TYPES


# ── emit at origin ────────────────────────────────────────────────────────────

def test_size_capped_skip_emits_event(monkeypatch):
    events = _capture_events(monkeypatch)
    src = MemSource([{"id": "big", "size_bytes": 100, "content": b"x" * 100}])
    list(DocumentIngestor(source=src, max_file_bytes=50).ingest_changes(ORG, None))

    skips = _skips(events)
    assert len(skips) == 1
    payload = skips[0]
    assert payload["reason"] == extraction.SIZE_CAPPED
    assert payload["org_id"] == ORG
    assert payload["connector_id"] == "documents"
    assert payload["artifact_id"] == "big"
    assert payload["count"] == 1


def test_budget_exceeded_skip_emits_event(monkeypatch):
    events = _capture_events(monkeypatch)
    src = MemSource([
        {"id": "a", "content": b"a" * 100, "ts": "2026-01-01T00:00:00Z"},
        {"id": "b", "content": b"b" * 100, "ts": "2026-01-02T00:00:00Z"},
    ])
    list(DocumentIngestor(source=src, extraction_budget_bytes=100).ingest_changes(ORG, None))

    skips = _skips(events)
    reasons = {s["artifact_id"]: s["reason"] for s in skips}
    assert reasons.get("b") == extraction.BUDGET_EXCEEDED
    assert "a" not in reasons  # 'a' extracted, no skip event


def test_unsupported_format_skip_emits_event(monkeypatch):
    events = _capture_events(monkeypatch)
    # A recognised-but-unsupported type → ExtractionSkipped after the read.
    src = MemSource([{"id": "diagram", "filename": "arch.vsdx", "content": b"x" * 90}])
    list(DocumentIngestor(source=src).ingest_changes(ORG, None))

    skips = _skips(events)
    assert len(skips) == 1
    assert skips[0]["artifact_id"] == "diagram"
    assert skips[0]["org_id"] == ORG


def test_extracted_files_emit_no_skip_event(monkeypatch):
    events = _capture_events(monkeypatch)
    src = MemSource([{"id": "ok", "content": b"hello world"}])
    list(DocumentIngestor(source=src).ingest_changes(ORG, None))
    assert _skips(events) == []


# ── non-blocking (telemetry failure must not break ingestion) ───────────────────

def test_skip_telemetry_failure_does_not_break_ingestion(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("telemetry backend down")

    monkeypatch.setattr(telemetry_mod, "record_event", _boom)

    src = MemSource([
        {"id": "big", "size_bytes": 100, "content": b"x" * 100, "ts": "2026-01-01T00:00:00Z"},
        {"id": "ok", "size_bytes": 5, "content": b"hi ok", "ts": "2026-01-02T00:00:00Z"},
    ])
    # A raising record_event must NOT propagate — ingestion completes and the
    # records are produced exactly as before.
    recs = _by_id(DocumentIngestor(source=src, max_file_bytes=50).ingest_changes(ORG, None))
    assert recs["big"]["extraction"]["status"] == "skipped"
    assert recs["big"]["extraction"]["reason"] == extraction.SIZE_CAPPED
    assert recs["ok"]["extraction"]["status"] == "extracted"
