"""The backend and frontend source-key registries must not drift.

``frontend/src/utils/sourceKeys.ts`` maps ``connector.id`` → display
``sourceSystem`` and the Source Intelligence page joins rows to connectors by
string equality on that value. ``backend/app/source_keys.py`` labels the rows.
If the two maps disagree for a connector, rows for it are silently invisible in
the UI (no error, just a source that reads "no signals") — exactly the failure
mode this pairing exists to prevent.

These tests parse the TypeScript literal rather than mirroring it, so adding a
connector on one side only fails the build without any test edit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.source_keys import CONNECTOR_ID_ALIASES, SOURCE_KEY_MAP, source_key_for

_TS_REGISTRY = (
    Path(__file__).resolve().parents[3]
    / "frontend" / "src" / "utils" / "sourceKeys.ts"
)


def _parse_ts_object(literal_name: str) -> dict:
    """Extract a `export const NAME ... = { ... }` string→string map from the TS file."""
    text = _TS_REGISTRY.read_text(encoding="utf-8")
    start = text.index(literal_name)
    body_start = text.index("{", start)
    body_end = text.index("};", body_start)
    body = text[body_start + 1:body_end]

    # Strip line comments so commented-out entries never count.
    body = re.sub(r"//[^\n]*", "", body)

    pairs = re.findall(r'([A-Za-z0-9_]+)\s*:\s*"([^"]*)"', body)
    return {k: v for k, v in pairs}


@pytest.fixture(scope="module")
def ts_source_key_map() -> dict:
    if not _TS_REGISTRY.exists():
        pytest.skip(f"frontend registry not present at {_TS_REGISTRY}")
    return _parse_ts_object("SOURCE_KEY_MAP")


def test_frontend_registry_is_parseable(ts_source_key_map: dict) -> None:
    """Guard the parser itself — an empty parse would make every check vacuous."""
    assert len(ts_source_key_map) >= 10
    assert ts_source_key_map.get("servicenow") == "ServiceNow"


def test_source_key_maps_are_identical(ts_source_key_map: dict) -> None:
    missing_in_backend = set(ts_source_key_map) - set(SOURCE_KEY_MAP)
    missing_in_frontend = set(SOURCE_KEY_MAP) - set(ts_source_key_map)

    assert not missing_in_backend, (
        "connectors declared in frontend/src/utils/sourceKeys.ts but absent from "
        f"backend/app/source_keys.py: {sorted(missing_in_backend)} — rows for them "
        "would be labelled with the raw connector id and never join in the UI."
    )
    assert not missing_in_frontend, (
        "connectors declared in backend/app/source_keys.py but absent from "
        f"frontend/src/utils/sourceKeys.ts: {sorted(missing_in_frontend)}"
    )

    mismatched = {
        key: (SOURCE_KEY_MAP[key], ts_source_key_map[key])
        for key in SOURCE_KEY_MAP
        if SOURCE_KEY_MAP[key] != ts_source_key_map[key]
    }
    assert not mismatched, (
        f"source key mismatch (backend, frontend): {mismatched}"
    )


def test_aliases_match_frontend(ts_source_key_map: dict) -> None:
    ts_aliases = _parse_ts_object("CONNECTOR_ID_ALIASES")
    assert CONNECTOR_ID_ALIASES == ts_aliases


def test_source_key_for_resolves_aliases_and_unknowns() -> None:
    assert source_key_for("servicenow") == "ServiceNow"
    assert source_key_for("azure_events") == "Azure Events"
    # Alias resolves to its canonical connector's display name.
    assert source_key_for("jira_confluence") == "Jira"
    # An unregistered connector falls back to its own id — the same fallback
    # sourceKeyForConnector applies, so both sides still agree on the join key.
    assert source_key_for("brand_new_connector") == "brand_new_connector"
    assert source_key_for("") == ""
