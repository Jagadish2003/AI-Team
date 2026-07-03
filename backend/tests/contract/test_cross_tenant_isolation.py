"""Cross-tenant leakage enforcing tests — R17-D3 / AT-449 (T4).

Section 3 of R17-D3 asks for an enforcing test that sets up two orgs with data and
proves that, authenticated as org B, NO tenant-sensitive surface returns org A's
data. This file covers every surface the story enumerates:

  * Connector tokens   — stored/read strictly per org (the path fixed in T1/T2).
  * Discovery findings — a run + its opportunities for org A are invisible to B.
  * Ingestion checkpoints (R16-A1) — keyed by (org_id, connector_id).
  * Graph (R16-B1)     — entities / relationships / traversal scoped to the org.

Together they back AC3 ("authenticated as org B, no path returns org A's tokens,
findings, checkpoints, or graph") and AC4 ("two customers connecting the same
connector type do not collide").

Auth model in tests: the static dev token carries no signed org claim, so the
tenancy middleware resolves the request org from the X-Org-Id header (see
middleware/tenancy.py). Sending X-Org-Id therefore authenticates the request as
that org. Role-gated routes (opportunities = viewer+, graph = analyst+) also need
a membership row, seeded via seed_owner(org, dev_token) so the test exercises the
TENANCY guard (404), not the RBAC gate (403).
"""
from __future__ import annotations

import os as _os
import sqlite3
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from database.models.entities import Entity
from database.models.entity_relationships import EntityRelationship

_DEV_TOKEN = "dev-token-change-me"

# Two tenants sharing the one instance. Unique per process so a session-shared DB
# never bleeds state in from a prior run.
ORG_A = "tenant_iso_org_a_" + uuid.uuid4().hex[:8]
ORG_B = "tenant_iso_org_b_" + uuid.uuid4().hex[:8]

# One Fernet key for the whole module so vault store/read round-trips.
_VAULT_KEY = Fernet.generate_key().decode()


def _auth(org_id: str) -> dict:
    """Headers that authenticate the dev user as ``org_id`` (X-Org-Id drives the
    org because the dev token carries no signed org claim)."""
    return {"Authorization": f"Bearer {_DEV_TOKEN}", "X-Org-Id": org_id}


def _vault_env() -> dict:
    return {"CREDENTIAL_VAULT_KEY": _VAULT_KEY}


def _token_response(access_token: str, *, expires_in: int = 7200) -> dict:
    return {
        "access_token": access_token,
        "refresh_token": "refresh-" + access_token,
        "expires_in": expires_in,
        "scope": "read",
    }


# ---------------------------------------------------------------------------
# Graph seeding helpers (mirror the established pattern in test_graph_query.py).
# Native PostgreSQL SQL; sqlite3.connect is routed to the test pool by conftest.
# ---------------------------------------------------------------------------


