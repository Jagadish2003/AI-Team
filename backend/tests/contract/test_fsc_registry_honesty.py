"""Contract tests for 2.0-D1 T5 — registry honesty for FSC.

Definition of done: the R191-R1 cross-check passes with FSC honestly classified in
both states — red when the entry claims FSC capability the codebase does not have,
green once T1-T4 land.

WHAT THIS SUBTASK ACTUALLY WAS
------------------------------
Not "add the FSC entry when the pack ships" — the entry was ALREADY there.
``salesforce_fsc`` has been in ``INDUSTRY_REGISTRY['financial_services']
.system_defaults`` as a PRIMARY ``system_of_record`` all along, ungated and not
roadmap-labelled, while no FSC pack, detectors or ingest existed. It passed the
R191-R1 guard because ``IMPLEMENTATION_ALIASES`` resolves ``salesforce_fsc`` to
``{salesforce}``, and the base Salesforce ingestor ships.

That alias answers the CONNECTOR question ("can we authenticate and read from this
org?") but not the PACK question ("do we read FSC objects and detect FSC
patterns?"). So the work was TIGHTENING THE GATE, not adding an entry.

The gate itself lives in ``test_r191_r1_ingestor_registry_enforcement.py``
(``TestPackLevelCapabilityGate``) so it sits with the check the release
Definition-of-Done item 1 names. This file covers the surrounding obligations:

  * the gate distinguishes connector-ships from capability-ships;
  * the gate has been OBSERVED FAILING (a gate never seen to fail is not known to
    be a gate);
  * all the surfaces that can present a system as connectable AGREE — a registry
    entry correctly gated while another surface still over-claims is the same
    dishonesty relocated.
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest


def _repo_root() -> Path:
    marker = Path("backend") / "app" / "salesforce_product_packs.py"
    for candidate in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        if (candidate / marker).is_file():
            return candidate
    import app.salesforce_product_packs as spp
    return Path(spp.__file__).resolve().parents[2]


REPO_ROOT = _repo_root()
BACKEND_ROOT = REPO_ROOT / "backend"
CATALOG_SEED = BACKEND_ROOT / "database" / "seed" / "connectors.json"
FRONTEND_STACK_BUILDER = REPO_ROOT / "frontend" / "src" / "pages" / "StackBuilderPage.tsx"
FRONTEND_CONNECTOR_TILE = (
    REPO_ROOT / "frontend" / "src" / "components" / "integrations" / "ConnectorTile.tsx"
)

FSC_PRODUCT_ID = "salesforce_fsc"
FSC_PACK_ID = "financial_services_cloud"
BASE_CONNECTOR_ID = "salesforce"


def _spp():
    try:
        return importlib.import_module("app.salesforce_product_packs")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module("backend.app.salesforce_product_packs")


def _roadmap():
    try:
        return importlib.import_module("app.connector_roadmap")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module("backend.app.connector_roadmap")


def _pack_config():
    try:
        return importlib.import_module("discovery.packs.pack_config")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module("backend.discovery.packs.pack_config")


def _industries():
    try:
        return importlib.import_module("discovery.packs.industry_registry")
    except ModuleNotFoundError:  # pragma: no cover
        return importlib.import_module("backend.discovery.packs.industry_registry")


def _frontend_cloud_pack_registry() -> dict:
    """Parse ``CLOUD_PACK_REGISTRY`` out of StackBuilderPage.tsx.

    Parsed rather than duplicated so the assertion is against the value the
    frontend actually ships.
    """
    source = FRONTEND_STACK_BUILDER.read_text(encoding="utf-8")
    match = re.search(
        r"const CLOUD_PACK_REGISTRY:\s*Record<string,\s*string>\s*=\s*\{(.*?)\};",
        source,
        re.DOTALL,
    )
    assert match, "CLOUD_PACK_REGISTRY not found in StackBuilderPage.tsx"
    body = match.group(1)
    # Strip comments so a commented-out entry is never read as live config.
    body = re.sub(r"//[^\n]*", "", body)
    return {
        key: value
        for key, value in re.findall(r"(\w+)\s*:\s*'([^']+)'", body)
    }


def _frontend_enabled_connector_ids() -> list:
    source = FRONTEND_CONNECTOR_TILE.read_text(encoding="utf-8")
    match = re.search(r"const ENABLED_CONNECTOR_IDS\s*=\s*\[(.*?)\];", source, re.DOTALL)
    assert match, "ENABLED_CONNECTOR_IDS not found in ConnectorTile.tsx"
    return re.findall(r"'([^']+)'", match.group(1))


def _registry_system_ids() -> set:
    ids = set()
    for config in _industries().INDUSTRY_REGISTRY.values():
        ids.update(config.system_defaults)
        ids.update(config.recommended_systems)
    return ids


# ── The finding: the entry pre-dated the capability ────────────────────────────

class TestTheEntryWasAlreadyPresent:
    """Documents what T5 actually found, so the reasoning survives in the tests."""

    def test_fsc_is_a_primary_system_of_record_for_financial_services(self):
        config = _industries().INDUSTRY_REGISTRY["financial_services"]
        assert FSC_PRODUCT_ID in config.system_defaults
        default = config.system_defaults[FSC_PRODUCT_ID]
        assert default.role == "system_of_record"
        assert default.priority == "primary"

    def test_it_is_not_roadmap_labelled_because_it_is_not_a_catalog_tile(self):
        """The product ids are declarations WITHIN the salesforce connector, not
        tiles — which is why the roadmap overlay never gated them."""
        catalog_ids = {
            str(row["id"])
            for row in json.loads(CATALOG_SEED.read_text(encoding="utf-8"))
        }
        assert BASE_CONNECTOR_ID in catalog_ids
        assert FSC_PRODUCT_ID not in catalog_ids

    def test_the_connector_alias_alone_would_have_passed_it(self):
        """The base Salesforce ingestor ships, so a connector-level check is
        satisfied by the alias regardless of whether FSC capability exists — which
        is exactly why the pack-level gate had to be added."""
        aliases = _load_guard().IMPLEMENTATION_ALIASES
        assert BASE_CONNECTOR_ID in aliases[FSC_PRODUCT_ID]
        implemented = _load_guard()._implemented_connector_ids()
        assert BASE_CONNECTOR_ID in implemented


def _load_guard():
    """Load the R191-R1 guard module by path (it is a test module, not a package)."""
    import importlib.util
    path = BACKEND_ROOT / "tests" / "contract" / \
        "test_r191_r1_ingestor_registry_enforcement.py"
    spec = importlib.util.spec_from_file_location("_r191_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── The gate distinguishes connector-ships from capability-ships ───────────────

class TestGateDistinguishesConnectorFromCapability:

    def test_fsc_declares_a_dedicated_pack(self):
        declared = _spp().get_product_pack(FSC_PRODUCT_ID)
        assert declared is not None
        assert declared.pack_id == FSC_PACK_ID
        assert declared.is_dedicated

    def test_every_presented_product_declares_a_pack(self):
        assert _spp().undeclared_products(_registry_system_ids()) == []

    def test_every_declared_pack_is_registered(self):
        assert _spp().products_naming_unregistered_packs(
            _pack_config().list_packs()
        ) == []

    def test_the_declaration_is_not_satisfied_by_the_connector_alone(self):
        """The whole point: a shipped base connector must NOT make an FSC claim
        true. Removing the FSC pack leaves the connector alias intact, and the
        capability check must still fail."""
        spp = _spp()
        without_fsc = [
            p for p in _pack_config().list_packs() if p != FSC_PACK_ID
        ]
        assert FSC_PRODUCT_ID in spp.products_naming_unregistered_packs(without_fsc)

    def test_generic_products_are_declared_and_visible(self):
        """Revenue Cloud and Health Cloud have no domain pack. That is a recorded
        scope decision, not a hidden over-claim."""
        spp = _spp()
        pending = spp.pending_domain_pack()
        assert set(pending) == {"salesforce_rc", "salesforce_hc"}
        for product_id in pending:
            declared = spp.get_product_pack(product_id)
            assert declared.kind == spp.GENERIC
            assert declared.reason.strip()

    def test_health_cloud_reason_records_the_2_0_1_deferral(self):
        """The release's own out-of-scope list defers the Health Cloud pack; the
        declaration should say so rather than implying capability."""
        reason = _spp().get_product_pack("salesforce_hc").reason
        assert "2.0.1" in reason

    def test_fsc_is_no_longer_in_the_pending_set(self):
        assert FSC_PRODUCT_ID not in _spp().pending_domain_pack()


# ── The gate has been observed failing ────────────────────────────────────────

class TestTheGateIsProvenToFail:
    """A gate that has never been observed failing is not known to be a gate."""

    def test_unregistering_the_fsc_pack_trips_the_gate(self):
        spp = _spp()
        packs = _pack_config().list_packs()
        assert spp.products_naming_unregistered_packs(packs) == []
        without_fsc = [p for p in packs if p != FSC_PACK_ID]
        assert spp.products_naming_unregistered_packs(without_fsc) == [FSC_PRODUCT_ID]

    def test_an_undeclared_new_product_trips_the_gate(self):
        spp = _spp()
        assert spp.undeclared_products(
            _registry_system_ids() | {"salesforce_unbacked_cloud"}
        ) == ["salesforce_unbacked_cloud"]

    def test_a_declaration_naming_a_nonexistent_pack_trips_the_gate(self):
        spp = _spp()
        probe = spp.ProductPack(
            product_id="salesforce_probe",
            pack_id="no_such_pack",
            kind=spp.DEDICATED,
            reason="probe",
        )
        spp.SALESFORCE_PRODUCT_PACKS["salesforce_probe"] = probe
        try:
            assert "salesforce_probe" in spp.products_naming_unregistered_packs(
                _pack_config().list_packs()
            )
        finally:
            spp.SALESFORCE_PRODUCT_PACKS.pop("salesforce_probe", None)
        assert spp.products_naming_unregistered_packs(
            _pack_config().list_packs()
        ) == []

    def test_the_real_gate_module_exposes_the_negative_check(self):
        """The proof lives with the guard, not only here."""
        guard = _load_guard()
        assert hasattr(guard, "TestPackLevelCapabilityGate")
        names = dir(guard.TestPackLevelCapabilityGate)
        assert "test_gate_goes_red_when_a_declared_pack_is_unregistered" in names
        assert "test_gate_goes_red_for_an_undeclared_product" in names


class TestTheHonestyGateCannotBecomeANoOp:
    """Release Definition-of-Done item 1 is "catalog honesty holds", demonstrated by
    the R191-R1 cross-check being green in CI. That claim is only as strong as the
    tests actually RUNNING, so this pins the DoD-named checks against silently
    turning into skips.
    """

    DOD_CROSS_CHECKS = (
        "test_registry_connectable_entries_have_shipped_ingestors",
        "test_connectable_catalog_tiles_have_shipped_ingestors",
        "test_unimplemented_catalog_tiles_stay_roadmap_not_connectable",
    )

    def test_the_dod_cross_checks_exist(self):
        guard = _load_guard()
        for name in self.DOD_CROSS_CHECKS:
            assert callable(getattr(guard, name, None)), f"missing {name}"

    @staticmethod
    def _guard_block(name: str) -> str:
        """Return the source of a top-level function/class in the guard module.

        Read from the file via ``ast`` rather than ``inspect.getsource``, which is
        unreliable for objects belonging to a module loaded by path.
        """
        import ast

        path = (
            BACKEND_ROOT / "tests" / "contract"
            / "test_r191_r1_ingestor_registry_enforcement.py"
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        lines = source.splitlines()
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ) and node.name == name:
                return "\n".join(lines[node.lineno - 1: node.end_lineno])
        raise AssertionError(f"{name} not found in the R191-R1 guard module")

    def test_the_dod_cross_checks_do_not_skip(self):
        """None of them may contain a skip path — a check that skips in CI is not
        evidence of anything, and these are the ones the release leans on."""
        for name in self.DOD_CROSS_CHECKS:
            block = self._guard_block(name).lower()
            assert "skip" not in block, (
                f"{name} can skip — the DoD's honesty claim would be vacuous"
            )

    def test_the_pack_level_gate_does_not_skip_either(self):
        """The gate this subtask added must be held to the same standard."""
        block = self._guard_block("TestPackLevelCapabilityGate")
        assert "pytest.skip" not in block
        assert "skipif" not in block


# ── The adjacent surfaces agree ───────────────────────────────────────────────

class TestAllSurfacesAgree:
    """A registry entry correctly gated while another surface over-claims is the
    same dishonesty in a different place."""

    def test_surface_1_industry_registry_is_gated(self):
        assert _spp().undeclared_products(_registry_system_ids()) == []

    def test_surface_2_the_catalog_tile_behind_fsc_genuinely_ships(self):
        """FSC is reached through the ``salesforce`` tile. The roadmap overlay must
        classify that tile as shipped and NOT roadmap, or the registry entry would
        be unreachable in practice."""
        roadmap = _roadmap()
        assert roadmap.is_shipped(BASE_CONNECTOR_ID) is True
        assert roadmap.is_roadmap(BASE_CONNECTOR_ID) is False

    def test_surface_2_the_connect_guard_would_not_block_salesforce(self):
        """The roadmap overlay drives the connect guard; a roadmap connector is
        refused. Salesforce must not be."""
        annotated = _roadmap().annotate_connector({"id": BASE_CONNECTOR_ID})
        assert annotated.get("roadmap") in (False, None)

    def test_surface_3_frontend_tile_allows_the_salesforce_connector(self):
        assert BASE_CONNECTOR_ID in _frontend_enabled_connector_ids()

    def test_frontend_pack_selection_agrees_with_the_backend_declaration(self):
        """THE SURFACE THAT WAS DISAGREEING.

        The frontend's CLOUD_PACK_REGISTRY is what actually selects packs at launch.
        It mapped salesforce_fsc to service_cloud, so an entry honestly gated as
        FSC-capable would still have produced Service Cloud findings at runtime.
        """
        frontend = _frontend_cloud_pack_registry()
        spp = _spp()
        for product_id, declared in spp.SALESFORCE_PRODUCT_PACKS.items():
            assert product_id in frontend, (
                f"{product_id} is declared in the backend but missing from the "
                f"frontend CLOUD_PACK_REGISTRY"
            )
            assert frontend[product_id] == declared.pack_id, (
                f"{product_id}: frontend selects {frontend[product_id]!r} but the "
                f"backend declares {declared.pack_id!r}"
            )

    def test_frontend_declares_no_product_the_backend_does_not(self):
        frontend = set(_frontend_cloud_pack_registry())
        declared = set(_spp().declared_product_ids())
        assert frontend == declared, (
            f"frontend-only: {sorted(frontend - declared)}, "
            f"backend-only: {sorted(declared - frontend)}"
        )

    def test_fsc_selects_its_own_pack_in_the_frontend(self):
        assert _frontend_cloud_pack_registry()[FSC_PRODUCT_ID] == FSC_PACK_ID

    def test_every_frontend_selected_pack_is_a_registered_pack(self):
        registered = set(_pack_config().list_packs())
        for product_id, pack_id in _frontend_cloud_pack_registry().items():
            assert pack_id in registered, f"{product_id} -> unregistered {pack_id!r}"


# ── The capability the entry now claims genuinely exists ──────────────────────

class TestTheClaimIsBacked:

    def test_the_pack_is_registered_with_detectors(self):
        pack = _pack_config().PACK_REGISTRY[FSC_PACK_ID]
        assert pack["detectors"], "FSC pack ships no detectors"
        assert len(pack["detectors"]) == 5

    def test_the_detectors_import_and_are_scored(self):
        m = _pack_config()
        scorer = importlib.import_module(
            "discovery.packs.financial_services_cloud_scorer"
        )
        for path in m.get_detector_modules(FSC_PACK_ID):
            detector_id = importlib.import_module(path).DETECTOR_ID
            assert scorer.is_financial_services_cloud_detector(detector_id)

    def test_the_ingest_reads_fsc_objects(self):
        fsc = importlib.import_module("discovery.ingest.fsc")
        soql_blob = " ".join(soql for _sobject, soql in fsc._LIVE_QUERIES)
        assert "FinServ__Referral__c" in soql_blob
        assert "FinServ__FinancialAccount__c" in soql_blob

    def test_the_pack_is_hinted_for_the_industry_that_presents_the_product(self):
        """The industry that anchors salesforce_fsc must also hint the FSC pack,
        or the product would be connectable with the pack never selected."""
        config = _industries().INDUSTRY_REGISTRY["financial_services"]
        assert FSC_PACK_ID in config.pack_hints

    def test_a_template_activates_the_pack(self):
        template_registry = importlib.import_module(
            "discovery.packs.template_registry"
        )
        defn = template_registry.get_template(FSC_PACK_ID)
        assert defn is not None
        assert defn.pack_id == FSC_PACK_ID


# ── Consistency of the declaration module itself ──────────────────────────────

class TestDeclarationModuleIsSound:

    def test_every_declaration_states_a_reason(self):
        for product_id, declared in _spp().SALESFORCE_PRODUCT_PACKS.items():
            assert declared.reason.strip(), f"{product_id} has no reason"

    def test_every_declaration_is_dedicated_or_generic(self):
        spp = _spp()
        for declared in spp.SALESFORCE_PRODUCT_PACKS.values():
            assert declared.kind in (spp.DEDICATED, spp.GENERIC)

    def test_product_id_matches_its_key(self):
        for key, declared in _spp().SALESFORCE_PRODUCT_PACKS.items():
            assert declared.product_id == key

    def test_dedicated_and_pending_partition_the_declarations(self):
        spp = _spp()
        assert set(spp.products_with_dedicated_pack()) | set(
            spp.pending_domain_pack()
        ) == set(spp.declared_product_ids())
        assert set(spp.products_with_dedicated_pack()) & set(
            spp.pending_domain_pack()
        ) == set()

    def test_is_salesforce_product_excludes_the_base_connector(self):
        spp = _spp()
        assert spp.is_salesforce_product(BASE_CONNECTOR_ID) is False
        assert spp.is_salesforce_product(FSC_PRODUCT_ID) is True
        assert spp.is_salesforce_product("") is False
        assert spp.is_salesforce_product("servicenow") is False

    def test_pack_for_product_is_none_for_unknown(self):
        assert _spp().pack_for_product("nope") is None
