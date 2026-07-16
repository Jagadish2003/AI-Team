"""
R18-A4 / AT-599 (T6) — Slack & Teams deep-content contract suite (Section 3 ACs).

The single, AC-mapped contract suite for the depth phase of the 1.6/1.7 chat
connectors. It drives BOTH platforms through the one public path a real discovery
run uses — the deep-content hand-off → the REAL R18-B1 retrieval substrate → the
REAL R18-B2 freshness/refresh machinery → the source-aware ``retrieve()`` API — and
the REAL corroboration engine for the trust ceiling. Nothing is stubbed except the
embedding provider (a fake registered with the REAL model gateway and selected via
``MODEL_EMBEDDING_PROVIDER``, exactly as the B1 acceptance suite does) and the P5
channel-selection lookup (monkeypatched so the selection boundary is deterministic).

Section 3 acceptance criteria proven here:

  AC1 — conversation content from a selected Slack channel AND a granted Teams
        channel is chunked, indexed, and retrievable with thread-level provenance
        (origin='observed', evidence pointer at the exact thread).
  AC2 — content from UNSELECTED, PRIVATE, and DM channels is never ingested —
        verified by seeding all three (per platform) and confirming absence from
        retrieval.
  AC3 — an edited message refreshes its whole thread; a deleted message's content
        leaves retrieval immediately.
  AC4 — incremental runs ingest only new/changed messages since the connector
        checkpoint — an unchanged second run re-reads nothing and re-indexes nothing.
  AC5 — a finding whose context includes conversation evidence can trace to the
        exact thread via the retrieved chunk's evidence pointer.
  AC6 — a Slack/Teams-only evidence base never yields a HIGH-confidence finding
        (the MEDIUM conversation ceiling holds) for BOTH platforms.

The store-backed criteria (AC1–AC5) skip cleanly where no pgvector-backed store is
present; AC6 is pure engine logic and always runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

import pytest

import app.db as app_db
from app import db
from app.corroboration_engine import (
    apply_corroboration_confidence,
    build_corroboration_run_data,
    evaluate_corroboration,
)
from app.model_gateway import register_provider
from app.model_gateway._interface import (
    GenerationRequest,
    GenerationResult,
    ModelProvider,
)
from app.retrieval import embedder, freshness, refresh
from app.retrieval.api import retrieve
import discovery.ingest.slack as slack_mod
import discovery.ingest.teams as teams_mod
from discovery.ingest import change_runner
from discovery.ingest.slack import SlackIngestor
from discovery.ingest.teams import TeamsIngestor
from discovery.ingest.slack_signals import build_slack_corroboration_payload
from discovery.ingest.teams_signals import build_teams_corroboration_payload
from discovery.t3_ceiling_clamp import apply_t3_ceiling_clamp


# ---------------------------------------------------------------------------
# Store availability — the store-backed ACs (AC1–AC5) skip cleanly without it.
# ---------------------------------------------------------------------------
def _retrieval_store_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_chunks')")
            return cur.fetchone()[0] is not None
        finally:
            con.close()
    except Exception:
        return False


requires_store = pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)

# Distinct single-token embedding basis: each term maps to one axis, so a leak of
# one channel's content into another's would be unmistakable at retrieval.
_TERMS = ("alpha", "beta", "gamma", "omega")


class _FakeProvider(ModelProvider):
    """One-hot embedder over ``_TERMS`` registered with the REAL model gateway."""

    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        out = []
        for t in texts:
            low = (t or "").lower()
            out.append([1.0 if term in low else 0.0 for term in _TERMS] + [0.01])
        return out

    def embedding_identity(self):
        return self._identity


_PROVIDER = _FakeProvider("at599_deep_embed", ("at599-deep:model", "1"))
register_provider(_PROVIDER)


# ---------------------------------------------------------------------------
# Record builders — the shape each connector's reach path yields per message.
# ---------------------------------------------------------------------------
def _slack_msg(channel_id, ts, user, text, *, thread_ts=None, reply_count=0,
               kind="created", channel_name="chan"):
    rec = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": ts,
        "user": user,
        "text": text,
        "reply_count": reply_count,
        "reply_users_count": 0,
        "reactions": [],
        "change_kind": kind,
    }
    if thread_ts is not None:
        rec["thread_ts"] = thread_ts
    return rec


def _teams_rec(channel_id, message_id, user, text, *, team_id="T-eng",
               reply_to_id=None, reply_count=0, kind="created",
               channel_name="chan", created="2026-06-10T09:00:00Z", display=None):
    return {
        "source_system": "teams",
        "team_id": team_id,
        "team_name": "Engineering",
        "channel_id": channel_id,
        "channel_name": channel_name,
        "message_id": message_id,
        "reply_to_id": reply_to_id,
        "reply_count": reply_count,
        "created_at": created,
        "last_modified_at": None,
        "user": user,
        "user_display_name": display or user,
        "text": text,
        "change_kind": kind,
    }


# ---------------------------------------------------------------------------
# Store helpers.
# ---------------------------------------------------------------------------
def _cleanup(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        try:
            cur.execute("DELETE FROM retrieval_refresh_queue WHERE org_id = %s", (org_id,))
        except Exception:
            pass
        con.commit()
    finally:
        con.close()


def _rows_for(org_id: str, source_artifact: str) -> list:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT source_system, source_artifact, content_type, provenance "
            "FROM retrieval_chunks WHERE org_id = %s AND source_artifact = %s",
            (org_id, source_artifact),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def _ids(hits) -> set:
    return {h.source_artifact for h in hits}


@pytest.fixture
def org(request, monkeypatch):
    """Isolated org + fake embedding provider + offline channel access."""
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _PROVIDER.name)
    monkeypatch.setenv("INGEST_MODE", "offline")  # fixtures supply channel access
    name = f"at599_{request.node.name}"[:60]
    _cleanup(name)
    yield name
    _cleanup(name)


def _set_slack_selection(monkeypatch, channels):
    """Patch the org's saved Slack P5 selection (None = read every accessible)."""
    def fake_get(org_id, connector_id):
        if connector_id != "slack":
            return None
        record = {"id": "slack", "status": "connected"}
        if channels is not None:
            record["channels"] = channels
        return record

    monkeypatch.setattr(app_db, "org_connector_get", fake_get)


