"""R191-R1 T6 - CI guard for registry/catalog entries vs shipped ingestors.

AC1 requires every registry system_default and every connectable catalog tile to
reference a connector whose ingestion actually ships. The guard below discovers
implemented ingestors from the codebase, then checks the registry and catalog
configuration against that implementation set. If a roadmap connector is flipped
to shipped/connectable before its ingestor lands, CI fails here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Iterable

from app import connector_roadmap
from discovery.packs.industry_registry import INDUSTRY_REGISTRY

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_INGEST_ROOT = BACKEND_ROOT / "discovery" / "ingest"
DB_INGEST_ROOT = BACKEND_ROOT / "connectors" / "db"
CATALOG_SEED = BACKEND_ROOT / "database" / "seed" / "connectors.json"

STRING_ID_ASSIGNMENTS = {"connector_id", "CONNECTOR_ID"}

# Catalog/registry ids that are product aliases for an already-shipped ingestor.
# Keeping this mapping small and explicit means adding a real connector module
# for a roadmap id automatically permits flipping that id to shipped/connectable.
IMPLEMENTATION_ALIASES = {
    "github": {"git_content"},
    "salesforce_sc": {"salesforce"},
    "salesforce_ncino": {"salesforce", "ncino"},
    "salesforce_fsc": {"salesforce"},
    "salesforce_pss": {"salesforce"},
    "salesforce_rc": {"salesforce"},
    "salesforce_hc": {"salesforce"},
    "sql_server": {"sqlserver"},
}


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _assignment_name(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if any(part in {"__pycache__", "fixtures", "tests"} for part in path.parts):
            continue
        if path.name == "__init__.py":
            continue
        yield path


def _discover_ingestor_ids(root: Path) -> set[str]:
    """Discover explicit connector ids and legacy function-style ingestor names."""
    discovered: set[str] = set()
    for path in _iter_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        has_top_level_ingest = False

        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "ingest":
                has_top_level_ingest = True
            if isinstance(node, ast.Assign):
                value = _literal_string(node.value)
                if value is None:
                    continue
                for target in node.targets:
                    if _assignment_name(target) in STRING_ID_ASSIGNMENTS:
                        discovered.add(value)
            elif isinstance(node, ast.AnnAssign):
                value = _literal_string(node.value) if node.value else None
                if value and _assignment_name(node.target) in STRING_ID_ASSIGNMENTS:
                    discovered.add(value)

        # The original SaaS ingestors are module-level ingest() implementations
        # and do not declare a connector_id constant. Their module stem is the
        # stable connector id used by the registry/catalog.
        if has_top_level_ingest:
            discovered.add(path.stem)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, ast.Assign):
                    value = _literal_string(item.value)
                    if value is None:
                        continue
                    for target in item.targets:
                        if _assignment_name(target) in STRING_ID_ASSIGNMENTS:
                            discovered.add(value)
                elif isinstance(item, ast.AnnAssign):
                    value = _literal_string(item.value) if item.value else None
                    if value and _assignment_name(item.target) in STRING_ID_ASSIGNMENTS:
                        discovered.add(value)

    return discovered


def _implemented_connector_ids() -> set[str]:
    # R191-R1 names backend/discovery/ingest as the source for shipped ingestors.
    # Native DB ingestors ship in backend/connectors/db, so include that ingestor
    # package as well instead of hardcoding database connector ids.
    return (
        _discover_ingestor_ids(DISCOVERY_INGEST_ROOT)
        | _discover_ingestor_ids(DB_INGEST_ROOT)
    )


def _implementation_keys(connector_id: str) -> set[str]:
    return {connector_id, *IMPLEMENTATION_ALIASES.get(connector_id, set())}


def _missing_implementation(connector_id: str, implemented: set[str]) -> bool:
    return not (_implementation_keys(connector_id) & implemented)


def _catalog_ids() -> set[str]:
    return {
        str(row["id"])
        for row in json.loads(CATALOG_SEED.read_text(encoding="utf-8"))
    }


def _format_failures(rows: Iterable[tuple[str, str, str]]) -> str:
    return ", ".join(
        f"{surface}:{owner}:{connector_id}"
        for surface, owner, connector_id in rows
    )


def test_registry_connectable_entries_have_shipped_ingestors():
    implemented = _implemented_connector_ids()
    missing: list[tuple[str, str, str]] = []

    for industry_id, config in INDUSTRY_REGISTRY.items():
        for connector_id in config.system_defaults:
            if _missing_implementation(connector_id, implemented):
                missing.append(("system_default", industry_id, connector_id))
        for connector_id in config.recommended_systems:
            if _missing_implementation(connector_id, implemented):
                missing.append(("recommended_system", industry_id, connector_id))

    assert missing == [], (
        "Registry connectable entries must reference shipped ingestors discovered "
        "from backend/discovery/ingest/ and backend/connectors/db/. Missing: "
        f"{_format_failures(missing)}"
    )


def test_connectable_catalog_tiles_have_shipped_ingestors():
    implemented = _implemented_connector_ids()
    catalog_ids = _catalog_ids()
    shipped_catalog_ids = set(connector_roadmap.SHIPPED_CONNECTOR_IDS)

    missing_from_catalog = sorted(shipped_catalog_ids - catalog_ids)
    assert missing_from_catalog == [], (
        "SHIPPED_CONNECTOR_IDS must only name real catalog tiles. Missing from "
        f"connectors.json: {missing_from_catalog}"
    )

    missing_ingestors = sorted(
        connector_id
        for connector_id in shipped_catalog_ids
        if _missing_implementation(connector_id, implemented)
    )
    assert missing_ingestors == [], (
        "Connectable catalog tiles must reference shipped ingestors discovered "
        "from backend/discovery/ingest/ and backend/connectors/db/. Missing: "
        f"{missing_ingestors}"
    )


def test_unimplemented_catalog_tiles_stay_roadmap_not_connectable():
    implemented = _implemented_connector_ids()
    catalog_ids = _catalog_ids()

    unimplemented_tiles_marked_shipped = sorted(
        connector_id
        for connector_id in catalog_ids
        if _missing_implementation(connector_id, implemented)
        and connector_roadmap.is_shipped(connector_id)
    )
    assert unimplemented_tiles_marked_shipped == [], (
        "A catalog tile without a shipped ingestor must stay roadmap/non-connectable "
        f"until its implementation lands: {unimplemented_tiles_marked_shipped}"
    )
