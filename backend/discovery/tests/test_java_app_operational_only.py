"""
R17-A3 / T1 (AC8) — phase one reads the OPERATIONAL surface only, never source code.

This is the phase-one boundary: the ingestor reads what a *running* Java
application exposes about itself — Spring Boot Actuator health/metrics/info and
application logs — and nothing else. Reading the application's source code /
structure is the separate Release 1.8 code-and-structure story; external APM is a
possible later extension. These tests pin that the connector can only ever emit
the two operational surfaces, so the boundary cannot silently regress.
"""
from __future__ import annotations

import pytest

from discovery.ingest.java_app import OPERATIONAL_SURFACES, JavaAppIngestor


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    monkeypatch.setenv("INGEST_MODE", "offline")


def _all_records():
    return [r for b in JavaAppIngestor().ingest_changes("org1", None) for r in b.records]


def test_surfaces_are_exactly_actuator_and_logs():
    assert set(OPERATIONAL_SURFACES) == {"actuator", "logs"}


def test_no_source_code_surface_is_declared():
    # No source-code / structure surface is part of phase one.
    forbidden = {"source", "source_code", "code", "ast", "structure", "repo"}
    assert forbidden.isdisjoint(set(OPERATIONAL_SURFACES))


def test_every_emitted_record_is_an_operational_surface_only():
    records = _all_records()
    assert records
    for r in records:
        assert r["surface"] in OPERATIONAL_SURFACES
        # Defensive: nothing source-code-derived rides along on a record.
        keys = set(r)
        assert keys.isdisjoint({"source_code", "ast", "file_contents", "repo_path"})


def test_phase_one_boundary_is_documented():
    # AC8 is a design-review criterion: the exclusion must be stated in the module.
    import discovery.ingest.java_app as mod

    doc = (mod.__doc__ or "").lower()
    assert "source code" in doc
    assert "1.8" in doc  # points at the later code-and-structure phase