# ===========================================================================
# AC1 — selected Slack + granted Teams content is retrievable with thread-level
#       provenance.
# ===========================================================================
@requires_store
def test_ac1_slack_selected_channel_retrievable_with_thread_provenance(org, monkeypatch):
    _set_slack_selection(monkeypatch, None)  # no selection → all accessible
    records = [
        _slack_msg("C001", "1000.0001", "U1", "the alpha incident on payments",
                   reply_count=1, channel_name="ops-incidents"),
        _slack_msg("C001", "1000.0500", "U2", "alpha rollback done",
                   thread_ts="1000.0001", channel_name="ops-incidents"),
    ]
    result = SlackIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)
    assert result.artifacts_failed == 0
    assert result.artifacts_handed_off == 1  # parent + reply → ONE thread

    rows = _rows_for(org, "C001:1000.0001")
    assert rows, "no chunks indexed for the Slack thread"
    assert all(r["source_system"] == "slack" for r in rows)
    assert all(r["content_type"] == "conversation" for r in rows)

    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "alpha", k=5, source_filter=["slack"])
    assert hits, "alpha did not retrieve the Slack thread"
    top = hits[0]
    assert top.source_system == "slack"
    assert top.source_artifact == "C001:1000.0001"  # thread-level id
    assert "U1:" in top.content and "U2:" in top.content  # author-attributed