def _insert_entity(org_id: str, display_name: str, *, run_id: str) -> str:
    entity = Entity(
        org_id=org_id,
        entity_type="person",
        canonical_name=" ".join(display_name.split()).lower() + "-" + uuid.uuid4().hex[:8],
        display_name=display_name,
        source_system="test",
        resolution_confidence=1.0,
        resolution_status="resolved",
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = entity.to_db_row()
    with sqlite3.connect(_os.environ.get("DB_PATH", "")) as conn:
        conn.execute(
            """INSERT INTO entities (
                id, org_id, entity_type, canonical_name, display_name,
                source_system, source_record_id, resolution_confidence,
                resolution_status, first_seen_run_id, last_seen_run_id,
                run_count, metadata, created_at, updated_at
            ) VALUES (
                %(id)s, %(org_id)s, %(entity_type)s, %(canonical_name)s, %(display_name)s,
                %(source_system)s, %(source_record_id)s, %(resolution_confidence)s,
                %(resolution_status)s, %(first_seen_run_id)s, %(last_seen_run_id)s,
                %(run_count)s, %(metadata)s, %(created_at)s, %(updated_at)s
            )""",
            row,
        )
        conn.commit()
    return row["id"]


def _insert_relationship(org_id: str, from_id: str, to_id: str, *, run_id: str) -> str:
    rel = EntityRelationship(
        org_id=org_id,
        from_entity_id=from_id,
        to_entity_id=to_id,
        relationship_type="owns",
        confidence=0.9,
        inferred=False,
        first_seen_run_id=run_id,
        last_seen_run_id=run_id,
        run_count=1,
    )
    row = rel.to_db_row()
    with sqlite3.connect(_os.environ.get("DB_PATH", "")) as conn:
        conn.execute(
            """INSERT INTO entity_relationships (
                id, org_id, from_entity_id, to_entity_id, relationship_type,
                confidence, inferred, evidence, first_seen_run_id,
                last_seen_run_id, run_count, created_at
            ) VALUES (
                %(id)s, %(org_id)s, %(from_entity_id)s, %(to_entity_id)s, %(relationship_type)s,
                %(confidence)s, %(inferred)s, %(evidence)s, %(first_seen_run_id)s,
                %(last_seen_run_id)s, %(run_count)s, %(created_at)s
            )""",
            row,
        )
        conn.commit()
    return row["id"]


# ---------------------------------------------------------------------------
# T4-AC1 / AC4 — connector tokens are stored & read strictly per org
# ---------------------------------------------------------------------------


def test_connector_token_status_is_org_isolated(client: TestClient):
    """A token connected by org A must not show as connected for org B; org B sees
    needs_auth (no token), proving cross-tenant connector access is prevented."""
    from app.auth.vault import store_token
    from app.rbac import seed_owner

    connector = "salesforce"
    # token-status is role-gated (viewer+, csc rbac fix 60a84c3). Seed both orgs so
    # the RBAC gate passes and we assert the tenancy isolation, not a 403.
    seed_owner(ORG_A, _DEV_TOKEN)
    seed_owner(ORG_B, _DEV_TOKEN)
    with _patch_env(_vault_env()):
        store_token(ORG_A, connector, _token_response("orgA-access-token"))

    # Org A — its own freshly stored token reads back as connected.
    own = client.get(f"/api/connectors/{connector}/token-status", headers=_auth(ORG_A))
    assert own.status_code == 200
    assert own.json()["status"] == "connected"

    # Org B — never connected this connector; it must NOT inherit org A's token.
    other = client.get(f"/api/connectors/{connector}/token-status", headers=_auth(ORG_B))
    assert other.status_code == 200
    assert other.json()["status"] == "needs_auth", (
        "org B must not see org A's connector token"
    )


def test_two_orgs_same_connector_tokens_do_not_collide(client: TestClient):
    """AC4: two customers connecting the SAME connector type each hold their own
    isolated token — neither overwrites nor reads the other's."""
    import asyncio

    from app.auth.vault import get_token, store_token

    connector = "jira"
    with _patch_env(_vault_env()):
        store_token(ORG_A, connector, _token_response("orgA-jira-secret"))
        store_token(ORG_B, connector, _token_response("orgB-jira-secret"))

        rec_a = asyncio.run(get_token(ORG_A, connector))
        rec_b = asyncio.run(get_token(ORG_B, connector))

    assert rec_a.access_token == "orgA-jira-secret"
    assert rec_b.access_token == "orgB-jira-secret"
    assert rec_a.access_token != rec_b.access_token, "tokens collided across tenants"


# ---------------------------------------------------------------------------
# T4-AC2 — discovery findings (a run + its opportunities) cannot be retrieved
#          across tenants
# ---------------------------------------------------------------------------


def test_discovery_findings_are_org_isolated(client: TestClient):
    """A run and its opportunities created for org A are 404 to org B (silent deny),
    and never appear in org B's run list."""
    from app import db
    from app.rbac import seed_owner

    # Both orgs are members so the role gate (viewer+) passes and the test
    # exercises the tenancy guard (404), not RBAC (403).
    seed_owner(ORG_A, _DEV_TOKEN)
    seed_owner(ORG_B, _DEV_TOKEN)

    run_id = "run_iso_" + uuid.uuid4().hex[:10]
    db.upsert_run(run_id, {"id": run_id, "org_id": ORG_A, "status": "done"})
    db.run_kv_set(
        "opps",
        run_id,
        [{"id": "opp-A-1", "title": "Org A only finding", "impact": 5, "effort": 2}],
    )

    # Org A — owns the run, so it can read its own findings.
    own = client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(ORG_A))
    assert own.status_code == 200
    assert any(o["id"] == "opp-A-1" for o in own.json())

    # Org B — the run/findings are denied as 404, indistinguishable from not-found.
    assert (
        client.get(f"/api/runs/{run_id}", headers=_auth(ORG_B)).status_code == 404
    )
    assert (
        client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(ORG_B)).status_code
        == 404
    ), "org B must not retrieve org A's discovery findings"

    # Org B's run list must not contain org A's run either.
    listed = client.get("/api/runs", headers=_auth(ORG_B))
    assert listed.status_code == 200
    assert run_id not in [r.get("id") for r in listed.json()]


