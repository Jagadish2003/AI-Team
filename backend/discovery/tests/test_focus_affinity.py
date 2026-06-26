"""
R16-C2 — T1: tests for the FOCUS_AFFINITY mapping.

Verifies the canonical Discovery Focus -> detector affinity mapping:
  * every focus has an explicit backend interpretation,
  * enterprise_wide carries no affinity bias,
  * affinity ids are real, registered pack DETECTOR_IDs (no typos / no UI titles),
  * every non-enterprise pack detector is emphasised by at least one focus,
  * unknown focus / detector ids degrade safely,
  * the mapping is deterministic.
"""
import importlib

import pytest

from discovery.packs import focus_affinity as fa
from discovery.packs.pack_config import PACK_REGISTRY


# ── Canonical detector ids, sourced from the registered pack detector modules ──

def _registered_detector_ids() -> set:
    """Import every detector module referenced by any pack and collect its
    DETECTOR_ID constant — the single source of truth for valid ids."""
    ids = set()
    for pack in PACK_REGISTRY.values():
        for module_path in pack["detectors"]:
            module = importlib.import_module(module_path)
            ids.add(module.DETECTOR_ID)
    return ids


REGISTERED_DETECTOR_IDS = _registered_detector_ids()

SEVEN_FOCUSES = {
    "member_customer_service",
    "core_operations",
    "approvals_compliance",
    "cross_system_handoffs",
    "back_office_productivity",
    "engineering_change",
    "enterprise_wide",
}


# ── Shape & coverage ───────────────────────────────────────────────────────────

def test_all_seven_focuses_present():
    assert set(fa.FOCUS_AFFINITY.keys()) == SEVEN_FOCUSES
    assert fa.VALID_FOCUS_IDS == frozenset(SEVEN_FOCUSES)
    assert set(fa.list_focus_ids()) == SEVEN_FOCUSES


def test_enterprise_wide_has_no_bias():
    assert fa.FOCUS_NO_AFFINITY_BIAS is None
    assert fa.FOCUS_AFFINITY[fa.FOCUS_ENTERPRISE_WIDE] is fa.FOCUS_NO_AFFINITY_BIAS
    assert fa.is_enterprise_wide_focus(" enterprise_wide ") is True
    assert fa.FOCUS_AFFINITY["enterprise_wide"] is None
    assert fa.get_focus_affinity("enterprise_wide") is None
    assert fa.has_affinity_bias("enterprise_wide") is False


def test_non_enterprise_focuses_all_have_affinity():
    for focus in SEVEN_FOCUSES - {"enterprise_wide"}:
        affinity = fa.get_focus_affinity(focus)
        assert affinity is not None, focus
        assert len(affinity) > 0, focus
        assert fa.has_affinity_bias(focus) is True, focus


def test_affinity_ids_are_real_detector_ids():
    """No typos and no UI titles — every affinity id is a registered DETECTOR_ID."""
    for focus, detectors in fa.FOCUS_AFFINITY.items():
        if detectors is None:
            continue
        for det in detectors:
            assert det in REGISTERED_DETECTOR_IDS, f"{focus} -> unknown detector {det!r}"


def test_every_pack_detector_is_emphasised_somewhere():
    """Each focus accounts for existing pack detectors (Service Cloud, nCino,
    STRS, GitHub, SQL Server, Enterprise) — none is orphaned."""
    covered = set(fa.all_affinity_detector_ids())
    missing = REGISTERED_DETECTOR_IDS - covered
    assert not missing, f"detectors not emphasised by any focus: {sorted(missing)}"


def test_no_duplicate_ids_within_a_focus():
    for focus, detectors in fa.FOCUS_AFFINITY.items():
        if detectors is None:
            continue
        assert len(detectors) == len(set(detectors)), focus


# ── Product boundaries (Section 2) ─────────────────────────────────────────────

def test_approvals_compliance_owns_the_gate():
    affinity = fa.get_focus_affinity("approvals_compliance")
    for det in (
        "APPROVAL_BOTTLENECK",
        "PERMISSION_BOTTLENECK",
        "COVENANT_TRACKING_GAP",
        "DB_SLA_BREACH_RATE",
        "ENT_SLA_BREACH_BY_TEAM",
        "BENEFIT_ELECTION_DEADLINE",
    ):
        assert det in affinity, det


def test_cross_system_handoffs_owns_the_handoff():
    affinity = fa.get_focus_affinity("cross_system_handoffs")
    for det in (
        "HANDOFF_FRICTION",
        "CROSS_SYSTEM_ECHO",
        "ENT_CHANGE_INCIDENT_CORRELATION",
    ):
        assert det in affinity, det


def test_stuck_approval_is_compliance_not_handoff():
    """The decisive boundary: a stuck approval is the gate, not the handoff."""
    assert fa.detector_matches_focus("approvals_compliance", "APPROVAL_BOTTLENECK")
    assert not fa.detector_matches_focus("cross_system_handoffs", "APPROVAL_BOTTLENECK")
    # ...and a stuck handoff is the handoff, not the gate.
    assert fa.detector_matches_focus("cross_system_handoffs", "HANDOFF_FRICTION")
    assert not fa.detector_matches_focus("approvals_compliance", "HANDOFF_FRICTION")


# ── Membership / emphasis check ────────────────────────────────────────────────

def test_detector_matches_focus_emphasis():
    assert fa.detector_matches_focus("engineering_change", "GITHUB_PR_REVIEW_BOTTLENECK")
    assert not fa.detector_matches_focus("engineering_change", "APPROVAL_BOTTLENECK")


def test_enterprise_wide_emphasises_nothing():
    """No bias means detector_matches_focus is False for everything -> unbiased."""
    for det in REGISTERED_DETECTOR_IDS:
        assert fa.detector_matches_focus("enterprise_wide", det) is False
        assert fa.focus_emphasis_rank("enterprise_wide", det) == fa.FOCUS_NEUTRAL_RANK
        emphasis = fa.build_focus_emphasis("enterprise_wide", det)
        assert emphasis["matched"] is False
        assert emphasis["rank"] == fa.FOCUS_NEUTRAL_RANK
        assert emphasis["affinity"] == []
        assert "full unweighted view" in emphasis["rationale"]


# ── Safe degradation ───────────────────────────────────────────────────────────

def test_unknown_focus_degrades_to_no_bias():
    assert fa.get_focus_affinity("totally_made_up") is None
    assert fa.has_affinity_bias("totally_made_up") is False
    assert fa.is_valid_focus("totally_made_up") is False


def test_none_focus_degrades_to_no_bias():
    assert fa.get_focus_affinity(None) is None
    assert fa.detector_matches_focus(None, "APPROVAL_BOTTLENECK") is False


def test_unknown_detector_matches_no_focus():
    assert fa.detector_matches_focus("approvals_compliance", "NOT_A_REAL_DETECTOR") is False
    assert fa.detector_matches_focus("approvals_compliance", None) is False


def test_focus_id_normalisation_is_tolerant():
    assert fa.get_focus_affinity("  APPROVALS_COMPLIANCE  ") == fa.get_focus_affinity(
        "approvals_compliance"
    )
    assert fa.is_valid_focus("Core_Operations") is True


# ── Determinism ────────────────────────────────────────────────────────────────

def test_lookup_is_deterministic():
    for focus in SEVEN_FOCUSES:
        assert fa.get_focus_affinity(focus) == fa.get_focus_affinity(focus)
    assert fa.all_affinity_detector_ids() == fa.all_affinity_detector_ids()
    assert fa.list_focus_ids() == fa.list_focus_ids()