@requires_store
def test_ac1_teams_granted_channel_retrievable_with_thread_provenance(org):
    records = [
        _teams_rec("19:ops", "m1", "U100", "the beta incident on payments",
                   reply_count=1, display="Ada", channel_name="ops-incidents",
                   created="2026-06-10T09:00:00Z"),
        _teams_rec("19:ops", "m2", "U101", "beta rollback done", reply_to_id="m1",
                   display="Lin", channel_name="ops-incidents",
                   created="2026-06-10T09:05:00Z"),
    ]
    result = TeamsIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)
    assert result.artifacts_failed == 0
    assert result.artifacts_handed_off == 1

    rows = _rows_for(org, "T-eng/19:ops:m1")
    assert rows, "no chunks indexed for the Teams thread"
    assert all(r["source_system"] == "teams" for r in rows)
    assert all(r["content_type"] == "conversation" for r in rows)

    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "beta", k=5, source_filter=["teams"])
    assert hits, "beta did not retrieve the Teams thread"
    top = hits[0]
    assert top.source_system == "teams"
    assert top.source_artifact == "T-eng/19:ops:m1"  # thread-level id
    assert "Ada:" in top.content and "Lin:" in top.content  # display-name attributed


# ===========================================================================
# AC2 — unselected + private + DM channel content is never ingested / retrievable.
#       (The existing per-platform handoff tests seed only a PRIVATE channel; this
#       closes the AC2 gap by seeding all THREE excluded kinds at once.)
# ===========================================================================
@requires_store
def test_ac2_slack_unselected_private_and_dm_channels_never_retrievable(org, monkeypatch):
    # Only C001 is selected. Seed a same-term message in each excluded kind so a
    # leak would surface unmistakably: C002 accessible-but-unselected, C900 private,
    # D001 a DM (never enumerated by the connector).
    _set_slack_selection(monkeypatch, ["C001"])
    records = [
        _slack_msg("C001", "1000.0001", "U1", "gamma note in the selected channel",
                   channel_name="ops-incidents"),
        _slack_msg("C002", "2000.0001", "U2", "gamma note in an UNSELECTED channel",
                   channel_name="deploys"),
        _slack_msg("C900", "3000.0001", "U9", "gamma secret in a PRIVATE channel",
                   channel_name="leadership-private"),
        _slack_msg("D001", "4000.0001", "U8", "gamma secret in a DM"),
    ]
    result = SlackIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)

    # Only the selected accessible channel was handed off.
    assert result.artifacts_handed_off == 1
    assert _rows_for(org, "C001:1000.0001")
    for excluded in ("C002:2000.0001", "C900:3000.0001", "D001:4000.0001"):
        assert _rows_for(org, excluded) == [], f"{excluded} must never be indexed"

    embedder.embed_pending_for_org(org)
    ids = _ids(retrieve(org, "gamma", k=10, source_filter=["slack"]))
    assert "C001:1000.0001" in ids
    assert not any(i.startswith(("C002", "C900", "D001")) for i in ids)


@requires_store
def test_ac2_teams_ungranted_private_and_dm_channels_never_retrievable(org):
    # Teams scope = granted standard channels. 19:ops is granted; 19:not-granted is
    # ungranted (the "unselected" analogue), 19:leads-private is private, and
    # 19:dm-chat models a DM the connector never enumerates. All carry the same term.
    records = [
        _teams_rec("19:ops", "m1", "U100", "omega note in the granted channel",
                   display="Ada", channel_name="ops-incidents"),
        _teams_rec("19:not-granted", "n1", "U901", "omega note in an UNGRANTED channel",
                   display="Stranger"),
        _teams_rec("19:leads-private", "p1", "U900", "omega secret in a PRIVATE channel",
                   display="Exec"),
        _teams_rec("19:dm-chat", "x1", "U700", "omega secret in a DM", display="Someone"),
    ]
    result = TeamsIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)

    assert result.artifacts_handed_off == 1
    assert _rows_for(org, "T-eng/19:ops:m1")
    for excluded in (
        "T-eng/19:not-granted:n1",
        "T-eng/19:leads-private:p1",
        "T-eng/19:dm-chat:x1",
    ):
        assert _rows_for(org, excluded) == [], f"{excluded} must never be indexed"

    embedder.embed_pending_for_org(org)
    ids = _ids(retrieve(org, "omega", k=10, source_filter=["teams"]))
    assert "T-eng/19:ops:m1" in ids
    assert not any(
        ("not-granted" in i or "leads-private" in i or "dm-chat" in i) for i in ids
    )


