"""Unit tests for the source-aware retrieval API (R18-B1 T4).

Covers the T4 acceptance surface for ``retrieve()`` with the embedding gateway and
the pgvector store faked, so this runs in the unit suite (the DB-backed end-to-end
coverage lives in the contract suite):

* org-scoping — the org_id is threaded to the store's hard-partitioned search (AC3);
* source-aware — ``source_filter`` is normalised (trim/dedupe/sort) and passed
  through; an explicit filter naming nothing valid returns ``[]`` (AC4);
* min-score — the floor is passed through to the store (AC4);
* EvidencePointer — each result exposes ``chunk_id`` + ``retrieval_result_id`` and
  builds a valid observed pointer carrying them (AC5);
* robustness — blank query / non-positive k / gateway embedding miss return ``[]``
  and never raise;
* observability — one ``retrieval.query_completed`` telemetry event per call,
  carrying counts and filter shape only (never the query text or content).
"""
from __future__ import annotations

import app.telemetry as telemetry
from app.provenance import OBSERVED
from app.retrieval import api


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _row(chunk_id, similarity, source_system="confluence", source_artifact="page/1",
         is_stale=False):
    return {
        "chunk_id": chunk_id,
        "org_id": "org_x",
        "content": f"content-{chunk_id}",
        "content_hash": "h",
        "content_type": "prose",
        "source_system": source_system,
        "source_artifact": source_artifact,
        "source_timestamp": "2026-07-07T00:00:00+00:00",
        "chunk_position": 0,
        "embedding_model": "fake:m",
        "embedding_model_version": "1",
        "is_stale": is_stale,
        "similarity": similarity,
    }


class _Recorder:
    def __init__(self):
        self.search_calls = []
        self.telemetry = []
        self.rows = []

    def install(self, monkeypatch, *, embed=True, rows=None):
        self.rows = rows if rows is not None else []
        monkeypatch.setattr(
            api.embedder, "embed_texts", lambda texts: [[0.1, 0.2, 0.3]] if embed else []
        )
        monkeypatch.setattr(
            api.embedder, "active_embedding_model", lambda: ("fake:m", "1")
        )

        def _search(**kwargs):
            self.search_calls.append(kwargs)
            return list(self.rows)

        monkeypatch.setattr(api.store, "search", _search)
        monkeypatch.setattr(
            telemetry, "record_event", lambda et, payload=None: self.telemetry.append((et, payload))
        )
        return self


# ---------------------------------------------------------------------------
# Happy path: mapping, ranking, org-scoping
# ---------------------------------------------------------------------------


def test_returns_ranked_chunks_mapped_from_store_rows(monkeypatch):
    rec = _Recorder().install(
        monkeypatch, rows=[_row("c1", 0.91), _row("c2", 0.80)]
    )
    out = api.retrieve("org_x", "revenue growth", k=5)

    assert [c.chunk_id for c in out] == ["c1", "c2"]  # store order preserved
    assert [c.similarity for c in out] == [0.91, 0.80]
    assert all(isinstance(c.similarity, float) for c in out)
    # retrieval_result_id is minted per hit and unique.
    rids = {c.retrieval_result_id for c in out}
    assert len(rids) == 2 and all(rids)
    # org-scoping: the querying org is threaded to the store.
    assert rec.search_calls[0]["org_id"] == "org_x"


