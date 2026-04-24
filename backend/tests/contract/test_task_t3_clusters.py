"""
Sprint 4 T3 contract tests — CrossSystem LinkedClusters.

Tests:
  - 404 for unknown runId
  - Clusters endpoint returns list (may be empty on offline run without
    INC-/CS-/JIRA- patterns in evidence)
  - Determinism: two reads of same run return identical response
  - Schema: each cluster has required fields and correct types
  - build_clusters unit test: pure function, no DB/FastAPI dependency
"""
import os
import pytest
from fastapi.testclient import TestClient
from app.main import app


def _auth_headers():
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def client():
    return TestClient(app)


@pytest.fixture(scope="session")
def live_run_id(client):
    """Start a run once and return its runId for all cluster tests."""
    body = {
        "connectedSources":       ["ServiceNow", "Jira & Confluence"],
        "uploadedFiles":          ["upload_001"],
        "sampleWorkspaceEnabled": False,
        "mode":    "offline",
        "systems": ["salesforce", "servicenow", "jira"],
    }
    r = client.post("/api/runs/start", headers=_auth_headers(), json=body)
    assert r.status_code in (200, 201)
    run_id = r.json().get("runId") or r.json().get("id")
    assert run_id, f"No runId in response: {r.json()}"
    return run_id


# ── Endpoint tests ────────────────────────────────────────────────────────────

def test_clusters_unknown_run_404(client):
    """GET /clusters for a non-existent runId must return 404 (no fallback)."""
    r = client.get(
        "/api/runs/run_does_not_exist_xyz/clusters",
        headers=_auth_headers(),
    )
    assert r.status_code == 404, (
        f"Expected 404 for unknown run, got {r.status_code}: {r.text}"
    )


def test_clusters_returns_list(client, live_run_id):
    """GET /clusters must return a JSON array (may be empty)."""
    r = client.get(
        f"/api/runs/{live_run_id}/clusters",
        headers=_auth_headers(),
    )
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list), f"Expected list, got {type(data)}"


def test_clusters_deterministic(client, live_run_id):
    """Two reads of the same run must return identical bytes."""
    c1 = client.get(f"/api/runs/{live_run_id}/clusters", headers=_auth_headers())
    c2 = client.get(f"/api/runs/{live_run_id}/clusters", headers=_auth_headers())
    assert c1.status_code == 200
    assert c2.status_code == 200
    assert c1.text == c2.text, "Clusters not deterministic across two reads"


def test_clusters_schema(client, live_run_id):
    """Each cluster must have the required fields with correct types."""
    r = client.get(f"/api/runs/{live_run_id}/clusters", headers=_auth_headers())
    assert r.status_code == 200
    clusters = r.json()
    for cluster in clusters:
        assert "id"            in cluster, f"Missing 'id' in {cluster}"
        assert "key"           in cluster, f"Missing 'key' in {cluster}"
        assert "normalizedKey" in cluster, f"Missing 'normalizedKey' in {cluster}"
        assert "sources"       in cluster, f"Missing 'sources' in {cluster}"
        assert "evidenceIds"   in cluster, f"Missing 'evidenceIds' in {cluster}"
        assert "summary"       in cluster, f"Missing 'summary' in {cluster}"
        assert "tsEpochMax"    in cluster, f"Missing 'tsEpochMax' in {cluster}"
        assert isinstance(cluster["sources"],     list), "'sources' must be list"
        assert isinstance(cluster["evidenceIds"], list), "'evidenceIds' must be list"
        assert isinstance(cluster["tsEpochMax"],  int),  "'tsEpochMax' must be int"


def test_clusters_auth_required(client):
    """GET /clusters without auth must return 401 or 403."""
    r = client.get("/api/runs/any_run_id/clusters")
    assert r.status_code in (401, 403), (
        f"Expected 401/403 without auth, got {r.status_code}"
    )


# ── Unit tests for build_clusters (no DB, no FastAPI) ─────────────────────────

def test_build_clusters_empty_evidence():
    from app.cross_system_linker import build_clusters
    assert build_clusters([]) == []
    assert build_clusters(None) == []