# ===========================================================================
# AC3 — edit refreshes the whole thread; delete removes content immediately.
# ===========================================================================
@requires_store
def test_ac3_edit_refreshes_thread_and_delete_removes_immediately(org, monkeypatch):
    _set_slack_selection(monkeypatch, None)
    # The refresh worker re-reads a stale thread through the registered resolver.
    refresh.register_content_resolver("slack", slack_mod.resolve_thread_content)

    thread_id = "C001:1000.0001"
    standalone_id = "C002:2000.0001"

    # 1) Index a thread (root + reply) and a standalone message.
    created = [
        _slack_msg("C001", "1000.0001", "U1", "alpha incident on payments", reply_count=1),
        _slack_msg("C001", "1000.0500", "U2", "beta rollback done", thread_ts="1000.0001"),
        _slack_msg("C002", "2000.0001", "U3", "gamma deploy to prod"),
    ]
    SlackIngestor().ingest_deep_content(org, created, freshness_fn=lambda e: None)
    embedder.embed_pending_for_org(org)
    assert thread_id in _ids(retrieve(org, "beta", k=5, source_filter=["slack"]))
    assert standalone_id in _ids(retrieve(org, "gamma", k=5, source_filter=["slack"]))

    # 2) EDIT: the source now reflects the edited reply text. The edit flows through
    #    REAL freshness → thread marked stale (excluded from retrieval at once).
    edited_channel = [
        {"ts": "1000.0001", "user": "U1", "text": "alpha incident on payments", "reply_count": 1},
        {"ts": "1000.0500", "user": "U2", "text": "omega mitigation shipped",
         "thread_ts": "1000.0001", "edited": {"ts": "1000.0600"}},
    ]
    monkeypatch.setattr(SlackIngestor, "_raw_messages",
                        lambda self, o, ch: list(edited_channel))
    monkeypatch.setattr(
        SlackIngestor, "_raw_channels",
        lambda self, o: [
            {"id": "C001", "name": "ops", "is_private": False,
             "is_member": True, "is_archived": False},
            {"id": "C002", "name": "deploys", "is_private": False,
             "is_member": True, "is_archived": False},
        ],
    )
    edit_res = SlackIngestor().ingest_deep_content(
        org, [_slack_msg("C001", "1000.0500", "U2", "omega mitigation shipped",
                         kind="updated", thread_ts="1000.0001")]
    )
    assert edit_res.threads_refreshed == 1
    assert thread_id not in _ids(retrieve(org, "beta", k=5, source_filter=["slack"]))

    # 3) The async refresh re-reads the WHOLE thread and re-chunks it → new text in,
    #    old text out, root untouched.
    refresh.refresh_pending_for_org(org)
    embedder.embed_pending_for_org(org)
    hits = retrieve(org, "omega", k=5, source_filter=["slack"])
    assert thread_id in _ids(hits)
    top = next(h for h in hits if h.source_artifact == thread_id)
    assert "omega mitigation shipped" in top.content
    assert "beta rollback" not in top.content
    assert "alpha incident on payments" in top.content  # whole thread present

    # 4) DELETE: a deleted standalone message's content leaves retrieval immediately.
    del_res = SlackIngestor().ingest_deep_content(
        org, [_slack_msg("C002", "2000.0001", "U3", "", kind="deleted")]
    )
    assert del_res.threads_removed == 1
    assert standalone_id not in _ids(retrieve(org, "gamma", k=10, source_filter=["slack"]))


# ===========================================================================
# AC4 — incremental: only new/changed messages since the checkpoint are read; an
#       unchanged second run re-reads nothing and re-indexes nothing.
# ===========================================================================
class _CheckpointStore:
    """In-memory checkpoint store for change_runner-driven contract tests."""

    def __init__(self):
        self.data: dict = {}

    def read(self, org_id, connector_id):
        return self.data.get((org_id, connector_id))

    def save(self, cp):
        self.data[(cp.org_id, cp.connector_id)] = cp


