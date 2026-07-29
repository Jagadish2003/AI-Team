"""R191-P1 T6 (AT-708) — golden-run regression (AC2).

The multi-pack story's safety bar: a SINGLE-pack run must produce findings
byte-identical to the pre-change pipeline. This test locks that with a golden
master — a committed snapshot of the deterministic findings a single-pack offline
run produces (``golden_multi_pack/single_pack_findings.json``). Any future change
to the pipeline that alters a single-pack run's findings (detector set, per-pack
calibration, identity, evidence content, thresholds) fails this test.

The golden was captured from the current single-pack output, which the full
discovery suite (1580+ tests) confirms is unchanged from the pre-multi-pack
pipeline; it then freezes that output so a regression cannot slip in later.

Canonical (deterministic) slice compared — the pack's OWN produced findings:
  detector_id, packId, packVersion, signal_source, metric_value, threshold,
  impact, effort, tier, opportunity_identity, title, category, and the evidence
  CONTENT (source/detectorId/type/title/snippet).

Deliberately excluded from the golden (documented cross-run/run-scoped state, not
part of a pack's findings): runId/orgId; evidence ids (embed run_id); confidence
and the corroboration_* overlay (ENT-2 elevates confidence from cross-run graph
state — see the runner). Those are covered by their own tests.

Regenerate (only after an INTENTIONAL, reviewed findings change):
    REGEN_GOLDEN=1 PYTHONPATH=. python -m pytest \
        discovery/tests/test_multi_pack_golden_run.py -k regenerate -s
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.runner import run  # noqa: E402

_GOLDEN_PATH = Path(__file__).parent / "golden_multi_pack" / "single_pack_findings.json"
_GOLDEN_PACKS = ["service_cloud", "ncino"]


def canonical_findings(payload: dict) -> list:
    """Deterministic, order-stable signature of a run's findings (see module doc)."""
    out = []
    for o in payload["opportunities"]:
        evidence = sorted(
            (
                e.get("source"),
                e.get("detectorId") or e.get("detector_id"),
                e.get("evidenceType"),
                e.get("title"),
                e.get("snippet"),
            )
            for e in o.get("evidence", [])
        )
        out.append(
            {
                "detector_id": o["detector_id"],
                "packId": o["packId"],
                "packVersion": o["packVersion"],
                "signal_source": o.get("signal_source"),
                "metric_value": o.get("metric_value"),
                "threshold": o.get("threshold"),
                "impact": o["impact"],
                "effort": o["effort"],
                "tier": o["tier"],
                "opportunity_identity": o["opportunity_identity"],
                "title": o.get("title"),
                "category": o.get("category"),
                "evidence": [list(e) for e in evidence],
                "evidence_count": len(o.get("evidence", [])),
            }
        )
    out.sort(key=lambda d: (d["packId"], d["opportunity_identity"], d["detector_id"]))
    return out


def _run_single(pack: str) -> list:
    payload = run(mode="offline", run_id=f"golden-{pack}", org_id="golden_org", pack=pack)
    # json round-trip so the comparison matches the on-disk golden's types exactly
    # (e.g. tuples → lists, int/float normalisation).
    return json.loads(json.dumps(canonical_findings(payload)))


@pytest.mark.skipif(os.getenv("REGEN_GOLDEN") != "1", reason="golden regeneration is opt-in")
def test_regenerate_golden():  # pragma: no cover - maintenance utility
    data = {pack: _run_single(pack) for pack in _GOLDEN_PACKS}
    _GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _GOLDEN_PATH.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nwrote golden: { {k: len(v) for k, v in data.items()} }")


def test_golden_fixture_exists_and_is_populated():
    assert _GOLDEN_PATH.exists(), "golden fixture missing — regenerate with REGEN_GOLDEN=1"
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))
    assert set(golden) == set(_GOLDEN_PACKS)
    for pack in _GOLDEN_PACKS:
        assert golden[pack], f"golden for {pack} is empty"


@pytest.mark.parametrize("pack", _GOLDEN_PACKS)
def test_single_pack_run_matches_golden(pack):
    """AC2: a single-pack run's findings are byte-identical to the golden master."""
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))[pack]
    current = _run_single(pack)
    assert current == golden, (
        f"single-pack '{pack}' findings diverged from the golden master. If this "
        f"change is intentional, regenerate with REGEN_GOLDEN=1 and review the diff."
    )


@pytest.mark.parametrize("pack", _GOLDEN_PACKS)
def test_pack_ids_single_element_matches_golden(pack):
    """AC2: the NEW plural path (pack_ids=[X]) is byte-identical to the golden too —
    the singular and single-element multi-pack paths converge on the same findings."""
    golden = json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))[pack]
    payload = run(mode="offline", run_id=f"golden-{pack}", org_id="golden_org", pack_ids=[pack])
    current = json.loads(json.dumps(canonical_findings(payload)))
    assert current == golden
