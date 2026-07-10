"""Unit tests for the freshness metrics (R18-B2 T6 / AC7).

Covers the T6 acceptance surface — "Lag is visible" (Section 1):

* ``freshness_metrics(org_id)`` reports the three required signals per org —
  pending change events, stale chunk count, and backfill progress — plus the
  failed-refresh and embedding-backlog counts the dashboard needs to tell
  "working through it" apart from "stuck";
* the numbers come from the same org-scoped primitives the workers run on
  (refresh queue counts, store counts, the gateway-resolved active model), and
  every read is passed the caller's org;
* backfill progress math: an empty partition is converged (1.0), a repin reads
  as honest partial progress, and a degraded gateway (no active model) reads
  0.0 — never a false 'complete';
* metrics never lie by degrading: a failing read RAISES instead of returning
  zeros that would report perfect health while the store is down;
* the route response model accepts exactly the shape the module produces.

``refresh_queue`` / ``store`` / ``embedder`` are monkeypatched with recording
fakes, so this runs in the unit suite with no PostgreSQL/pgvector or gateway
dependency; the DB-backed end-to-end coverage (including the HTTP route) lives
in the contract suite (``test_retrieval_freshness_metrics_contract.py``).
"""
from __future__ import annotations

import pytest

from app.retrieval import metrics
from app.retrieval.metrics import backfill_progress, freshness_metrics


class _FakeQueue:
    def __init__(self, pending=0, failed=0):
        self.pending, self.failed = pending, failed
        self.calls: list[tuple[str, str]] = []

    def pending_count(self, org_id):
        self.calls.append(("pending_count", org_id))
        return self.pending

    def failed_count(self, org_id):
        self.calls.append(("failed_count", org_id))
        return self.failed


class _FakeStore:
    def __init__(self, total=0, embedded=0, stale=0, stale_model=0):
        self.total, self.embedded = total, embedded
        self.stale, self.stale_model = stale, stale_model
        self.calls: list[tuple] = []

    def count_chunks(self, org_id, embedded_only=False):
        self.calls.append(("count_chunks", org_id, embedded_only))
        return self.embedded if embedded_only else self.total

    def count_stale(self, org_id):
        self.calls.append(("count_stale", org_id))
        return self.stale

    def count_stale_model(self, org_id, model, version):
        self.calls.append(("count_stale_model", org_id, model, version))
        return self.stale_model


class _FakeEmbedder:
    def __init__(self, identity=("prov:model-a", "2")):
        self.identity = identity

    def active_embedding_model(self):
        return self.identity


@pytest.fixture
def fakes(monkeypatch):
    queue = _FakeQueue()
    store = _FakeStore()
    embedder = _FakeEmbedder()
    monkeypatch.setattr(metrics, "refresh_queue", queue)
    monkeypatch.setattr(metrics, "store", store)
    monkeypatch.setattr(metrics, "embedder", embedder)
    return queue, store, embedder


# ---------------------------------------------------------------------------
# The full metrics shape (AC7): the three required signals, per org
# ---------------------------------------------------------------------------


def test_metrics_report_pending_stale_and_backfill_per_org(fakes):
    queue, store, _ = fakes
    queue.pending, queue.failed = 3, 1
    store.total, store.embedded, store.stale, store.stale_model = 250, 240, 12, 40

    out = freshness_metrics("org_a")

    assert out["org_id"] == "org_a"
    assert out["pending_change_events"] == 3
    assert out["failed_refreshes"] == 1
    assert out["stale_chunks"] == 12
    assert out["chunks_total"] == 250
    assert out["chunks_embedded"] == 240
    assert out["pending_embeddings"] == 10
    assert out["generated_at"]
    backfill = out["backfill"]
    assert backfill["active_model"] == "prov:model-a"
    assert backfill["active_model_version"] == "2"
    assert backfill["embedded_total"] == 240
    assert backfill["awaiting_backfill"] == 40
    assert backfill["on_active_model"] == 200
    assert backfill["progress"] == round(200 / 240, 4)
    assert backfill["complete"] is False


def test_every_read_is_scoped_to_the_calling_org(fakes):
    queue, store, _ = fakes
    freshness_metrics("org_xyz")
    assert queue.calls and all(call[1] == "org_xyz" for call in queue.calls)
    assert store.calls and all(call[1] == "org_xyz" for call in store.calls)


# ---------------------------------------------------------------------------
# Backfill progress math
# ---------------------------------------------------------------------------


def test_empty_partition_is_converged_not_broken(fakes):
    _, store, _ = fakes  # total/embedded all 0
    out = backfill_progress("org_a")
    assert out["embedded_total"] == 0
    assert out["awaiting_backfill"] == 0
    assert out["progress"] == 1.0
    assert out["complete"] is True
    # Nothing embedded -> the stale-model count query is not even made.
    assert not any(call[0] == "count_stale_model" for call in store.calls)


def test_fully_backfilled_reads_complete(fakes):
    _, store, _ = fakes
    store.total = store.embedded = 50
    store.stale_model = 0
    out = backfill_progress("org_a")
    assert out["progress"] == 1.0
    assert out["complete"] is True


def test_fresh_repin_reads_zero_progress(fakes):
    _, store, _ = fakes
    store.total = store.embedded = 50
    store.stale_model = 50  # every vector is on the old model
    out = backfill_progress("org_a")
    assert out["on_active_model"] == 0
    assert out["progress"] == 0.0
    assert out["complete"] is False


def test_degraded_gateway_never_reads_complete(fakes):
    # No resolvable active model: nothing can count as current. The metric must
    # match the backfill worker (which parks) — not report a false 'complete'.
    _, store, embedder = fakes
    embedder.identity = ("", "")
    store.total = store.embedded = 10
    store.stale_model = 10  # everything is DISTINCT FROM the empty identity
    out = backfill_progress("org_a")
    assert out["active_model"] == ""
    assert out["progress"] == 0.0
    assert out["complete"] is False


def test_active_model_pair_is_passed_to_the_stale_count(fakes):
    _, store, embedder = fakes
    embedder.identity = ("prov:model-b", "7")
    store.total = store.embedded = 5
    backfill_progress("org_a")
    assert ("count_stale_model", "org_a", "prov:model-b", "7") in store.calls


# ---------------------------------------------------------------------------
# Metrics never lie by degrading to zeros
# ---------------------------------------------------------------------------


def test_a_failing_read_raises_instead_of_reporting_zeros(fakes, monkeypatch):
    _, store, _ = fakes

    def boom(org_id, embedded_only=False):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "count_chunks", boom)
    with pytest.raises(RuntimeError):
        freshness_metrics("org_a")


# ---------------------------------------------------------------------------
# The route response model accepts exactly this shape
# ---------------------------------------------------------------------------


def test_response_model_matches_metrics_shape(fakes):
    from app.routes_retrieval import FreshnessMetricsResponse

    queue, store, _ = fakes
    queue.pending, store.total, store.embedded, store.stale = 2, 30, 20, 4
    validated = FreshnessMetricsResponse(**freshness_metrics("org_a"))
    assert validated.org_id == "org_a"
    assert validated.pending_change_events == 2
    assert validated.stale_chunks == 4
    assert validated.pending_embeddings == 10
    assert validated.backfill.embedded_total == 20
