"""MSP-B5 T1 — explicit-citation runbook matching contract tests.

Covers the acceptance criteria that belong to T1:

  * AC1 — a recurrence whose notes cite a runbook resolves to an OBSERVED
          RunbookMatch deterministically, with evidence pointing at the citing
          incidents AND the runbook page/document.
  * AC7 — every lookup is org-scoped end to end: a citation from one org never
          resolves against an identically named/linked runbook in another org.

Plus the no-guess discipline the task mandates: a missing, invalid, ambiguous,
or unresolvable citation yields NO observed match (never a fuzzy/semantic guess —
that is MSP-B5 T2/T3). Pure-Python and offline: no ServiceNow credentials and no
contract DB, so it runs alongside the other MSP signal tests.
"""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from app.provenance import EvidencePointer  # noqa: E402
from discovery.detectors.ops_recurrence import (  # noqa: E402
    RecurrenceConfig,
    find_recurrences,
)
from discovery.detectors.runbook_match import (  # noqa: E402
    MATCH_OBSERVED,
    InMemoryRunbookLibrary,
    RetrievalRunbookLibrary,
    RunbookPage,
    match_runbooks,
    normalize_reference,
)
from discovery.signals.evidence_store import OrgScopeError  # noqa: E402
from discovery.signals.resolution_signature import (  # noqa: E402
    compute_incident_identity_signature,
    compute_resolution_signature,
)

_AS_OF = "2026-07-15 12:00:00"
_CONFIG = RecurrenceConfig(floor=3, window_days=30, max_examples=3)

_KB = "KB0010234"
_RUNBOOK_URL = "https://runbooks.acme.gov/loan/close"


# ─────────────────────────────────────────────────────────────────────────────
# Fixture builders — deterministic incidents whose resolution notes cite runbooks.
# ─────────────────────────────────────────────────────────────────────────────


def _incident(
    number: int,
    *,
    org_id: str = "org-a",
    category: str = "software",
    close_code: str = "Solved (Permanently)",
    ci_class: str = "cmdb_ci_server",
    group: str = "Platform Operations",
    short_description: str = "Portal email service unavailable",
    resolved_at: str = "2026-07-10 12:00:00",
    ttr: int = 3600,
    runbook_refs: tuple = (),
) -> dict:
    sys_id = f"incident-sys-{number:04d}"
    identity_signature = compute_incident_identity_signature(
        category=category, short_description=short_description, ci_class=ci_class
    )
    resolution_signature = compute_resolution_signature(
        category=category, close_code=close_code,
        resolved_by_group=group, ci_class=ci_class,
    )
    evidence = EvidencePointer.observed(
        source_system="servicenow",
        source_artifact=sys_id,
        source_timestamp=resolved_at,
        source_artifact_type="record_id",
    ).to_dict()
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
            "incident_identity_signature": identity_signature,
            "resolution_signature": resolution_signature,
            "incident_sys_id": sys_id,
            "evidence": evidence,
            "notes_evidence": dict(evidence),
            "runbook_references": list(runbook_refs),
        },
    }


def _payload(*incidents: dict, org_id: str = "org-a") -> dict:
    return {
        "org_id": org_id,
        "incident_metrics": {"org_id": org_id, "incidents": list(incidents)},
    }


def _recurrence(org_id: str = "org-a", runbook_refs: tuple = (_KB,), **kw):
    """A single recurrence of three matching incidents citing ``runbook_refs``."""
    incidents = [
        _incident(1, org_id=org_id, resolved_at="2026-07-10 12:00:00",
                  runbook_refs=runbook_refs, **kw),
        _incident(2, org_id=org_id, resolved_at="2026-07-12 12:00:00",
                  runbook_refs=runbook_refs, **kw),
        _incident(3, org_id=org_id, resolved_at="2026-07-14 12:00:00",
                  runbook_refs=runbook_refs, **kw),
    ]
    records = find_recurrences(
        _payload(*incidents, org_id=org_id), config=_CONFIG, as_of=_AS_OF, org_id=org_id
    )
    assert len(records) == 1
    return records[0]


def _library(*pages: RunbookPage) -> InMemoryRunbookLibrary:
    return InMemoryRunbookLibrary(pages)


def _runbook_page(org_id: str = "org-a", **kw) -> RunbookPage:
    kw.setdefault("source_system", "document")
    kw.setdefault("source_artifact", "runbooks/loan-close.md")
    kw.setdefault("identifiers", (_KB,))
    kw.setdefault("title", "Loan Close Runbook")
    return RunbookPage(org_id=org_id, **kw)


# ─────────────────────────────────────────────────────────────────────────────
# MSP-B4 seam — the recurrence surfaces the captured citations for T1.
# ─────────────────────────────────────────────────────────────────────────────


