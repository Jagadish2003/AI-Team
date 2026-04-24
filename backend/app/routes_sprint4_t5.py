"""
Sprint 4 T5 — Track A G-Run-Scoped Integration Pass  v1.3

Changes from v1.2:
  Issue 3 / Option A: snapshotBubbles and roadmapHighlights no longer read
    from the seed executive_reports record. Both are now derived from live
    run data so the DoD "no seed data on any screen" is fully true.
    snapshotBubbles: [] (empty until T6 LLM enrichment provides real bubbles)
    roadmapHighlights: derived from opps tier counts (not seed record)

  Issue 2 note: roadmap compute fallback (build_roadmap(opps)) is kept.
    This is acceptable because build_roadmap() is deterministic — same opps
    always produce the same roadmap structure. The stronger fix (T2 stores
    roadmap in KV) is a T2 follow-up task, not a T5 blocker. If the team
    wants to pin roadmap bytes in determinism tests, they must first store
    roadmap in T2 materialiser via run_kv_set("roadmap", run_id, roadmap).

  Issue B (status ownership): /status is owned by T2 routes. T5 does not
    touch /status at all. _get_status() is removed from this file.
    Step 4 in README remains "skip".

  No code changes to run_kv_get signatures — already correct in v1.2.

db.py contract (confirmed from uploaded file):
  run_kv_get(key: str, run_id: str, default: Any = None) -> Any
  run_kv_set(key: str, run_id: str, value: Any) -> None
  run_get(run_id: str) -> Dict  — raises HTTPException(404) if not found
  All T5 calls use explicit None default to match contract exactly.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import HTTPException

from .db import get_all, get_one, run_kv_get, run_get
from .roadmap_engine import build_roadmap


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _require_run(run_id: str) -> Dict[str, Any]:
    """db.run_get raises HTTPException(404) directly — no null check needed."""
    return run_get(run_id)


def _require_opps(run_id: str) -> List[Dict[str, Any]]:
    """
    Load run-scoped opps or raise 404.
    No seed fallback — failure is actionable, not silent.
    """
    opps = run_kv_get("opps", run_id, None)
    if opps is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No opportunities found for run '{run_id}'. "
                "T2 materialisation has not completed. "
                "Check GET /api/runs/{run_id}/status."
            ),
        )
    return opps


def _roadmap_highlights_from_opps(opps: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Derive roadmapHighlights from run-scoped opps tier counts.
    Issue 3 / Option A: no seed record used.
    Quick Win → next30, Strategic → next60, Complex → next90.
    """
    return {
        "next30Count":  sum(1 for o in opps if o.get("tier") == "Quick Win"),
        "next60Count":  sum(1 for o in opps if o.get("tier") == "Strategic"),
        "next90Count":  sum(1 for o in opps if o.get("tier") == "Complex"),
        "blockerCount": 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Handler bodies — copy into main.py per README_APPLY_T5.md
# ─────────────────────────────────────────────────────────────────────────────

def _list_evidence(run_id: str) -> List[Dict[str, Any]]:
    """
    Replaces: list_evidence() in main.py

    run_kv_get with explicit None default (db.py contract).
    No seed fallback — 404 with actionable message if KV missing.
    """
    _require_run(run_id)
    run_ev = run_kv_get("evidence", run_id, None)
    if run_ev is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No evidence found for run '{run_id}'. "
                "Ensure PATCH_materialize_t2.py has been applied "
                "and the run has completed successfully."
            ),
        )
    return run_ev


def _get_roadmap(run_id: str) -> Dict[str, Any]:
    """
    Replaces: get_roadmap() in main.py

    Primary: return stored roadmap from KV.
    Fallback: build_roadmap(run_opps) — deterministic, no seed table.
    If opps also missing → 404 via _require_opps().

    Note: T2 follow-up — store roadmap in run_kv_set("roadmap", run_id, ...)
    during materialisation to eliminate the compute fallback entirely.
    """
    _require_run(run_id)
    run_roadmap = run_kv_get("roadmap", run_id, None)
    if run_roadmap is not None:
        return run_roadmap
    # Compute from run-scoped opps — no get_all("opportunities") seed table
    opps = _require_opps(run_id)
    return build_roadmap(opps)


def _get_exec_report(run_id: str) -> Dict[str, Any]:
    """
    Replaces: get_exec_report() in main.py

    Issue 3 / Option A: snapshotBubbles and roadmapHighlights are now fully
    derived from run data — no seed record used anywhere.
      snapshotBubbles: [] until T6 LLM enrichment provides real bubbles
      roadmapHighlights: derived from opps tier counts
      topQuickWins: filtered from run opps (tier == Quick Win)
      sourcesAnalyzed: from run.inputs (unchanged)
      confidence: "MODERATE" constant until T6 computes from signal coverage
    """
    run = _require_run(run_id)

    run_exec = run_kv_get("executive_report", run_id, None)
    if run_exec is not None:
        return run_exec

    # sourcesAnalyzed from run.inputs — not live connector state
    inputs            = run.get("inputs") or {}
    connected_sources = inputs.get("connectedSources") or []
    uploaded_files    = inputs.get("uploadedFiles") or []
    sample_enabled    = bool(inputs.get("sampleWorkspaceEnabled", False))

    connectors    = get_all("connectors")
    name_to_tier  = {c.get("name"): c.get("tier") for c in connectors}
    rec_connected = sum(
        1 for n in connected_sources if name_to_tier.get(n) == "recommended"
    )

    sources_analyzed = {
        "recommendedConnected":   rec_connected,
        "totalConnected":         len(connected_sources),
        "uploadedFiles":          len(uploaded_files),
        "sampleWorkspaceEnabled": sample_enabled,
    }

    # All content derived from run-scoped opps — no seed record
    opps       = _require_opps(run_id)
    quick_wins = [o for o in opps if o.get("tier") == "Quick Win"]

    return {
        "confidence":        "MODERATE",   # T6 will compute from signal coverage
        "sourcesAnalyzed":   sources_analyzed,
        "topQuickWins":      quick_wins,
        "snapshotBubbles":   [],           # T6 LLM enrichment will populate
        "roadmapHighlights": _roadmap_highlights_from_opps(opps),
    }