class _CapturingSubstrate:
    """Wraps the REAL ``ingest_content`` and records every artifact handed to it."""

    def __init__(self):
        from app.retrieval.ingest import ingest_content

        self._delegate = ingest_content
        self.handed: list = []

    def __call__(self, org_id, artifacts):
        artifacts = list(artifacts)
        self.handed.extend(artifacts)
        return self._delegate(org_id, artifacts)


def _chunk_count(org_id: str) -> int:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM retrieval_chunks WHERE org_id = %s", (org_id,)
        )
        return int(cur.fetchone()["n"])
    finally:
        con.close()


@requires_store
def test_ac4_incremental_second_run_reads_and_indexes_nothing_new(org, monkeypatch):
    # Drive the REAL Slack ingestor through the change runner over the offline
    # fixture — exactly as a discovery run does — handing each batch to the real
    # substrate. The connector's shared (org,'slack') checkpoint makes the second
    # unchanged run read nothing new: no artifacts handed off, no chunks re-indexed.
    _set_slack_selection(monkeypatch, None)
    store = _CheckpointStore()
    ing = SlackIngestor()

    first = _CapturingSubstrate()

    def process(batch, sub):
        # The reach signal path is untouched: records still carry their signal block.
        for r in batch.records:
            assert "signals" in r
        # freshness_fn no-op keeps this offline-safe; edit/delete is AC3's concern.
        ing.ingest_deep_content(org, batch.records, ingest_fn=sub, freshness_fn=lambda e: None)

    r1 = change_runner.ingest_with_checkpoint(
        ing, org, process_batch=lambda b: process(b, first),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert r1.ok
    assert first.handed, "first run should hand off the fixture's conversation content"
    # Only accessible channels C001/C002 produced content (privacy/scope boundary).
    assert all(a.source_artifact.split(":")[0] in {"C001", "C002"} for a in first.handed)

    embedder.embed_pending_for_org(org)
    count_after_first = _chunk_count(org)
    assert count_after_first > 0

    # Second run over the same, unchanged workspace.
    second = _CapturingSubstrate()
    r2 = change_runner.ingest_with_checkpoint(
        ing, org, process_batch=lambda b: process(b, second),
        read_checkpoint=store.read, save_checkpoint=store.save,
    )
    assert r2.ok
    assert second.handed == [], "unchanged workspace must re-read/re-hand nothing (AC4)"
    assert _chunk_count(org) == count_after_first, "no content re-indexed on an unchanged run"


# ===========================================================================
# AC5 — a retrieved conversation chunk's evidence pointer traces to the exact thread.
# ===========================================================================
@requires_store
def test_ac5_slack_conversation_evidence_traces_to_exact_thread(org, monkeypatch):
    _set_slack_selection(monkeypatch, None)
    records = [
        _slack_msg("C001", "1718000000.000100", "U1", "alpha incident opened",
                   reply_count=1, channel_name="ops-incidents"),
        _slack_msg("C001", "1718000600.000200", "U2", "alpha triage underway",
                   thread_ts="1718000000.000100", channel_name="ops-incidents"),
    ]
    SlackIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)
    embedder.embed_pending_for_org(org)

    hits = retrieve(org, "alpha", k=5, source_filter=["slack"])
    assert hits
    ep = hits[0].to_evidence_pointer().to_dict()
    # A finding citing this conversation can point at the EXACT thread, observed.
    assert ep["source_system"] == "slack"
    assert ep["source_artifact"] == "C001:1718000000.000100"
    assert ep["origin"] == "observed"        # retrieved content was seen in-source
    assert ep["chunk_id"]                     # the R16-B1 spine field, now populated
    assert ep["retrieval_result_id"]          # unique per (query, chunk) hit