class TestRecurrenceSurfacesCitations:
    def test_cited_refs_and_citations_flow_onto_record(self):
        rec = _recurrence(runbook_refs=(_KB,))
        assert rec.cited_runbook_refs == (_KB,)
        assert len(rec.runbook_citations) == 3
        assert {c["incident_sys_id"] for c in rec.runbook_citations} == {
            "incident-sys-0001", "incident-sys-0002", "incident-sys-0003",
        }
        pointers = rec.citing_incident_pointers()
        assert len(pointers) == 3
        assert all(EvidencePointer.from_dict(p).is_valid() for p in pointers)

    def test_recurrence_without_citations_carries_none(self):
        rec = _recurrence(runbook_refs=())
        assert rec.cited_runbook_refs == ()
        assert rec.runbook_citations == ()
        assert rec.citing_incident_pointers() == ()


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — deterministic observed match, both-sided evidence.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC1ObservedCitationMatch:
    def test_citation_resolves_to_observed_match(self):
        rec = _recurrence(runbook_refs=(_KB,))
        library = _library(_runbook_page())
        match = match_runbooks("org-a", rec, library)

        assert match is not None
        assert match.match_state == MATCH_OBSERVED
        assert match.origin == "observed"
        assert match.match_confidence is None  # observed, not a scored proposal
        assert match.org_id == "org-a"
        assert match.recurrence_id == rec.record_id
        assert _KB in match.cited_references

    def test_evidence_points_at_both_the_incidents_and_the_runbook_page(self):
        rec = _recurrence(runbook_refs=(_KB,))
        library = _library(_runbook_page())
        match = match_runbooks("org-a", rec, library)

        # Resolved side: the exact runbook page/document.
        assert match.runbook["source_system"] == "document"
        assert match.runbook["source_artifact"] == "runbooks/loan-close.md"
        runbook_ptr = EvidencePointer.from_dict(match.runbook_evidence)
        assert runbook_ptr.is_valid()
        assert runbook_ptr.origin == "observed"
        assert runbook_ptr.source_artifact == "runbooks/loan-close.md"

        # Citing side: the incidents that named the runbook.
        assert len(match.citing_incident_evidence) == 3
        artifacts = {p["source_artifact"] for p in match.citing_incident_evidence}
        assert artifacts == {
            "incident-sys-0001", "incident-sys-0002", "incident-sys-0003",
        }
        assert all(
            EvidencePointer.from_dict(p).is_valid()
            for p in match.citing_incident_evidence
        )

    def test_match_is_deterministic(self):
        rec = _recurrence(runbook_refs=(_KB,))
        library = _library(_runbook_page())
        first = match_runbooks("org-a", rec, library).as_dict()
        second = match_runbooks("org-a", rec, library).as_dict()
        assert first == second

    def test_resolves_by_url_citation(self):
        rec = _recurrence(runbook_refs=(_RUNBOOK_URL,))
        page = _runbook_page(identifiers=(_KB,), url=_RUNBOOK_URL)
        match = match_runbooks("org-a", rec, _library(page))
        assert match is not None
        assert match.runbook["url"] == _RUNBOOK_URL

    def test_resolves_by_page_id_source_artifact(self):
        rec = _recurrence(runbook_refs=("runbooks/loan-close.md",))
        # The cited reference IS the runbook's stable page id (source_artifact).
        page = _runbook_page(identifiers=())
        match = match_runbooks("org-a", rec, _library(page))
        assert match is not None
        assert match.runbook["source_artifact"] == "runbooks/loan-close.md"


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — org-scoped end to end.
# ─────────────────────────────────────────────────────────────────────────────


class TestAC7OrgScoping:
    def test_citation_never_resolves_against_another_orgs_runbook(self):
        rec = _recurrence(org_id="org-a", runbook_refs=(_KB,))
        # The identically-named runbook belongs to org-b only.
        library = _library(_runbook_page(org_id="org-b"))
        assert match_runbooks("org-a", rec, library) is None

    def test_each_org_resolves_only_its_own_runbook(self):
        page_a = _runbook_page(org_id="org-a", source_artifact="runbooks/a.md")
        page_b = _runbook_page(org_id="org-b", source_artifact="runbooks/b.md")
        library = _library(page_a, page_b)

        rec_a = _recurrence(org_id="org-a", runbook_refs=(_KB,))
        match_a = match_runbooks("org-a", rec_a, library)
        assert match_a.runbook["source_artifact"] == "runbooks/a.md"

    def test_cross_org_recurrence_raises(self):
        rec = _recurrence(org_id="org-b", runbook_refs=(_KB,))
        with pytest.raises(OrgScopeError):
            match_runbooks("org-a", rec, _library(_runbook_page(org_id="org-a")))

    def test_missing_org_raises(self):
        rec = _recurrence(org_id="org-a", runbook_refs=(_KB,))
        with pytest.raises(OrgScopeError):
            match_runbooks("", rec, _library(_runbook_page()))


