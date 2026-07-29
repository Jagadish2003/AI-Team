"""salesforce_product_packs.py — 2.0-D1 T5: which pack serves which Salesforce product.

THE PROBLEM THIS EXISTS TO FIX
-----------------------------
The R191-R1 anchor-on-shipped rule says anything shown as connectable must have
ingestion that genuinely ships. Its CI guard
(``tests/contract/test_r191_r1_ingestor_registry_enforcement.py``) discovers
implemented ingestors from the codebase and validates the registry against them —
but it resolves the domain-specific Salesforce product ids through
``IMPLEMENTATION_ALIASES``, where ``salesforce_fsc`` maps to ``{salesforce}``.

That alias is defensible for the CONNECTOR-level question — "can we authenticate
and read from this org?" — because the base Salesforce ingestor ships. It does not
answer the PACK-level question: "do we read this product's objects and detect its
patterns?" Before 2.0-D1, ``salesforce_fsc`` was already in
``INDUSTRY_REGISTRY['financial_services'].system_defaults`` as a PRIMARY
``system_of_record``, and CI was green — while no FSC pack, no FSC detectors and no
FSC ingest existed. The entry claimed a capability the codebase did not have, and
the alias hid it.

This module closes that gap by making the product→pack relationship EXPLICIT and
TESTABLE, so an entry cannot claim more than is true.

WHY A DECLARATION RATHER THAN "MUST HAVE ITS OWN PACK"
-----------------------------------------------------
There are two honest arrangements, and a guard that demanded a dedicated pack for
every product would wrongly fail the second:

  * DEDICATED — the product has its own pack, with its own objects, detectors and
    calibration (Service Cloud, nCino, Public Sector Solutions, and now FSC).
  * GENERIC — the product is DELIBERATELY served by the generic ``service_cloud``
    pack because no domain pack exists yet. Revenue Cloud and Health Cloud are in
    this position (the Health Cloud pack is explicitly deferred to 2.0.1). That is
    legitimate as long as it is DECLARED — the dishonesty is an undeclared claim,
    not a generic one.

So the rule the guard enforces is: every domain-specific product id presented as
connectable must declare a pack HERE, and that pack must be registered in
``pack_config.PACK_REGISTRY``. A product silently inheriting a pack, or naming one
that does not exist, fails.

``pending_domain_pack`` records — without failing CI — which products are still
served generically, so the remaining gap is visible rather than implied.

This is the BACKEND source of truth. The frontend's ``CLOUD_PACK_REGISTRY``
(``StackBuilderPage.tsx``) is what actually selects packs at launch; a contract test
pins the two together so a registry entry cannot be honestly gated here while the
frontend sends a different pack.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# How a product's pack relationship is classified.
DEDICATED = "dedicated"   # the product has its own domain pack
GENERIC = "generic"       # deliberately served by the generic service_cloud pack


@dataclass(frozen=True)
class ProductPack:
    """The declared pack for one Salesforce product.

    product_id — the registry/system id (e.g. ``salesforce_fsc``).
    pack_id    — the pack that serves it; must be registered in PACK_REGISTRY.
    kind       — DEDICATED or GENERIC.
    reason     — why, in one line. Required: an undocumented GENERIC declaration is
                 the same silent over-claim this module exists to prevent.
    """
    product_id: str
    pack_id: str
    kind: str
    reason: str

    @property
    def is_dedicated(self) -> bool:
        return self.kind == DEDICATED


# The base ``salesforce`` id is the CONNECTOR, not a product declaration — it is a
# real catalog tile and is covered by the existing connector-level check. Only the
# domain-specific product ids need a pack declaration.
BASE_SALESFORCE_CONNECTOR_ID = "salesforce"

SALESFORCE_PRODUCT_PACKS: Dict[str, ProductPack] = {
    "salesforce_sc": ProductPack(
        product_id="salesforce_sc",
        pack_id="service_cloud",
        kind=DEDICATED,
        reason=(
            "Service Cloud is the pack's own domain — case management, flows and "
            "approval workflows are exactly what service_cloud detects."
        ),
    ),
    "salesforce_ncino": ProductPack(
        product_id="salesforce_ncino",
        pack_id="ncino",
        kind=DEDICATED,
        reason=(
            "nCino ships its own ingest (discovery/ingest/ncino.py over LLC_BI__ "
            "objects), five lending detectors, and the lending scorer."
        ),
    ),
    "salesforce_pss": ProductPack(
        product_id="salesforce_pss",
        pack_id="strs_benefits",
        kind=DEDICATED,
        reason=(
            "Public Sector Solutions is served by the STRS benefits pack — its own "
            "ingest, four benefit detectors and benefits scorer."
        ),
    ),
    "salesforce_fsc": ProductPack(
        product_id="salesforce_fsc",
        pack_id="financial_services_cloud",
        kind=DEDICATED,
        reason=(
            "2.0-D1: FSC ships its own ingest (discovery/ingest/fsc.py over "
            "FinServ__ objects), five FSC detectors and the FSC scorer. Before "
            "2.0-D1 this id was presented as connectable with none of that behind "
            "it — the gap this module's guard now catches."
        ),
    ),
    "salesforce_rc": ProductPack(
        product_id="salesforce_rc",
        pack_id="service_cloud",
        kind=GENERIC,
        reason=(
            "Revenue Cloud has no domain pack. It is deliberately served by the "
            "generic service_cloud pack, which the revenue_operations template "
            "already declares openly (pack_id='service_cloud'). Generic detection "
            "only — no revenue-specific objects or detectors."
        ),
    ),
    "salesforce_hc": ProductPack(
        product_id="salesforce_hc",
        pack_id="service_cloud",
        kind=GENERIC,
        reason=(
            "Health Cloud has no domain pack — it is explicitly deferred to 2.0.1 "
            "(demand-gated) in the Release 2.0 out-of-scope list. Served by the "
            "generic service_cloud pack meanwhile: generic detection only, no "
            "Health-Cloud objects or detectors."
        ),
    ),
}


# ── Public API ────────────────────────────────────────────────────────────────


def is_salesforce_product(system_id: str) -> bool:
    """True when ``system_id`` is a domain-specific Salesforce product id.

    The base ``salesforce`` connector id is deliberately excluded: it is a catalog
    tile covered by the connector-level check, not a product declaration.
    """
    sid = str(system_id or "").strip()
    return sid.startswith(f"{BASE_SALESFORCE_CONNECTOR_ID}_")


def get_product_pack(product_id: str) -> Optional[ProductPack]:
    """Return the declared pack for a product, or None when undeclared."""
    return SALESFORCE_PRODUCT_PACKS.get(str(product_id or "").strip())


def pack_for_product(product_id: str) -> Optional[str]:
    """Return the pack id serving ``product_id``, or None when undeclared."""
    declared = get_product_pack(product_id)
    return declared.pack_id if declared else None


def declared_product_ids() -> List[str]:
    """Every product id with a declared pack."""
    return sorted(SALESFORCE_PRODUCT_PACKS)


def products_with_dedicated_pack() -> List[str]:
    """Products backed by their own domain pack."""
    return sorted(
        p.product_id for p in SALESFORCE_PRODUCT_PACKS.values() if p.is_dedicated
    )


def pending_domain_pack() -> List[str]:
    """Products still served generically — the honest remaining gap.

    Recorded rather than enforced: these are pre-existing, deliberate scope
    decisions (Health Cloud is 2.0.1), and the point is that they are VISIBLE
    instead of hidden behind a connector alias.
    """
    return sorted(
        p.product_id for p in SALESFORCE_PRODUCT_PACKS.values() if not p.is_dedicated
    )


def undeclared_products(system_ids) -> List[str]:
    """Return the domain-specific Salesforce product ids with NO declaration.

    This is the check the R191-R1 guard runs over the industry registry: a product
    presented as connectable with no declared pack is claiming a capability nobody
    has written down.
    """
    return sorted({
        str(sid).strip()
        for sid in (system_ids or [])
        if is_salesforce_product(sid) and get_product_pack(sid) is None
    })


def products_naming_unregistered_packs(registered_pack_ids) -> List[str]:
    """Return declarations pointing at a pack that is not registered.

    This is the gate that goes RED if the FSC pack registration is removed while
    ``salesforce_fsc`` is still presented as connectable.
    """
    registered = {str(p) for p in (registered_pack_ids or [])}
    return sorted(
        product_id
        for product_id, declared in SALESFORCE_PRODUCT_PACKS.items()
        if declared.pack_id not in registered
    )