@requires_store
def test_ac5_teams_conversation_evidence_traces_to_exact_thread(org):
    records = [
        _teams_rec("19:ops", "m1", "U100", "beta incident opened", reply_count=1,
                   display="Ada", channel_name="ops-incidents",
                   created="2026-06-10T09:00:00Z"),
        _teams_rec("19:ops", "m2", "U101", "beta triage underway", reply_to_id="m1",
                   display="Lin", channel_name="ops-incidents",
                   created="2026-06-10T09:05:00Z"),
    ]
    TeamsIngestor().ingest_deep_content(org, records, freshness_fn=lambda e: None)
    embedder.embed_pending_for_org(org)

    hits = retrieve(org, "beta", k=5, source_filter=["teams"])
    assert hits
    ep = hits[0].to_evidence_pointer().to_dict()
    assert ep["source_system"] == "teams"
    assert ep["source_artifact"] == "T-eng/19:ops:m1"
    assert ep["origin"] == "observed"
    assert ep["chunk_id"]
    assert ep["retrieval_result_id"]


# ===========================================================================
# AC6 — a Slack/Teams-only evidence base never yields a HIGH-confidence finding
#       (the MEDIUM conversation ceiling holds). Pure engine logic — always runs.
# ===========================================================================
_DETECTOR = "HANDOFF_FRICTION"
_PACK = "service_cloud"


def _now():
    return datetime.now(timezone.utc)


def _slack_escalation(now):
    ts = f"{now.timestamp():.6f}"
    return [{
        "channel_id": "C1", "channel_name": "ops-incidents", "ts": ts, "user": "u1",
        "reply_count": 6, "reply_users_count": 4, "reactions": [],
        "text": "war room — customers blocked",
    }]


def _teams_escalation(now):
    return [{
        "team_id": "T-eng", "channel_id": "19:ops", "channel_name": "ops-incidents",
        "created_at": now.isoformat(), "reply_count": 6, "reply_users_count": 4,
        "reactions": [], "text": "war room — customers blocked",
    }]


def test_ac6_slack_only_evidence_never_reaches_high():
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "slack"],  # 2 systems → COR-08 single-source not tripped
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_slack_corroboration_payload(_slack_escalation(now))],
    )
    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    assert "COR-05" in result.rule_ids       # supporting-only conversation rule
    assert "COR-06" not in result.rule_ids   # no primary corroborator present
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False
    # Defence-in-depth: even against a MEDIUM baseline the verdict never reaches HIGH.
    assert apply_corroboration_confidence("MEDIUM", result) == "MEDIUM"
    assert apply_t3_ceiling_clamp("HIGH", system_id="slack") == "MEDIUM"


def test_ac6_teams_only_evidence_never_reaches_high():
    now = _now()
    run_data = build_corroboration_run_data(
        systems=["salesforce", "teams"],
        sn_by_detector={},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_teams_corroboration_payload(_teams_escalation(now))],
    )
    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")

    assert "COR-05" in result.rule_ids
    assert "COR-06" not in result.rule_ids
    assert result.elevated_confidence == "MEDIUM"
    assert result.confidence_elevated is False
    assert apply_corroboration_confidence("MEDIUM", result) == "MEDIUM"
    assert apply_t3_ceiling_clamp("HIGH", system_id="teams") == "MEDIUM"


def test_ac6_conversation_only_mix_is_capped_but_primary_elevates():
    # A Slack+Teams-only mix (both conversation sources, no system of record) stays
    # capped at MEDIUM …
    assert apply_t3_ceiling_clamp(
        "HIGH",
        corroboration_sources=["Slack (supporting only)", "Teams (escalation pattern)"],
    ) == "MEDIUM"

    # … but the ceiling is RESPECTED, not a hard block: WITH a system-of-record
    # corroborator (ServiceNow, COR-06) the conversation signal legitimately elevates.
    now = _now()
    recent_iso = (now - timedelta(days=1)).isoformat()
    run_data = build_corroboration_run_data(
        systems=["servicenow", "slack"],
        sn_by_detector={_DETECTOR: [{"state": "Open", "sys_created_on": recent_iso}]},
        jira_by_detector={},
        run_timestamp_iso=now.isoformat(),
        source_payloads=[build_slack_corroboration_payload(_slack_escalation(now))],
    )
    result = evaluate_corroboration(_DETECTOR, _PACK, run_data, now, "default")
    assert "COR-06" in result.rule_ids
    assert result.elevated_confidence == "HIGH"
    assert apply_corroboration_confidence("MEDIUM", result) == "HIGH"
