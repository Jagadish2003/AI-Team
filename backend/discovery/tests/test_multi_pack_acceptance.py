"""R191-P1 T6 (AT-708) — consolidated Section-3 acceptance suite (AC1–AC6).

One coherent, reviewable place that validates every R191-P1 acceptance criterion
against the built behaviour (offline runner; fixtures; no live credentials). It
complements — not replaces — the per-task suites, giving the story a single
Definition-of-Done artifact:

  AC1  multi-pack findings + provenance    (both packs produce findings; run
                                            record lists both ids + versions)
  AC2  single-pack golden regression       (byte-identical — owned by
                                            test_multi_pack_golden_run.py; a
                                            determinism sanity check is repeated
                                            here for completeness)
  AC3  per-pack calibration isolation      (no blended calibration across packs)
  AC4  no cross-pack merging               (overlapping opportunities stay two
                                            findings with distinct provenance)
  AC5  template activation                 (a template declaring two packs
                                            activates both — resolution layer;
                                            full HTTP launch + run-health rows are
                                            in tests/contract/test_multi_pack_template_and_health.py)
  AC6  evidence resolves within its pack    (every evidence item carries its
                                            finding's packId)
"""
from __future__ import annotations

import os
from uuid import uuid4

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.runner import run  # noqa: E402
from discovery.packs.pack_config import get_pack_version  # noqa: E402
from discovery.packs.template_registry import (  # noqa: E402
    FocusDefaults,
    TemplateDefinition,
    register_template,
    resolve_launch_config,
    unregister_template,
)

_PACK_A = "service_cloud"
_PACK_B = "ncino"


def _calibration_slice(opp):
    return (
        opp["detector_id"], opp["packId"], opp["packVersion"],
        opp["impact"], opp["effort"], opp["tier"], opp["opportunity_identity"],
    )


def _by_pack(payload):
    out = {}
    for o in payload["opportunities"]:
        out.setdefault(o["packId"], []).append(o)
    return out


@pytest.fixture(scope="module")
def two_pack():
    return run(mode="offline", run_id="accept-two", org_id="accept_org",
               pack_ids=[_PACK_A, _PACK_B])


# ── AC1: multi-pack findings + provenance ─────────────────────────────────────

def test_ac1_findings_from_both_packs_with_run_record_stamps(two_pack):
    by_pack = _by_pack(two_pack)
    assert by_pack.get(_PACK_A) and by_pack.get(_PACK_B)
    for o in two_pack["opportunities"]:
        assert o["packId"] in (_PACK_A, _PACK_B)
        assert o["packVersion"] == get_pack_version(o["packId"])
    assert two_pack["packIds"] == [_PACK_A, _PACK_B]
    assert two_pack["packVersions"] == {
        _PACK_A: get_pack_version(_PACK_A),
        _PACK_B: get_pack_version(_PACK_B),
    }
    assert {p["packId"] for p in two_pack["packs"]} == {_PACK_A, _PACK_B}


# ── AC2: single-pack determinism (byte-identical golden owned elsewhere) ───────

@pytest.mark.parametrize("pack", [_PACK_A, _PACK_B])
def test_ac2_single_pack_singular_and_plural_paths_agree(pack):
    a = run(mode="offline", run_id="accept-ac2a", org_id="accept_org", pack=pack)
    b = run(mode="offline", run_id="accept-ac2b", org_id="accept_org", pack_ids=[pack])
    assert sorted(_calibration_slice(o) for o in a["opportunities"]) == sorted(
        _calibration_slice(o) for o in b["opportunities"]
    )


# ── AC3: per-pack calibration isolation (no blending) ─────────────────────────

def test_ac3_pack_calibration_isolated_from_co_selected_pack(two_pack):
    alone = run(mode="offline", run_id="accept-alone", org_id="accept_org", pack_ids=[_PACK_B])
    nc_alone = sorted(_calibration_slice(o) for o in alone["opportunities"])
    nc_together = sorted(_calibration_slice(o) for o in _by_pack(two_pack)[_PACK_B])
    assert nc_alone == nc_together


# ── AC4: no cross-pack merging ────────────────────────────────────────────────

def test_ac4_overlapping_opportunities_stay_two_findings(two_pack):
    by_detector = {}
    for o in two_pack["opportunities"]:
        by_detector.setdefault(o["detector_id"], []).append(o)
    cross_pack = {d: fs for d, fs in by_detector.items()
                  if len({f["packId"] for f in fs}) > 1}
    assert cross_pack, "expected a detector surfaced by both packs (APPROVAL_BOTTLENECK)"
    for _det, findings in cross_pack.items():
        assert len({f["opportunity_identity"] for f in findings}) == len(findings)


# ── AC5: a template declaring two packs activates both ────────────────────────

def test_ac5_two_pack_template_activates_both_packs():
    template_id = f"accept_combined_{uuid4().hex[:6]}"
    register_template(
        TemplateDefinition(
            template_id=template_id, label="x", description="x",
            suggested_systems=["servicenow"], suggested_roles={},
            focus_defaults=FocusDefaults(focus_id="core_operations"),
            pack_id=_PACK_A, packs=[_PACK_A, _PACK_B],
        )
    )
    try:
        resolved = resolve_launch_config(template_id)  # untouched
        assert resolved["effective"]["pack_ids"] == [_PACK_A, _PACK_B]
        assert resolved["effective"]["pack_id"] == _PACK_A
    finally:
        unregister_template(template_id)


# ── AC6: evidence resolves within its finding's own pack context ──────────────

def test_ac6_every_evidence_item_carries_its_findings_pack(two_pack):
    for o in two_pack["opportunities"]:
        for ev in o["evidence"]:
            assert ev.get("packId") == o["packId"]
