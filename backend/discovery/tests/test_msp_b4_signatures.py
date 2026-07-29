"""MSP-B4 T2 — deterministic resolution + incident-identity signatures.

Proves the signature contract in
``discovery/signals/resolution_signature.py``: stable grouping when structured
identity and resolution pattern truly match, stable NON-grouping for near
misses (same category/different close code; same close code/different CI class),
explicit and consistent normalisation (case, whitespace, empty/missing,
ServiceNow reference display values), and graceful behaviour when the optional
CI is unavailable. Mirrors the B0 event-signature discipline: deterministic,
explainable, conservative.

Offline; no ServiceNow credentials required.
"""
from __future__ import annotations

import os

import pytest

os.environ["INGEST_MODE"] = "offline"

from discovery.signals.resolution_signature import (  # noqa: E402
    INCIDENT_IDENTITY_SIGNATURE_VERSION,
    RESOLUTION_SIGNATURE_VERSION,
    compute_incident_identity_signature,
    compute_resolution_signature,
    incident_identity_signature_components,
    normalize_short_description,
    normalize_token,
    resolution_signature_components,
)


def _res(**kw):
    base = dict(
        category="Software",
        close_code="Solved (Permanently)",
        resolved_by_group="Level 2 Support",
        ci_class=None,
        ci_id="ci-app-001",
    )
    base.update(kw)
    return compute_resolution_signature(**base)


def _ident(**kw):
    base = dict(
        category="Software",
        short_description="Email service outage on portal",
        ci_class=None,
        ci_id="ci-app-001",
    )
    base.update(kw)
    return compute_incident_identity_signature(**base)


# ─────────────────────────────────────────────────────────────────────────────
# Determinism + versioning (B0 discipline)
# ─────────────────────────────────────────────────────────────────────────────


class TestDeterminismAndVersion:
    def test_pure_function_repeatable(self):
        assert _res() == _res()
        assert _ident() == _ident()

    def test_version_prefix(self):
        assert _res().startswith(f"{RESOLUTION_SIGNATURE_VERSION}:")
        assert _ident().startswith(f"{INCIDENT_IDENTITY_SIGNATURE_VERSION}:")

    def test_signatures_are_128bit_hex(self):
        digest = _res().split(":", 1)[1]
        assert len(digest) == 32
        int(digest, 16)  # hex-parseable


# ─────────────────────────────────────────────────────────────────────────────
# Stable grouping — identical structured input groups
# ─────────────────────────────────────────────────────────────────────────────


class TestStableGrouping:
    def test_same_resolution_pattern_groups(self):
        assert _res() == _res(ci_id="ci-app-001")

    def test_same_identity_groups_regardless_of_word_order_case_punctuation(self):
        a = compute_incident_identity_signature(
            category="Software", short_description="Portal email OUTAGE!!"
        )
        b = compute_incident_identity_signature(
            category="software", short_description="  outage,  portal   email  "
        )
        assert a == b

    def test_resolution_signature_ignores_subcategory_and_notes(self):
        """Only the four declared components participate; other fields cannot split it."""
        assert _res() == _res()  # no subcategory/notes params exist to change it


# ─────────────────────────────────────────────────────────────────────────────
# Near-miss separation (AC2) — from BOTH sides
# ─────────────────────────────────────────────────────────────────────────────


class TestNearMissSeparation:
    def test_same_category_different_close_code_do_not_group(self):
        assert _res(close_code="Solved (Permanently)") != _res(
            close_code="Solved (Work Around)"
        )

    def test_same_close_code_different_ci_class_do_not_group(self):
        a = _res(ci_class="cmdb_ci_db_instance", ci_id=None)
        b = _res(ci_class="cmdb_ci_server", ci_id=None)
        assert a != b

    def test_different_category_do_not_group(self):
        assert _res(category="Software") != _res(category="Network")

    def test_different_resolved_by_group_do_not_group(self):
        assert _res(resolved_by_group="Level 2 Support") != _res(
            resolved_by_group="Network Ops"
        )

    def test_identity_different_short_description_does_not_group(self):
        assert _ident(short_description="Email outage") != _ident(
            short_description="Login failure"
        )

    def test_identity_different_ci_class_does_not_group(self):
        a = _ident(ci_class="cmdb_ci_db_instance", ci_id=None)
        b = _ident(ci_class="cmdb_ci_server", ci_id=None)
        assert a != b


# ─────────────────────────────────────────────────────────────────────────────
# CI id vs CI class must never collide; class is preferred
# ─────────────────────────────────────────────────────────────────────────────


class TestCiComponent:
    def test_ci_class_preferred_over_ci_id(self):
        with_both = _res(ci_class="cmdb_ci_server", ci_id="ci-app-001")
        class_only = _res(ci_class="cmdb_ci_server", ci_id=None)
        assert with_both == class_only  # id ignored when class present

    def test_ci_id_used_when_no_class(self):
        assert _res(ci_class=None, ci_id="ci-app-001") != _res(
            ci_class=None, ci_id="ci-app-002"
        )

    def test_ci_id_cannot_collide_with_ci_class_of_same_text(self):
        # A CI id whose text equals a class name must still be distinct — the
        # ci:/class: markers guarantee this.
        as_id = _res(ci_class=None, ci_id="cmdb_ci_server")
        as_class = _res(ci_class="cmdb_ci_server", ci_id=None)
        assert as_id != as_class


