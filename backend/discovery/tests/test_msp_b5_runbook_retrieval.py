"""MSP-B5 T2 — runbook-scoped semantic retrieval PATH contract tests.

Covers the retrieval-path behaviour the task specifies and the acceptance
criterion that belongs to T2:

  * AC7 — every retrieval query uses REDACTED note text only (the seeded-secret
          test lives upstream of the query builder), and every lookup is
          org-scoped end to end.

Plus the task's explicit retrieval-path requirements: reuse the Release 1.8
retrieval API; restrict to the runbook library (no Slack/code/incidents);
configured candidate limit + minimum score; stale excluded unless surfaced;
deterministic query construction; empty/incomplete fields handled without
placeholder noise; and a retrieval failure / unavailable embedding provider
returning an UNAVAILABLE result (distinct from a genuine empty) without breaking
the run. Pure-Python and offline — no gateway, no vector store, no contract DB.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

os.environ["INGEST_MODE"] = "offline"

from discovery.detectors.ops_recurrence import (  # noqa: E402
    RecurrenceConfig,
    find_recurrences,
)
from discovery.detectors.runbook_match import (  # noqa: E402
    DEFAULT_CANDIDATE_LIMIT,
    DEFAULT_MIN_SCORE,
    RETRIEVAL_OK,
    RETRIEVAL_UNAVAILABLE,
    RUNBOOK_SCOPE,
    RunbookRetrievalConfig,
    build_resolution_query,
    retrieve_runbook_candidates,
)
from discovery.ingest.secret_redaction import scan_and_redact  # noqa: E402
from discovery.signals.evidence_store import OrgScopeError  # noqa: E402
from discovery.signals.resolution_signature import (  # noqa: E402
    compute_incident_identity_signature,
    compute_resolution_signature,
)

_AS_OF = "2026-07-15 12:00:00"
_CONFIG = RecurrenceConfig(floor=3, window_days=30, max_examples=3)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures — a deterministic recurrence + a fake 1.8 retrieval API.
# ─────────────────────────────────────────────────────────────────────────────


def _incident(number: int, *, org_id: str = "org-a", category: str = "software",
              close_code: str = "Solved (Permanently)", ci_class: str = "cmdb_ci_server",
              group: str = "Platform Operations",
              short_description: str = "Portal email service unavailable",
              resolved_at: str = "2026-07-10 12:00:00", ttr: int = 3600) -> dict:
    sys_id = f"incident-sys-{number:04d}"
    return {
        "sys_id": sys_id,
        "number": f"INC{number:07d}",
        "org_id": org_id,
        "category": category,
        "ci_class": ci_class,
        "short_description": short_description,
        "assignment_group": group,
        "close_code": close_code,
        "resolved_at": resolved_at,
        "resolution": {
            "is_resolved": True,
            "resolution_category": category,
            "close_code": close_code,
            "resolved_by_group": group,
            "resolved_at": resolved_at,
            "time_to_resolve_seconds": ttr,
            "incident_identity_signature": compute_incident_identity_signature(
                category=category, short_description=short_description, ci_class=ci_class
            ),
            "resolution_signature": compute_resolution_signature(
                category=category, close_code=close_code,
                resolved_by_group=group, ci_class=ci_class,
            ),
            "incident_sys_id": sys_id,
        },
    }


def _recurrence(org_id: str = "org-a", **kw):
    incidents = [
        _incident(1, org_id=org_id, resolved_at="2026-07-10 12:00:00", **kw),
        _incident(2, org_id=org_id, resolved_at="2026-07-12 12:00:00", **kw),
        _incident(3, org_id=org_id, resolved_at="2026-07-14 12:00:00", **kw),
    ]
    payload = {"org_id": org_id,
               "incident_metrics": {"org_id": org_id, "incidents": incidents}}
    records = find_recurrences(payload, config=_CONFIG, as_of=_AS_OF, org_id=org_id)
    assert len(records) == 1
    return records[0]


@dataclass
class _Chunk:
    content: str = "runbook body"
    similarity: float = 0.9
    source_system: str = "document"
    source_artifact: str = "runbooks/loan-close.md"
    chunk_id: str = "chunk-1"
    retrieval_result_id: str = "rr-1"
    source_timestamp: str = "2026-07-01T00:00:00+00:00"
    is_stale: bool = False


class _FakeRetrieve:
    """A stand-in for app.retrieval.api.retrieve that records how it was called."""

    def __init__(self, result=None, raise_exc: Exception | None = None):
        self.calls: list = []
        self._result = list(result) if result is not None else []
        self._raise = raise_exc

    def __call__(self, org_id, query_text, *, k, source_filter, min_score, include_stale):
        self.calls.append({
            "org_id": org_id, "query_text": query_text, "k": k,
            "source_filter": list(source_filter), "min_score": min_score,
            "include_stale": include_stale,
        })
        if self._raise is not None:
            raise self._raise
        return list(self._result)


def _run(rec, *, retrieve, available=True, config=None, redacted_texts=(), org_id="org-a"):
    return retrieve_runbook_candidates(
        org_id, rec, config=config, redacted_texts=redacted_texts,
        retrieve_fn=retrieve, embedding_available_fn=lambda q: available,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic query construction from the resolution pattern.
# ─────────────────────────────────────────────────────────────────────────────


class TestQueryConstruction:
    def test_query_contains_the_structured_resolution_pattern(self):
        rec = _recurrence()
        query = build_resolution_query(rec)
        assert "software" in query                 # category
        assert "solved (permanently)" in query     # close code (normalised)
        assert "cmdb_ci_server" in query           # CI class
        assert "platform operations" in query      # resolved-by group

    def test_query_is_deterministic(self):
        rec = _recurrence()
        assert build_resolution_query(rec) == build_resolution_query(rec)

    def test_incomplete_fields_add_no_placeholder_noise(self):
        # A recurrence with no close code / group / CI class still builds a clean
        # query from what exists — never "None", "", or a bare "class:" marker.
        rec = _recurrence(close_code="", group="", ci_class="")
        query = build_resolution_query(rec)
        assert "none" not in query.lower()
        assert "class:" not in query
        assert "ci:" not in query
        assert "  " not in query  # no doubled whitespace from skipped fields

    def test_redacted_text_is_appended(self):
        rec = _recurrence()
        query = build_resolution_query(rec, redacted_texts=["restart the mail relay"])
        assert "restart the mail relay" in query

    def test_query_omits_specific_ci_sys_id_noise(self):
        # When the pattern is keyed on a specific CI id (not a class), the opaque
        # sys_id must not leak into the query as noise.
        rec = _recurrence(ci_class="")  # forces ci_component to ci:<sys_id> or empty
        query = build_resolution_query(rec)
        assert "ci:" not in query


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — redacted note text only, org-scoped end to end.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC7RedactedAndOrgScoped:
    def test_seeded_secret_never_enters_the_query(self):
        # The seeded-secret test is UPSTREAM of the query builder: B4's redactor
        # strips the secret from the note, and only that redacted text is handed to
        # the builder. The builder itself has no path to raw notes.
        secret = "AKIAIOSFODNN7EXAMPLE"
        raw_note = f"Rotated the key {secret} and restarted the relay."
        redacted = scan_and_redact(raw_note)
        assert secret not in redacted.text            # B4 redacted it upstream
        assert redacted.pattern_types                 # something was redacted

        rec = _recurrence()
        query = build_resolution_query(rec, redacted_texts=[redacted.text])
        assert secret not in query                    # so it never reaches the query
        assert "restarted the relay" in query         # the safe text survives

    def test_query_carries_no_person_or_freetext_from_the_record(self):
        # The record holds no free text / short-description tokens, so nothing
        # person-like can appear in a query built from the record alone.
        rec = _recurrence()
        query = build_resolution_query(rec)
        assert "portal email service" not in query.lower()  # short-desc not on record

    def test_retrieval_is_org_scoped(self):
        rec = _recurrence(org_id="org-a")
        fake = _FakeRetrieve(result=[_Chunk()])
        _run(rec, retrieve=fake, org_id="org-a")
        assert fake.calls[0]["org_id"] == "org-a"

    def test_cross_org_recurrence_raises(self):
        rec = _recurrence(org_id="org-b")
        with pytest.raises(OrgScopeError):
            _run(rec, retrieve=_FakeRetrieve(), org_id="org-a")

    def test_missing_org_raises(self):
        rec = _recurrence(org_id="org-a")
        with pytest.raises(OrgScopeError):
            _run(rec, retrieve=_FakeRetrieve(), org_id="")


# ─────────────────────────────────────────────────────────────────────────────
# Runbook-library scope + configured limit / score / staleness.
# ─────────────────────────────────────────────────────────────────────────────


class TestScopeAndConfig:
    def test_query_is_restricted_to_the_runbook_library(self):
        rec = _recurrence()
        fake = _FakeRetrieve(result=[_Chunk()])
        _run(rec, retrieve=fake)
        source_filter = fake.calls[0]["source_filter"]
        assert source_filter == list(RUNBOOK_SCOPE)
        # The forbidden, unrelated sources are never in scope.
        for forbidden in ("slack", "git", "servicenow", "teams"):
            assert forbidden not in source_filter

    def test_default_limit_and_min_score_are_passed(self):
        rec = _recurrence()
        fake = _FakeRetrieve(result=[_Chunk()])
        _run(rec, retrieve=fake)
        assert fake.calls[0]["k"] == DEFAULT_CANDIDATE_LIMIT
        assert fake.calls[0]["min_score"] == DEFAULT_MIN_SCORE

    def test_config_overrides_limit_and_min_score(self):
        rec = _recurrence()
        fake = _FakeRetrieve(result=[_Chunk()])
        cfg = RunbookRetrievalConfig(candidate_limit=12, min_score=0.42)
        _run(rec, retrieve=fake, config=cfg)
        assert fake.calls[0]["k"] == 12
        assert fake.calls[0]["min_score"] == 0.42

    def test_stale_excluded_by_default(self):
        rec = _recurrence()
        fake = _FakeRetrieve(result=[_Chunk()])
        _run(rec, retrieve=fake)
        assert fake.calls[0]["include_stale"] is False

    def test_stale_surfaced_when_configured(self):
        rec = _recurrence()
        fake = _FakeRetrieve(result=[_Chunk()])
        _run(rec, retrieve=fake, config=RunbookRetrievalConfig(include_stale=True))
        assert fake.calls[0]["include_stale"] is True

    def test_config_from_env(self, monkeypatch):
        monkeypatch.setenv("MSP_B5_RUNBOOK_CANDIDATE_LIMIT", "9")
        monkeypatch.setenv("MSP_B5_RUNBOOK_MIN_SCORE", "0.55")
        monkeypatch.setenv("MSP_B5_RUNBOOK_INCLUDE_STALE", "true")
        monkeypatch.setenv("MSP_B5_RUNBOOK_SOURCE_SYSTEMS", "confluence,sharepoint")
        cfg = RunbookRetrievalConfig.from_env()
        assert cfg.candidate_limit == 9
        assert cfg.min_score == 0.55
        assert cfg.include_stale is True
        assert cfg.source_systems == ("confluence", "sharepoint")


# ─────────────────────────────────────────────────────────────────────────────
# Candidates returned + the unavailable-vs-empty distinction.
# ─────────────────────────────────────────────────────────────────────────────


class TestCandidatesAndAvailability:
    def test_successful_search_with_candidates(self):
        rec = _recurrence()
        chunk = _Chunk(source_artifact="runbooks/loan-close.md", similarity=0.88)
        result = _run(rec, retrieve=_FakeRetrieve(result=[chunk]))
        assert result.status == RETRIEVAL_OK
        assert result.available is True
        assert result.found_any is True
        assert result.candidates[0].source_artifact == "runbooks/loan-close.md"
        assert result.candidates[0].similarity == 0.88

    def test_genuine_empty_is_ok_not_unavailable(self):
        rec = _recurrence()
        # Search ran fine (provider available) but matched nothing.
        result = _run(rec, retrieve=_FakeRetrieve(result=[]), available=True)
        assert result.status == RETRIEVAL_OK
        assert result.available is True
        assert result.found_any is False

    def test_unavailable_provider_is_distinct_from_empty(self):
        rec = _recurrence()
        # Empty result AND the embedding provider is down -> unavailable, not "no match".
        result = _run(rec, retrieve=_FakeRetrieve(result=[]), available=False)
        assert result.status == RETRIEVAL_UNAVAILABLE
        assert result.available is False
        assert result.found_any is False

    def test_retrieval_failure_returns_unavailable_without_raising(self):
        rec = _recurrence()
        fake = _FakeRetrieve(raise_exc=RuntimeError("vector store down"))
        result = _run(rec, retrieve=fake)  # must not raise
        assert result.status == RETRIEVAL_UNAVAILABLE

    def test_embedding_probe_failure_is_unavailable(self):
        rec = _recurrence()

        def boom(_query):
            raise RuntimeError("gateway down")

        result = retrieve_runbook_candidates(
            "org-a", rec, retrieve_fn=_FakeRetrieve(result=[]),
            embedding_available_fn=boom,
        )
        assert result.status == RETRIEVAL_UNAVAILABLE

    def test_empty_query_short_circuits_without_calling_retrieve(self):
        # A recurrence with no usable structured fields and no redacted text yields
        # an empty query -> a predictable genuine-empty OK result, no search issued.
        rec = _recurrence(category="", close_code="", group="", ci_class="")
        fake = _FakeRetrieve(result=[_Chunk()])
        result = _run(rec, retrieve=fake)
        assert result.query == ""
        assert result.status == RETRIEVAL_OK
        assert result.found_any is False
        assert fake.calls == []  # retrieve never called
