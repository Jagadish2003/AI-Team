from __future__ import annotations
from typing import Any, Dict, List
from ..types import DetectorResult

THRESHOLD = 0.4

def detect(ingested: Dict[str, Any]) -> List[DetectorResult]:
    rows = ingested.get("salesforce_knowledge_coverage") or []
    out: List[DetectorResult] = []
    for r in rows:
        closed = float(r.get("closed_count") or 0)
        if closed < 30:
            continue
        with_kb = float(r.get("with_kb_count") or 0)
        gap = 1.0 - (with_kb / closed if closed else 0.0)
        if gap > THRESHOLD:
            out.append(DetectorResult(
                detector_id="KNOWLEDGE_GAP",
                signal_source="Salesforce.CaseArticle",
                metric_name="knowledge_gap_score",
                metric_value=gap,
                threshold=THRESHOLD,
                label="KNOWLEDGE_GAP",
                raw_evidence={"category": r.get("category"), "closed_count": closed, "with_kb_count": with_kb, "gap_score": gap}
            ))
    return out