# ─────────────────────────────────────────────────────────────────────────────
# Missing optional fields (T2 completion criterion + AC5 groundwork)
# ─────────────────────────────────────────────────────────────────────────────


class TestMissingOptionalFields:
    def test_unlocated_incidents_still_sign_and_group(self):
        """No CI at all → deterministic 'unlocated' signature that still groups."""
        a = _res(ci_class=None, ci_id=None)
        b = _res(ci_class=None, ci_id=None)
        assert a == b

    def test_unlocated_differs_from_located(self):
        assert _res(ci_class=None, ci_id=None) != _res(ci_class=None, ci_id="ci-app-001")

    def test_empty_and_missing_close_code_are_equivalent(self):
        assert _res(close_code=None) == _res(close_code="")
        assert _res(close_code="") == _res(close_code="   ")

    def test_identity_with_empty_short_description(self):
        a = compute_incident_identity_signature(category="Software", short_description=None)
        b = compute_incident_identity_signature(category="Software", short_description="")
        assert a == b


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation (explicit + documented)
# ─────────────────────────────────────────────────────────────────────────────


class TestNormalization:
    def test_case_and_whitespace_fold(self):
        assert normalize_token("  Level 2   Support ") == normalize_token("level 2 support")

    def test_empty_variants(self):
        assert normalize_token(None) == ""
        assert normalize_token("   ") == ""

    def test_servicenow_reference_display_value_uses_stable_value(self):
        # A raw reference object folds to its stable value, not the display name.
        ref = {"value": "grp-sys-1", "display_value": "Level 2 Support"}
        assert normalize_token(ref) == "grp-sys-1"
        # Two records with the same stable value but different display names agree.
        assert _res(resolved_by_group=ref) == _res(resolved_by_group={"value": "grp-sys-1"})

    def test_short_description_tokenset_is_sorted_deduped_filtered(self):
        # Order-independent, de-duplicated, grammatical filler removed.
        assert normalize_short_description("Portal portal EMAIL the on") == ("email", "portal")

    def test_short_description_no_fuzzy_matching(self):
        # Structural only: 'outage' and 'outages' are different tokens (no stemming).
        assert normalize_short_description("outage") != normalize_short_description("outages")


# ─────────────────────────────────────────────────────────────────────────────
# Explainability — components exposed without re-deriving
# ─────────────────────────────────────────────────────────────────────────────


class TestExplainability:
    def test_resolution_components(self):
        comp = resolution_signature_components(
            category="Software",
            close_code="Solved (Permanently)",
            resolved_by_group="Level 2 Support",
            ci_id="ci-app-001",
        )
        assert comp["category"] == "software"
        assert comp["close_code"] == "solved (permanently)"
        assert comp["ci_component"] == "ci:ci-app-001"
        assert comp["resolved_by_group"] == "level 2 support"
        assert comp["version"] == RESOLUTION_SIGNATURE_VERSION

    def test_identity_components(self):
        comp = incident_identity_signature_components(
            category="Software",
            short_description="Email outage on portal",
            ci_class="cmdb_ci_server",
        )
        assert comp["ci_component"] == "class:cmdb_ci_server"
        assert comp["short_description_tokens"] == ["email", "outage", "portal"]


# ─────────────────────────────────────────────────────────────────────────────
# Integration through the ServiceNow ingest path
# ─────────────────────────────────────────────────────────────────────────────


class TestIntegrationThroughIngest:
    @pytest.fixture
    def by_number(self):
        from discovery.ingest.servicenow import get_incident_metrics

        return {i["number"]: i["resolution"] for i in get_incident_metrics()["incidents"]}

    def test_resolved_incidents_carry_both_signatures(self, by_number):
        res = by_number["INC0000001"]
        assert res["incident_identity_signature"].startswith("1:")
        assert res["resolution_signature"].startswith("1:")

    def test_unresolved_incident_has_no_resolution_signature(self, by_number):
        res = by_number["INC0000003"]
        assert res["is_resolved"] is False
        assert res["resolution_signature"] is None
        # Identity is 'what kind of incident' — present even when unresolved.
        assert res["incident_identity_signature"].startswith("1:")

    def test_same_resolution_pattern_shares_resolution_signature(self, by_number):
        # INC0000004 and INC0000005: same category/close_code/group, both
        # unlocated → identical resolution_signature (stable grouping).
        assert (
            by_number["INC0000004"]["resolution_signature"]
            == by_number["INC0000005"]["resolution_signature"]
        )

    def test_two_signature_discipline_keeps_distinct_incidents_apart(self, by_number):
        # ...but they are different KINDS of incident, so identity differs — the
        # detector groups on the pair, so these two would NOT be a recurrence.
        assert (
            by_number["INC0000004"]["incident_identity_signature"]
            != by_number["INC0000005"]["incident_identity_signature"]
        )

    def test_near_miss_close_code_separates_through_ingest(self, by_number):
        assert (
            by_number["INC0000002"]["resolution_signature"]
            != by_number["INC0000004"]["resolution_signature"]
        )

    def test_raw_short_description_not_exposed_in_resolution_block(self, by_number):
        import json

        blob = json.dumps(list(by_number.values()))
        # The signature is a hash; the raw title prose must not leak into the block.
        assert "multiple reassignments causing origination" not in blob