def test_build_clusters_no_patterns():
    from app.cross_system_linker import build_clusters
    evidence = [
        {"id": "ev_001", "source": "Salesforce",
         "title": "Case closed",
         "snippet": "Customer resolved", "tsLabel": "17 Apr 2026, 10:00"},
    ]
    assert build_clusters(evidence) == []


def test_build_clusters_single_pattern():
    from app.cross_system_linker import build_clusters
    evidence = [
        {"id": "ev_001", "source": "Salesforce",
         "title": "Case CS-1001 closed",
         "snippet": "Customer resolved", "tsLabel": "17 Apr 2026, 10:00"},
        {"id": "ev_002", "source": "ServiceNow",
         "title": "INC-10042 resolved",
         "snippet": "Linked to CS-1001", "tsLabel": "17 Apr 2026, 11:00"},
    ]
    clusters = build_clusters(evidence)
    keys = {c.key for c in clusters}
    assert "CS-1001" in keys
    assert "INC-10042" in keys


def test_build_clusters_multi_source_grouping():
    """Same key appearing in SF and SN evidence → single cluster with both sources."""
    from app.cross_system_linker import build_clusters
    evidence = [
        {"id": "ev_sf_01", "source": "Salesforce",
         "title": "Case mentions INC-9999",
         "snippet": "Owner: Jane", "tsLabel": "17 Apr 2026, 09:00"},
        {"id": "ev_sn_01", "source": "ServiceNow",
         "title": "INC-9999 escalated",
         "snippet": "Linked to Jira JIRA-555", "tsLabel": "17 Apr 2026, 10:00"},
    ]
    clusters = build_clusters(evidence)
    inc_cluster = next((c for c in clusters if c.key == "INC-9999"), None)
    assert inc_cluster is not None
    assert "Salesforce" in inc_cluster.sources
    assert "ServiceNow" in inc_cluster.sources
    assert "ev_sf_01" in inc_cluster.evidenceIds
    assert "ev_sn_01" in inc_cluster.evidenceIds


def test_build_clusters_deterministic_same_input():
    """Same evidence list must always produce identical cluster list."""
    from app.cross_system_linker import build_clusters
    evidence = [
        {"id": "ev_a", "source": "Salesforce",
         "title": "INC-100 opened",    "snippet": "", "tsLabel": "17 Apr 2026, 08:00"},
        {"id": "ev_b", "source": "ServiceNow",
         "title": "INC-100 resolved",  "snippet": "", "tsLabel": "17 Apr 2026, 09:00"},
        {"id": "ev_c", "source": "Jira",
         "title": "JIRA-200 created",  "snippet": "", "tsLabel": "17 Apr 2026, 07:00"},
    ]
    r1 = [c.model_dump() for c in build_clusters(evidence)]
    r2 = [c.model_dump() for c in build_clusters(evidence)]
    assert r1 == r2


def test_build_clusters_ts_epoch_from_ts_label():
    """tsEpochMax must be derived from tsLabel when tsEpoch is absent."""
    from app.cross_system_linker import build_clusters
    evidence = [
        {"id": "ev_001", "source": "Salesforce",
         "title": "INC-777 flagged",
         "snippet": "", "tsLabel": "17 Apr 2026, 14:23"},
    ]
    clusters = build_clusters(evidence)
    assert len(clusters) == 1
    # tsEpochMax must be non-zero (derived from tsLabel)
    assert clusters[0].tsEpochMax > 0, (
        "tsEpochMax should be non-zero when tsLabel is present"
    )


def test_build_clusters_ids_are_positional_and_stable():
    """Cluster IDs are clu_001, clu_002 ... assigned after deterministic sort."""
    from app.cross_system_linker import build_clusters
    evidence = [
        {"id": "ev_a", "source": "SF",
         "title": "INC-001 note", "snippet": "", "tsLabel": "17 Apr 2026, 10:00"},
        {"id": "ev_b", "source": "SN",
         "title": "CS-002 note",  "snippet": "", "tsLabel": "17 Apr 2026, 11:00"},
    ]
    clusters = build_clusters(evidence)
    ids = [c.id for c in clusters]
    assert all(id_.startswith("clu_") for id_ in ids)
    # IDs are sequential from 001
    assert ids[0] == "clu_001"
    if len(clusters) > 1:
        assert ids[1] == "clu_002"