# ---------------------------------------------------------------------------
# T4-AC3 — ingestion checkpoints remain isolated (keyed by org_id, connector_id)
# ---------------------------------------------------------------------------


def test_ingestion_checkpoints_are_org_isolated(client: TestClient):
    """A checkpoint saved for (org A, connector) is invisible to org B; org B reads
    None — the "first run" state — never org A's position."""
    from discovery.ingest.base import Checkpoint
    from discovery.ingest.checkpoint_repository import read_checkpoint, save_checkpoint

    connector = "servicenow"
    save_checkpoint(
        Checkpoint(
            connector_id=connector,
            org_id=ORG_A,
            value="2026-06-29T00:00:00Z",
            captured_at="2026-06-29T00:00:00Z",
        )
    )

    # Org A reads its own checkpoint back.
    own = read_checkpoint(ORG_A, connector)
    assert own is not None and own.value == "2026-06-29T00:00:00Z"

    # Org B has no checkpoint for this connector — no cross-org read.
    assert read_checkpoint(ORG_B, connector) is None, (
        "org B must not read org A's ingestion checkpoint"
    )


# ---------------------------------------------------------------------------
# T4-AC4 — graph traversal cannot cross tenants
# ---------------------------------------------------------------------------


def test_graph_traversal_is_org_isolated(client: TestClient):
    """Entities, relationships, and every graph query are org-scoped: authenticated
    as org B, no traversal reaches org A's entities, and the org summary is empty."""
    from app.rbac import seed_owner

    seed_owner(ORG_A, _DEV_TOKEN)  # analyst+ gate for the graph routes
    seed_owner(ORG_B, _DEV_TOKEN)

    run_id = "run_graph_iso_" + uuid.uuid4().hex[:8]
    a1 = _insert_entity(ORG_A, "Alice OrgA", run_id=run_id)
    a2 = _insert_entity(ORG_A, "Bob OrgA", run_id=run_id)
    _insert_relationship(ORG_A, a1, a2, run_id=run_id)

    # Org A (owner of the graph) — positive control: it can traverse its own graph
    # and its summary reflects the seeded entities.
    own_nb = client.get(f"/api/graph/entity/{a1}/neighbourhood", headers=_auth(ORG_A))
    assert own_nb.status_code == 200
    own_summary = client.get("/api/graph/org/summary", headers=_auth(ORG_A))
    assert own_summary.status_code == 200
    assert sum(own_summary.json()["entity_counts_by_type"].values()) >= 2

    # Org B — cannot resolve org A's entity (404), cannot path between them (404),
    # and its own org summary is empty (no cross-org entities/relationships).
    nb = client.get(f"/api/graph/entity/{a1}/neighbourhood", headers=_auth(ORG_B))
    assert nb.status_code == 404, "org B must not traverse into org A's entities"

    path = client.get(
        f"/api/graph/path?from_entity_id={a1}&to_entity_id={a2}", headers=_auth(ORG_B)
    )
    assert path.status_code == 404, "org B must not path between org A's entities"

    summary_b = client.get("/api/graph/org/summary", headers=_auth(ORG_B))
    assert summary_b.status_code == 200
    body_b = summary_b.json()
    assert sum(body_b["entity_counts_by_type"].values()) == 0
    assert sum(body_b["relationship_counts_by_type"].values()) == 0


