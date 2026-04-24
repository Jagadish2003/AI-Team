from typing import List, Dict, Any
from . import db
from .cross_system_linker import build_clusters

def compute_and_store_clusters(run_id: str, evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    clusters = build_clusters(evidence)
    data = [c.model_dump() for c in clusters]
    db.run_kv_set("clusters", run_id, data)
    return data

def get_clusters(run_id: str) -> List[Dict[str, Any]]:
    return db.run_kv_get("clusters", run_id, [])