# ─────────────────────────────────────────────────────────────────────────────
# No-guess discipline — missing / invalid / ambiguous / unresolvable => no match.
# ─────────────────────────────────────────────────────────────────────────────


class TestNoObservedMatch:
    def test_no_citation_yields_no_match(self):
        rec = _recurrence(runbook_refs=())
        assert match_runbooks("org-a", rec, _library(_runbook_page())) is None

    def test_unresolvable_citation_yields_no_match(self):
        rec = _recurrence(runbook_refs=("KB9999999",))  # not in the library
        assert match_runbooks("org-a", rec, _library(_runbook_page())) is None

    def test_near_miss_identifier_does_not_fuzzy_match(self):
        # One digit off — deterministic exact matching must NOT resolve it.
        rec = _recurrence(runbook_refs=("KB0010235",))
        assert match_runbooks("org-a", rec, _library(_runbook_page())) is None

    def test_ambiguous_single_citation_yields_no_match(self):
        # The same cited id declared by two DISTINCT runbook pages -> ambiguous.
        page_1 = _runbook_page(source_artifact="runbooks/one.md")
        page_2 = _runbook_page(source_artifact="runbooks/two.md")
        rec = _recurrence(runbook_refs=(_KB,))
        assert match_runbooks("org-a", rec, _library(page_1, page_2)) is None

    def test_citations_to_two_distinct_runbooks_yield_no_match(self):
        rec = _recurrence(runbook_refs=(_KB, "KB0010500"))
        page_1 = _runbook_page(source_artifact="runbooks/one.md", identifiers=(_KB,))
        page_2 = _runbook_page(source_artifact="runbooks/two.md",
                               identifiers=("KB0010500",))
        assert match_runbooks("org-a", rec, _library(page_1, page_2)) is None

    def test_invalid_reference_is_ignored(self):
        rec = _recurrence(runbook_refs=("   ",))  # blank -> unusable
        assert match_runbooks("org-a", rec, _library(_runbook_page())) is None


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation + the retrieval-backed library (provenance join, no DB).
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalization:
    def test_identifier_tokens_normalise_case_insensitively(self):
        assert normalize_reference("kb0010234") == normalize_reference("KB0010234")
        assert normalize_reference("KB0010234") == "id:KB0010234"

    def test_urls_canonicalise(self):
        assert normalize_reference("https://X.gov/A/") == normalize_reference(
            "https://x.gov/A"
        )

    def test_kinds_do_not_collide(self):
        # A token and a same-spelled free name resolve to different key spaces.
        assert normalize_reference("KB0010234") != normalize_reference("kb 0010234")

    def test_empty_reference_is_none(self):
        assert normalize_reference("") is None
        assert normalize_reference(None) is None


class TestRetrievalRunbookLibrary:
    def _reader(self, rows_by_org):
        def read(org_id, source_systems):
            return rows_by_org.get(org_id, [])
        return read

    def test_builds_pages_from_substrate_provenance(self):
        rows = {
            "org-a": [
                {
                    "source_system": "document",
                    "source_artifact": "runbooks/loan-close.md",
                    "source_timestamp": "2026-07-01 00:00:00",
                    "chunk_id": "chunk-1",
                    "provenance": {"runbook_id": _KB, "title": "Loan Close"},
                }
            ]
        }
        library = RetrievalRunbookLibrary(provenance_reader=self._reader(rows))
        rec = _recurrence(runbook_refs=(_KB,))
        match = match_runbooks("org-a", rec, library)
        assert match is not None
        assert match.runbook["source_artifact"] == "runbooks/loan-close.md"

    def test_retrieval_library_is_org_partitioned(self):
        rows = {
            "org-b": [
                {
                    "source_system": "document",
                    "source_artifact": "runbooks/loan-close.md",
                    "provenance": {"runbook_id": _KB},
                }
            ]
        }
        library = RetrievalRunbookLibrary(provenance_reader=self._reader(rows))
        rec = _recurrence(org_id="org-a", runbook_refs=(_KB,))
        assert match_runbooks("org-a", rec, library) is None

    def test_substrate_failure_degrades_to_no_match(self):
        def boom(org_id, source_systems):
            raise RuntimeError("substrate down")

        library = RetrievalRunbookLibrary(provenance_reader=boom)
        rec = _recurrence(runbook_refs=(_KB,))
        # A failing library read must never crash the run — it degrades to no
        # observed match (the documentation-gap path), never an exception.
        assert match_runbooks("org-a", rec, library) is None
