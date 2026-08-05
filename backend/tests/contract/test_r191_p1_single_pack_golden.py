"""R191-P1 AC2 - single-pack golden-run regression guard.

The multi-pack work must not change a normal single-pack run. This test captures
the offline Service Cloud run shape that matters to customers: run pack metadata,
opportunities, nested evidence, raw evidence, and score/debug payloads.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GOLDEN_RUN_ID = "r191-p1-single-pack-golden"
GOLDEN_ORG_ID = "r191-p1-golden-org"
GOLDEN_PACK_ID = "service_cloud"
GOLDEN_SYSTEMS = ("salesforce", "jira")
GOLDEN_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "r191_p1_single_pack_golden.json"
)

OPPORTUNITY_GOLDEN_KEYS = (
    "detector_id",
    "opportunity_identity",
    "orgId",
    "runId",
    "packId",
    "packVersion",
    "tier",
    "impact",
    "effort",
    "confidence",
    "metric_value",
    "threshold",
    "roadmap_stage",
    "signal_source",
    "evidenceIds",
    "raw_evidence",
    "score_debug",
    "evidence",
)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = datetime(2026, 8, 3, 18, 47, 0, tzinfo=timezone.utc)
        if tz is None:
            return fixed.replace(tzinfo=None)
        return fixed.astimezone(tz)


def _normalize_evidence_timestamps(opportunity: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(opportunity)
    normalized["evidence"] = [
        {**evidence, "tsLabel": "<normalized>"}
        for evidence in opportunity.get("evidence", [])
    ]
    return normalized


def _golden_projection(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "run": {
            "mode": payload["mode"],
            "succeeded": payload["succeeded"],
            "packId": payload["packId"],
            "packIds": payload["packIds"],
            "packName": payload["packName"],
            "packVersion": payload["packVersion"],
            "packVersions": payload["packVersions"],
            "detectorsExecuted": payload["detectorsExecuted"],
            "topLevelEvidence": payload.get("evidence", []),
        },
        "opportunities": [
            _normalize_evidence_timestamps(
                {key: opportunity.get(key) for key in OPPORTUNITY_GOLDEN_KEYS}
            )
            for opportunity in payload["opportunities"]
        ],
    }


def _disable_non_payload_side_effects(monkeypatch, runner) -> None:
    import app.entity_extractor as entity_extractor
    import app.relationship_mapper as relationship_mapper

    monkeypatch.setattr(runner, "record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "update_run_step", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "snapshot_signals", lambda *args, **kwargs: None)
    monkeypatch.setattr(entity_extractor, "extract_entities", lambda *args, **kwargs: [])
    monkeypatch.setattr(relationship_mapper, "map_relationships", lambda *args, **kwargs: None)


def test_single_pack_offline_run_matches_golden_payload(monkeypatch):
    import discovery.runner as runner

    monkeypatch.setattr(runner, "datetime", FixedDateTime)
    _disable_non_payload_side_effects(monkeypatch, runner)

    logging.disable(logging.CRITICAL)
    try:
        payload = runner.run(
            mode="offline",
            run_id=GOLDEN_RUN_ID,
            org_id=GOLDEN_ORG_ID,
            systems=list(GOLDEN_SYSTEMS),
            pack=GOLDEN_PACK_ID,
        )
    finally:
        logging.disable(logging.NOTSET)

    actual = json.dumps(
        _golden_projection(payload),
        indent=2,
        sort_keys=True,
    ) + "\n"
    expected = GOLDEN_PATH.read_text(encoding="utf-8")

    assert actual == expected