def test_k_is_passed_through_to_store(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q", k=3)
    assert rec.search_calls[0]["k"] == 3


# ---------------------------------------------------------------------------
# Source-aware (AC4)
# ---------------------------------------------------------------------------


def test_source_filter_normalised_trim_dedupe_sort(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q", source_filter=["slack", " confluence ", "confluence", ""])
    assert rec.search_calls[0]["source_filter"] == ["confluence", "slack"]


def test_no_source_filter_passes_none(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q")
    assert rec.search_calls[0]["source_filter"] is None


def test_source_filter_naming_nothing_valid_returns_empty_without_search(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[_row("c1", 0.9)])
    out = api.retrieve("org_x", "q", source_filter=["", "   "])
    assert out == []
    assert rec.search_calls == []  # scoped to nothing → never widens to all sources
    # Still observable.
    assert rec.telemetry and rec.telemetry[-1][0] == "retrieval.query_completed"


# ---------------------------------------------------------------------------
# min-score (AC4)
# ---------------------------------------------------------------------------


def test_min_score_passed_through(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q", min_score=0.6)
    assert rec.search_calls[0]["min_score"] == 0.6


# ---------------------------------------------------------------------------
# Stale handling (R18-B2 T4 / AC1) — excluded by default, policy-flagged
# ---------------------------------------------------------------------------


def test_include_stale_defaults_false_and_is_passed_to_store(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q")
    # Default excludes stale: the store is told not to include stale rows.
    assert rec.search_calls[0]["include_stale"] is False


def test_include_stale_true_is_passed_to_store(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q", include_stale=True)
    assert rec.search_calls[0]["include_stale"] is True


def test_is_stale_flag_mapped_onto_results(monkeypatch):
    _Recorder().install(
        monkeypatch,
        rows=[_row("fresh", 0.9, is_stale=False), _row("stale", 0.8, is_stale=True)],
    )
    out = api.retrieve("org_x", "q", include_stale=True)
    by_id = {c.chunk_id: c for c in out}
    assert by_id["fresh"].is_stale is False
    assert by_id["stale"].is_stale is True


def test_stale_flag_defaults_false_when_row_omits_it(monkeypatch):
    # A row without an is_stale key (e.g. an older projection) maps to not-stale,
    # never raises.
    row = _row("c1", 0.9)
    row.pop("is_stale")
    _Recorder().install(monkeypatch, rows=[row])
    out = api.retrieve("org_x", "q")
    assert out[0].is_stale is False


def test_telemetry_records_include_stale_flag(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q")
    payload = [e for e in rec.telemetry if e[0] == "retrieval.query_completed"][0][1]
    assert payload["include_stale"] is False
    # stale_count is only carried when stale chunks are being included.
    assert "stale_count" not in payload


def test_telemetry_records_stale_count_when_including_stale(monkeypatch):
    rec = _Recorder().install(
        monkeypatch,
        rows=[_row("a", 0.9, is_stale=True), _row("b", 0.8, is_stale=False)],
    )
    api.retrieve("org_x", "q", include_stale=True)
    payload = [e for e in rec.telemetry if e[0] == "retrieval.query_completed"][0][1]
    assert payload["include_stale"] is True
    assert payload["stale_count"] == 1


# ---------------------------------------------------------------------------
# EvidencePointer fields (AC5)
# ---------------------------------------------------------------------------


def test_result_builds_valid_observed_evidence_pointer(monkeypatch):
    _Recorder().install(monkeypatch, rows=[_row("c1", 0.77)])
    out = api.retrieve("org_x", "q")
    ptr = out[0].to_evidence_pointer()

    assert ptr.is_valid()
    assert ptr.origin == OBSERVED
    assert ptr.chunk_id == "c1"  # filled from the stored chunk (AC5)
    assert ptr.retrieval_result_id == out[0].retrieval_result_id  # per-query id (AC5)
    assert ptr.confidence == 0.77  # similarity carried as confidence
    assert ptr.source_system == "confluence"


# ---------------------------------------------------------------------------
# Robustness — never raises, returns [] on empty inputs / gateway miss
# ---------------------------------------------------------------------------


def test_blank_query_returns_empty(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[_row("c1", 0.9)])
    assert api.retrieve("org_x", "   ") == []
    assert rec.search_calls == []


def test_missing_org_returns_empty(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[_row("c1", 0.9)])
    assert api.retrieve("", "q") == []
    assert rec.search_calls == []


def test_non_positive_k_returns_empty_without_search(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[_row("c1", 0.9)])
    assert api.retrieve("org_x", "q", k=0) == []
    assert rec.search_calls == []


def test_gateway_embedding_miss_returns_empty(monkeypatch):
    rec = _Recorder().install(monkeypatch, embed=False, rows=[_row("c1", 0.9)])
    out = api.retrieve("org_x", "q")
    assert out == []
    assert rec.search_calls == []  # no vector → no search
    et, payload = rec.telemetry[-1]
    assert et == "retrieval.query_completed"
    assert payload["query_embedded"] is False


# ---------------------------------------------------------------------------
# Observability (telemetry) — counts and shape only, no query text/content
# ---------------------------------------------------------------------------


def test_emits_query_completed_with_counts_and_shape_only(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[_row("c1", 0.9), _row("c2", 0.8)])
    api.retrieve("org_x", "secret query text", k=4, source_filter=["confluence"], min_score=0.5)

    events = [e for e in rec.telemetry if e[0] == "retrieval.query_completed"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["org_id"] == "org_x"
    assert payload["k"] == 4
    assert payload["result_count"] == 2
    assert payload["source_filter"] == ["confluence"]
    assert payload["min_score"] == 0.5
    assert payload["query_embedded"] is True
    # PII guard: never the query text or chunk content.
    serialized = str(payload)
    assert "secret query text" not in serialized
    assert "content-c1" not in serialized


def test_unscoped_query_omits_filter_keys_in_telemetry(monkeypatch):
    rec = _Recorder().install(monkeypatch, rows=[])
    api.retrieve("org_x", "q")
    payload = [e for e in rec.telemetry if e[0] == "retrieval.query_completed"][0][1]
    assert "source_filter" not in payload
    assert "min_score" not in payload


def test_registered_event_type_present():
    # record_event() raises for an unregistered type, so registration must exist.
    assert "retrieval.query_completed" in telemetry.REGISTERED_EVENT_TYPES