# ---------------------------------------------------------------------------
# T4-AC5 — the headline enforcing test: org B, every surface at once.
# Mirrors the Section 3 pseudocode (test_no_cross_tenant_read): seed org A across
# all surfaces, authenticate as org B, assert nothing of A's is reachable.
# ---------------------------------------------------------------------------


def test_no_cross_tenant_read(client: TestClient):
    """Authenticated as org B, no path returns org A's tokens, findings,
    checkpoints, or graph (R17-D3 Section 3 / AC3)."""
    import asyncio

    from app import db
    from app.auth.vault import get_token, store_token
    from app.rbac import seed_owner
    from discovery.ingest.base import Checkpoint
    from discovery.ingest.checkpoint_repository import read_checkpoint, save_checkpoint

    org_a = "xtenant_A_" + uuid.uuid4().hex[:8]
    org_b = "xtenant_B_" + uuid.uuid4().hex[:8]
    seed_owner(org_a, _DEV_TOKEN)
    seed_owner(org_b, _DEV_TOKEN)

    # --- seed org A across every tenant-sensitive surface ---
    with _patch_env(_vault_env()):
        store_token(org_a, "salesforce", _token_response("A-sf-token"))

    run_id = "xtenant_run_" + uuid.uuid4().hex[:8]
    db.upsert_run(run_id, {"id": run_id, "org_id": org_a, "status": "done"})
    db.run_kv_set("opps", run_id, [{"id": "opp-A", "title": "A finding"}])

    save_checkpoint(
        Checkpoint(
            connector_id="salesforce",
            org_id=org_a,
            value="A-checkpoint",
            captured_at="2026-06-29T00:00:00Z",
        )
    )

    graph_run = "xtenant_graph_" + uuid.uuid4().hex[:8]
    e1 = _insert_entity(org_a, "A Entity One", run_id=graph_run)
    e2 = _insert_entity(org_a, "A Entity Two", run_id=graph_run)
    _insert_relationship(org_a, e1, e2, run_id=graph_run)

    # --- authenticate as org B; assert nothing of A's leaks ---

    # connector tokens
    assert (
        client.get("/api/connectors/salesforce/token-status", headers=_auth(org_b)).json()[
            "status"
        ]
        == "needs_auth"
    )
    with _patch_env(_vault_env()):
        # B holds no token for this connector — the vault refuses to mint one.
        try:
            asyncio.run(get_token(org_b, "salesforce"))
            raise AssertionError("org B unexpectedly retrieved a connector token")
        except Exception as exc:  # ConnectorNotAuthenticatedError
            assert "salesforce" in str(exc) or exc.__class__.__name__ == "ConnectorNotAuthenticatedError"

    # discovery findings
    assert client.get(f"/api/runs/{run_id}", headers=_auth(org_b)).status_code == 404
    assert (
        client.get(f"/api/runs/{run_id}/opportunities", headers=_auth(org_b)).status_code
        == 404
    )
    assert run_id not in [
        r.get("id") for r in client.get("/api/runs", headers=_auth(org_b)).json()
    ]

    # ingestion checkpoints
    assert read_checkpoint(org_b, "salesforce") is None

    # graph
    assert (
        client.get(f"/api/graph/entity/{e1}/neighbourhood", headers=_auth(org_b)).status_code
        == 404
    )
    summary_b = client.get("/api/graph/org/summary", headers=_auth(org_b)).json()
    assert sum(summary_b["entity_counts_by_type"].values()) == 0
    assert sum(summary_b["relationship_counts_by_type"].values()) == 0


# ---------------------------------------------------------------------------
# small env-patch helper (kept local so the module is self-contained)
# ---------------------------------------------------------------------------
from contextlib import contextmanager  # noqa: E402


@contextmanager
def _patch_env(values: dict):
    saved = {k: _os.environ.get(k) for k in values}
    _os.environ.update(values)
    try:
        yield
    finally:
        for k, old in saved.items():
            if old is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = old
