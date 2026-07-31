"""R191-R1 T6 - CI guard for registry/catalog entries vs shipped ingestors.

AC1 requires every registry system_default and every connectable catalog tile to
reference a connector whose ingestion actually ships. The guard below discovers
implemented ingestors from the codebase, then checks the registry and catalog
configuration against that implementation set. If a roadmap connector is flipped
to shipped/connectable before its ingestor lands, CI fails here.

2.0-D1 T5 — THE PACK-LEVEL GATE
-------------------------------
The connector-level check above answers "can we authenticate and read from this
org?". It resolves the domain-specific Salesforce product ids through
``IMPLEMENTATION_ALIASES``, where ``salesforce_fsc`` maps to ``{salesforce}`` — so
because the base Salesforce ingestor ships, the FSC entry counted as backed.

That left a real hole. Before 2.0-D1, ``salesforce_fsc`` sat in
``INDUSTRY_REGISTRY['financial_services'].system_defaults`` as a PRIMARY
``system_of_record`` and this file was GREEN, while no FSC pack, no FSC detectors
and no FSC ingest existed. The alias answered the connector question and hid the
capability question.

``TestPackLevelCapabilityGate`` closes it: a domain-specific product id presented
as connectable must ALSO declare a pack (``app/salesforce_product_packs.py``) and
that pack must be registered. The declaration distinguishes a DEDICATED domain pack
from a deliberate GENERIC one, so Revenue Cloud and Health Cloud stay honestly
green while an undeclared or non-existent pack fails.

Release 2.0 Definition-of-Done item 1 — "catalog honesty holds" — is demonstrated
by this file being green, so the gate is also proven to FAIL: see
``test_gate_goes_red_when_a_declared_pack_is_unregistered``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Iterable

import pytest

from app import connector_roadmap
from app import salesforce_product_packs as spp
from discovery.packs.industry_registry import INDUSTRY_REGISTRY
from discovery.packs.pack_config import PACK_REGISTRY, list_packs

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DISCOVERY_INGEST_ROOT = BACKEND_ROOT / "discovery" / "ingest"
DB_INGEST_ROOT = BACKEND_ROOT / "connectors" / "db"
CATALOG_SEED = BACKEND_ROOT / "database" / "seed" / "connectors.json"

STRING_ID_ASSIGNMENTS = {"connector_id", "CONNECTOR_ID"}

# Catalog/registry ids that are product aliases for an already-shipped ingestor.
# Keeping this mapping small and explicit means adding a real connector module
# for a roadmap id automatically permits flipping that id to shipped/connectable.
#
# NOTE (2.0-D1 T5): these aliases answer the CONNECTOR question only — every
# Salesforce product is reachable through the shipped base Salesforce ingestor. They
# deliberately do NOT answer whether that product's own objects and detectors ship.
# ``TestPackLevelCapabilityGate`` below is what enforces that, so the alias no
# longer lets a product claim domain capability it does not have.
IMPLEMENTATION_ALIASES = {
    "github": {"git_content"},
    "salesforce_sc": {"salesforce"},
    "salesforce_ncino": {"salesforce", "ncino"},
    "salesforce_fsc": {"salesforce", "fsc"},
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


def _registry_system_ids() -> set[str]:
    """Every system id the industry registry presents as connectable."""
    ids: set[str] = set()
    for config in INDUSTRY_REGISTRY.values():
        ids.update(config.system_defaults)
        ids.update(config.recommended_systems)
    return ids


class TestPackLevelCapabilityGate:
    """2.0-D1 T5 — the entry may not claim more capability than the code has."""

    def test_every_presented_salesforce_product_declares_a_pack(self):
        """A domain-specific product presented as connectable must say which pack
        serves it. An undeclared one is claiming a capability nobody wrote down."""
        undeclared = spp.undeclared_products(_registry_system_ids())
        assert undeclared == [], (
            "These Salesforce product ids are presented as connectable in the "
            "industry registry but declare no pack in "
            "app/salesforce_product_packs.py. Declare the pack that serves each "
            f"(DEDICATED or GENERIC, with a reason), or remove the entry: {undeclared}"
        )

    def test_every_declared_pack_is_registered(self):
        """THE GATE. A product may not name a pack that does not exist.

        This is what goes red if ``financial_services_cloud`` is removed from
        PACK_REGISTRY while ``salesforce_fsc`` is still a primary system_of_record
        for financial_services.
        """
        missing = spp.products_naming_unregistered_packs(list_packs())
        assert missing == [], (
            "These Salesforce products declare a pack that is NOT registered in "
            "pack_config.PACK_REGISTRY — the entry claims a capability the codebase "
            f"does not have: {missing}"
        )

    def test_gate_goes_red_when_a_declared_pack_is_unregistered(self):
        """Prove the gate IS a gate.

        A gate that has never been observed failing is not known to be a gate, so
        this drives the exact scenario T1 fixed: the FSC pack absent while
        ``salesforce_fsc`` is still presented as connectable. The check must go red.
        Uses the pure helper against a pack list with FSC removed, so nothing is
        mutated and the proof does not depend on editing the real registry.
        """
        packs_without_fsc = [p for p in list_packs() if p != "financial_services_cloud"]
        missing = spp.products_naming_unregistered_packs(packs_without_fsc)
        assert "salesforce_fsc" in missing, (
            "removing the FSC pack did NOT trip the gate — the check is not "
            "actually enforcing pack-level capability"
        )

    def test_gate_goes_red_for_an_undeclared_product(self):
        """The other failure mode: a new product id presented without a declaration."""
        undeclared = spp.undeclared_products(
            _registry_system_ids() | {"salesforce_brand_new_cloud"}
        )
        assert undeclared == ["salesforce_brand_new_cloud"]

    def test_fsc_is_backed_by_a_dedicated_pack_not_a_generic_fallback(self):
        """FSC specifically: after T1-T4 it must be DEDICATED, not generic."""
        declared = spp.get_product_pack("salesforce_fsc")
        assert declared is not None
        assert declared.pack_id == "financial_services_cloud"
        assert declared.is_dedicated, (
            "salesforce_fsc is declared generic — but FSC ships its own ingest, "
            "detectors and scorer, so it must claim its own pack"
        )

    def test_the_fsc_pack_really_ships_detectors_and_a_scorer(self):
        """The declaration must not outrun the implementation."""
        assert PACK_REGISTRY["financial_services_cloud"]["detectors"], (
            "the FSC pack is registered but ships no detectors"
        )
        from discovery.packs import financial_services_cloud_scorer as scorer

        assert scorer.FSC_DETECTOR_IDS, "the FSC pack ships no scored detectors"

    def test_the_fsc_ingest_ships(self):
        """And the ingest the connector-level alias now names."""
        from discovery.ingest import fsc

        assert hasattr(fsc, "ingest")
        assert any("FinServ__" in soql for _sobject, soql in fsc._LIVE_QUERIES), (
            "the FSC ingest reads no FinServ__ objects — the capability claim is "
            "not backed"
        )

    def test_generic_declarations_are_recorded_not_hidden(self):
        """Products still served generically must be visible, with a reason.

        These are pre-existing, deliberate scope decisions (the Health Cloud pack is
        2.0.1). The dishonesty this subtask targets is an UNDECLARED claim, so the
        gate requires the declaration rather than failing the decision.
        """
        pending = spp.pending_domain_pack()
        assert "salesforce_hc" in pending and "salesforce_rc" in pending
        assert "salesforce_fsc" not in pending
        for product_id in pending:
            declared = spp.get_product_pack(product_id)
            assert declared.reason.strip(), f"{product_id} has no stated reason"
            assert declared.pack_id in set(list_packs())

    def test_the_alias_map_and_the_pack_declaration_cover_the_same_products(self):
        """The two halves of the rule must not drift apart: every aliased Salesforce
        product needs a pack declaration and vice versa."""
        aliased = {k for k in IMPLEMENTATION_ALIASES if spp.is_salesforce_product(k)}
        declared = set(spp.declared_product_ids())
        assert aliased == declared, (
            f"alias-only: {sorted(aliased - declared)}, "
            f"declaration-only: {sorted(declared - aliased)}"
        )

    def test_base_salesforce_connector_is_not_treated_as_a_product(self):
        """``salesforce`` is the catalog tile, covered by the connector-level check."""
        assert spp.is_salesforce_product("salesforce") is False
        assert spp.get_product_pack("salesforce") is None


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
