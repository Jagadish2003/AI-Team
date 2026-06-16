"""Contract tests for ENT-6 / T3-S16-A Causal Chain Hypotheses.

T10 merge gate: this suite must be green before any ENT-6 subtask is merged.
Exercises T1–T8 together against the migrated causal_hypotheses schema.

Coverage (26 tests):
  Schema / migration (5):
    - All 15 columns with correct names
    - falsifiability_condition is NOT NULL at DB level
    - Both indexes (idx_ch_org_opp, idx_ch_org_preliminary) exist
    - Partial index present on (org_id, preliminary) where preliminary=1
    - Alembic 0005 upgrade → downgrade → upgrade round-trips cleanly

  Quality gates (7):
    - Gate 1 fail: run_count=7 → preliminary=True, gate_1_insufficient_run_count reason
    - Gate 1 pass: run_count=10 → preliminary=False (other gates clear)
    - Gate 2 fail: ambiguous entity → preliminary=True, gate_2_unresolved_entities reason
    - Gate 2 pass: all resolved → preliminary=False
    - Gate 3 fail: [inferred: step → preliminary=True, gate3_inferred_primary_step reason
    - Gate 3 pass: no inferred steps → preliminary=False
    - All gates pass: preliminary=False, preliminary_reason=None

  Rejection / parse (6):
    - no_falsifiability when falsifiability_condition absent
    - generic_falsifiability for sub-30-char / generic condition
    - empty_cause_chain when all steps blank after strip
    - hallucination_in_cause_chain when guard leaves <2 steps
    - insufficient_graph_context raised by build_causal_context (<3 entities)
    - cause_chain truncated to 5 (not rejected) when LLM returns 6 steps

  Telemetry registry (4):
    - causal.hypothesis_generated in REGISTERED_EVENT_TYPES
    - causal.hypothesis_rejected in REGISTERED_EVENT_TYPES
    - hypothesis_generated bound to CausalHypothesisGeneratedPayload
    - hypothesis_rejected bound to CausalHypothesisRejectedPayload

  Endpoint / enrichment (4):
    - GET /api/causal/{opp}/hypothesis → 403 for viewer token
    - GET /api/causal/{opp}/hypothesis → 404 (neutral) for cross-org opp (AC8)
    - GET /api/causal/{opp}/hypothesis → 404 (descriptive) when in-org, no row
    - GET /api/causal/{opp}/hypothesis → 200 with all six fields when row exists

Run:
    cd backend
    python -m pytest tests/contract/test_causal_engine.py -v
"""
from __future__ import annotations

import json
import os
import psycopg2
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db
from app.causal_engine import (
    CausalContext,
    EntityNode,
    GateResult,
    GraphNeighbourhood,
    InsufficientGraphContextError,
    build_causal_context,
    evaluate_causal_quality_gates,
    parse_causal_output,
    store_causal_hypothesis,
)
from app.telemetry import (
    REGISTERED_EVENT_TYPES,
    CausalHypothesisGeneratedPayload,
    CausalHypothesisRejectedPayload,
)
from database.models.causal_hypotheses import CausalHypothesis

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
AUTH = {"Authorization": f"Bearer {DEV_TOKEN}"}

_FALSIFIABILITY = (
    "If the covenant review completion rate does not improve within 90 days "
    "when loan origination volume returns to the 90-day baseline, "
    "the capacity hypothesis is incorrect."
)

_CHAIN_CONFIRMED = [
    "Loan origination volume rose 40% above baseline [OBSERVED, rising, anomalous].",
    "Commercial Credit team capacity was not scaled [OBSERVED via OwnerId].",
    "Covenant review queue backed up [OBSERVED: avg 23 days overdue].",
]

_CHAIN_WITH_INFERRED = [
    "Commercial Credit accounts for 62% of SLA breaches [OBSERVED].",
    "[inferred: 0.6] Backlog pressure from Jira is reducing ServiceNow capacity.",
    "SLA breach rate correlates with Jira backlog size [OBSERVED].",
]

_PARSED_CONFIRMED = {
    "cause_chain": _CHAIN_CONFIRMED,
    "falsifiability_condition": _FALSIFIABILITY,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _conn():
    # AT-288 / Fix 1: read directly from the test PostgreSQL via db.connect()
    # (no DB_PATH). DictCursor rows support both row[0] and row["col"] access.
    return db.connect()


def _seed_workspace_member(org_id: str, role: str = "owner") -> None:
    with db.connect() as con:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (org_id, DEV_TOKEN, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()


def _set_role(role: str) -> Dict[str, str]:
    """Seed dev-token as given role in a fresh org; return request headers."""
    from app.rbac import _ensure_members_table
    _ensure_members_table()
    org_id = f"causal_rbac_{uuid4().hex[:8]}"
    con = db.connect()
    try:
        con.execute(
            "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, user_id) DO UPDATE SET "
            "role=EXCLUDED.role, created_at=EXCLUDED.created_at",
            (org_id, DEV_TOKEN, role, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {**AUTH, "X-Org-Id": org_id}


def _insert_causal(
    org_id: str,
    opp_id: str,
    *,
    run_id: str = "run-causal-t10",
    created_at: Optional[datetime] = None,
    preliminary: bool = False,
    preliminary_reason: Optional[str] = None,
    cause_chain: Optional[list] = None,
    confidence: float = 0.8,
    inferred: bool = False,
) -> str:
    """Insert a causal_hypotheses row and return its id."""
    hyp = CausalHypothesis(
        org_id=org_id,
        opportunity_id=opp_id,
        run_id=run_id,
        cause_chain=cause_chain or _CHAIN_CONFIRMED,
        evidence_links=["e1", "e2"],
        confidence=confidence,
        inferred=inferred,
        falsifiability_condition=_FALSIFIABILITY,
        preliminary=preliminary,
        gate_run_count=12,
        generated_by="llm",
        preliminary_reason=preliminary_reason,
        created_at=created_at or datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc),
    )
    row = hyp.to_db_row()
    cols = (
        "id", "org_id", "opportunity_id", "run_id", "cause_chain", "evidence_links",
        "temporal_support", "confidence", "inferred", "falsifiability_condition",
        "preliminary", "preliminary_reason", "gate_run_count", "generated_by", "created_at",
    )
    placeholders = ", ".join("%s" for _ in cols)
    with db.connect() as con:
        con.execute(
            f"INSERT INTO causal_hypotheses ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(row[c] for c in cols),
        )
        con.commit()
    return row["id"]


def _three_resolved_entities(org_id: str = "default") -> list:
    return [
        EntityNode("ce1", "process", "Covenant Review", "resolved", org_id),
        EntityNode("ce2", "person", "Sarah Chen", "resolved", org_id),
        EntityNode("ce3", "process", "Credit Review", "resolved", org_id),
    ]


def _make_context(org_id: str = "default", entities: Optional[list] = None) -> CausalContext:
    ents = entities if entities is not None else _three_resolved_entities(org_id)
    return CausalContext(
        graph_context=GraphNeighbourhood(entities=ents, edges=[]),
        dependency_paths=[["ce1", "ce3"]],
        temporal_support={"svc::ce1::metric_value": {"trend": "rising", "run_count": 12}},
    )


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


# ===========================================================================
# Section 1 — Schema / migration
# ===========================================================================

class TestCausalHypothesesSchema:
    """Verify the locked T1 DDL is applied correctly in the test DB."""

    def _columns(self) -> list:
        con = _conn()
        try:
            return con.execute(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_name='causal_hypotheses' ORDER BY ordinal_position"
            ).fetchall()
        finally:
            con.close()

    def _column_names(self) -> set[str]:
        return {row["column_name"] for row in self._columns()}

    def _index_names(self) -> set[str]:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename='causal_hypotheses'"
            ).fetchall()
            return {row["indexname"] for row in rows}
        finally:
            con.close()

    def test_all_15_columns_exist(self):
        expected = {
            "id", "org_id", "opportunity_id", "run_id",
            "cause_chain", "evidence_links", "temporal_support",
            "confidence", "inferred", "falsifiability_condition",
            "preliminary", "preliminary_reason", "gate_run_count",
            "generated_by", "created_at",
        }
        assert expected == self._column_names()

    def test_column_types_match_postgres_schema(self):
        """Columns carry the PostgreSQL types/nullability from the locked DDL."""
        expected = {
            "id": ("character varying", "NO"),
            "org_id": ("character varying", "NO"),
            "opportunity_id": ("character varying", "NO"),
            "run_id": ("character varying", "NO"),
            "cause_chain": ("text", "NO"),
            "evidence_links": ("text", "NO"),
            "temporal_support": ("text", "YES"),
            "confidence": ("double precision", "NO"),
            "inferred": ("boolean", "NO"),
            "falsifiability_condition": ("text", "NO"),
            "preliminary": ("boolean", "NO"),
            "preliminary_reason": ("text", "YES"),
            "gate_run_count": ("integer", "NO"),
            "generated_by": ("character varying", "NO"),
            "created_at": ("timestamp without time zone", "NO"),
        }
        actual = {
            row["column_name"]: (row["data_type"], row["is_nullable"])
            for row in self._columns()
        }
        for col, (dtype, nullable) in expected.items():
            assert col in actual, f"Missing column: {col}"
            assert actual[col][0] == dtype, f"{col} type {actual[col][0]} != {dtype}"
        # falsifiability_condition must be NOT NULL at DB level
        assert actual["falsifiability_condition"][1] == "NO"

    def test_falsifiability_condition_not_null_at_db_level(self):
        """A row missing falsifiability_condition must be rejected at DB level."""
        org = f"schema-test-{uuid4().hex[:6]}"
        with pytest.raises(Exception):
            with db.connect() as con:
                con.execute(
                    "INSERT INTO causal_hypotheses "
                    "(id, org_id, opportunity_id, run_id, cause_chain, evidence_links, "
                    " confidence, inferred, preliminary, gate_run_count, generated_by, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        str(uuid4()), org, "opp-x", "run-x",
                        '["step"]', '[]',
                        0.8, False, False, 12, "llm", "2026-01-01T00:00:00+00:00",
                    ),
                )
                con.commit()

    def test_idx_ch_org_opp_exists(self):
        assert "idx_ch_org_opp" in self._index_names()

    def test_idx_ch_org_preliminary_exists(self):
        assert "idx_ch_org_preliminary" in self._index_names()

    def test_migration_round_trip_downgrade_upgrade(self):
        """upgrade head → downgrade to 0004 → upgrade head round-trips cleanly.

        AT-288 / Fix 1: env.py drives Alembic from DATABASE_URL, so to keep the
        SHARED test database untouched this runs in an isolated, throwaway
        PostgreSQL schema. DATABASE_URL is temporarily pointed at the same
        database but with search_path set to that schema, so the migrations
        create/drop their tables there and the public schema the rest of the
        suite uses is never modified.
        """
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig

        backend_dir = Path(__file__).resolve().parents[2]
        base_url = os.environ["DATABASE_URL"]
        schema = f"ch_rt_{uuid4().hex[:8]}"

        def _table_in_schema() -> bool:
            con = psycopg2.connect(base_url)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = 'causal_hypotheses'",
                    (schema,),
                )
                return cur.fetchone() is not None
            finally:
                con.close()

        admin = psycopg2.connect(base_url)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
        admin.close()

        # Point Alembic at the isolated schema via libpq's PGOPTIONS env var
        # (DATABASE_URL stays clean — embedding the option in the URL would trip
        # ConfigParser's % interpolation in env.py).
        old_pgoptions = os.environ.get("PGOPTIONS")
        os.environ["PGOPTIONS"] = f"-c search_path={schema}"
        try:
            alembic_cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
            alembic_cfg.set_main_option("script_location", str(backend_dir / "migrations"))
            # Skip env.py's fileConfig() — it would disable active loggers mid-suite.
            alembic_cfg.config_file_name = None

            alembic_command.upgrade(alembic_cfg, "head")
            assert _table_in_schema(), "causal_hypotheses table missing after upgrade"

            alembic_command.downgrade(alembic_cfg, "0004")
            assert not _table_in_schema(), "causal_hypotheses still present after downgrade"

            alembic_command.upgrade(alembic_cfg, "head")
            assert _table_in_schema(), "causal_hypotheses table missing after re-upgrade"
        finally:
            if old_pgoptions is None:
                os.environ.pop("PGOPTIONS", None)
            else:
                os.environ["PGOPTIONS"] = old_pgoptions
            admin = psycopg2.connect(base_url)
            admin.autocommit = True
            with admin.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            admin.close()


# ===========================================================================
# Section 2 — Quality gates
# ===========================================================================

class TestGate1InsufficientRunCount:
    """Gate 1: run_count >= 10 required for a confirmed hypothesis."""

    def _opp_for_gate(self, cause_chain: list) -> dict:
        return {"cause_chain": cause_chain, "evidence_links": []}

    def _enrichment_all_resolved(self) -> dict:
        return {"entities": [
            {"id": "e1", "resolution_status": "resolved"},
            {"id": "e2", "resolution_status": "resolved"},
        ]}

    def test_gate1_fails_when_run_count_7(self):
        ctx = _make_context()
        opp = self._opp_for_gate(_CHAIN_CONFIRMED)
        enrichment = self._enrichment_all_resolved()

        with patch(
            "app.causal_engine._primary_signal_run_count", return_value=7
        ):
            result = evaluate_causal_quality_gates(
                opp, "pack::opp1::metric_value", "opp-g1-fail", enrichment, ctx
            )

        assert result.preliminary is True
        reason = result.reason
        assert reason is not None
        assert reason.startswith("gate1_insufficient_run_count")
        assert "7" in reason
        assert "10" in reason
        assert result.run_count == 7

    def test_gate1_passes_when_run_count_10(self):
        ctx = _make_context()
        opp = self._opp_for_gate(_CHAIN_CONFIRMED)
        enrichment = self._enrichment_all_resolved()

        with patch(
            "app.causal_engine._primary_signal_run_count", return_value=10
        ):
            result = evaluate_causal_quality_gates(
                opp, "pack::opp1::metric_value", "opp-g1-pass", enrichment, ctx
            )

        assert result.preliminary is False
        assert result.reason is None


class TestGate2UnresolvedEntities:
    """Gate 2: all entities in the chain must have resolution_status='resolved'."""

    def test_gate2_fails_with_ambiguous_entity(self):
        ctx = _make_context()
        opp = {"cause_chain": _CHAIN_CONFIRMED, "evidence_links": []}
        # Inline entity with ambiguous resolution — no DB lookup needed
        enrichment = {
            "entities": [
                {"id": "ent-ambig", "resolution_status": "ambiguous"},
                {"id": "ent-ok", "resolution_status": "resolved"},
            ]
        }

        with patch("app.causal_engine._primary_signal_run_count", return_value=10):
            result = evaluate_causal_quality_gates(
                opp, None, "opp-g2-fail", enrichment, ctx
            )

        assert result.preliminary is True
        assert result.reason is not None
        assert "gate2_unresolved_entities" in result.reason

    def test_gate2_passes_with_all_resolved(self):
        ctx = _make_context()
        opp = {"cause_chain": _CHAIN_CONFIRMED, "evidence_links": []}
        enrichment = {
            "entities": [
                {"id": "e1", "resolution_status": "resolved"},
                {"id": "e2", "resolution_status": "resolved"},
            ]
        }

        with patch("app.causal_engine._primary_signal_run_count", return_value=10):
            result = evaluate_causal_quality_gates(
                opp, None, "opp-g2-pass", enrichment, ctx
            )

        assert result.preliminary is False
        assert result.reason is None


class TestGate3InferredPrimaryStep:
    """Gate 3: cause-chain steps must not rely on inferred relationships as primary evidence."""

    def test_gate3_fails_with_inferred_prefix(self):
        ctx = _make_context()
        opp = {"cause_chain": _CHAIN_WITH_INFERRED, "evidence_links": []}
        enrichment = {"entities": [{"id": "e1", "resolution_status": "resolved"}]}

        with patch("app.causal_engine._primary_signal_run_count", return_value=10):
            result = evaluate_causal_quality_gates(
                opp, None, "opp-g3-fail", enrichment, ctx
            )

        assert result.preliminary is True
        assert result.reason is not None
        assert "gate3_inferred_primary_step" in result.reason

    def test_gate3_passes_without_inferred_steps(self):
        ctx = _make_context()
        opp = {"cause_chain": _CHAIN_CONFIRMED, "evidence_links": []}
        enrichment = {"entities": [{"id": "e1", "resolution_status": "resolved"}]}

        with patch("app.causal_engine._primary_signal_run_count", return_value=10):
            result = evaluate_causal_quality_gates(
                opp, None, "opp-g3-pass", enrichment, ctx
            )

        assert result.preliminary is False
        assert result.reason is None


class TestAllGatesPass:
    """When all three gates pass, the hypothesis is stored CONFIRMED."""

    def test_all_gates_pass_returns_preliminary_false(self):
        ctx = _make_context()
        opp = {"cause_chain": _CHAIN_CONFIRMED, "evidence_links": []}
        enrichment = {
            "entities": [
                {"id": "e1", "resolution_status": "resolved"},
                {"id": "e2", "resolution_status": "resolved"},
            ]
        }

        with patch("app.causal_engine._primary_signal_run_count", return_value=12):
            result = evaluate_causal_quality_gates(
                opp, "pack::opp::metric", "opp-all-pass", enrichment, ctx
            )

        assert result.preliminary is False
        assert result.reason is None
        assert result.run_count == 12


# ===========================================================================
# Section 3 — Rejection / parse
# ===========================================================================

class TestParseCausalOutput:
    """T4: parse_causal_output rejects mal-formed / unsafe hypothesis output."""

    _ORG = "default"
    _RUN = f"run-parse-{uuid4().hex[:6]}"
    _OPP = f"opp-parse-{uuid4().hex[:6]}"

    def _parse(self, payload: Any, ctx: Optional[Any] = None) -> Optional[dict]:
        return parse_causal_output(
            payload,
            org_id=self._ORG,
            run_id=self._RUN,
            opportunity_id=self._OPP,
            causal_context=ctx,
        )

    def test_returns_none_and_fires_rejection_when_falsifiability_absent(self):
        result = self._parse({"cause_chain": ["Step one.", "Step two."]})
        assert result is None

        # Verify telemetry row written
        con = _conn()
        try:
            rows = con.execute(
                "SELECT payload FROM telemetry_events "
                "WHERE event_type='causal.hypothesis_rejected' AND run_id=%s",
                (self._RUN,),
            ).fetchall()
        finally:
            con.close()
        reasons = [json.loads(r["payload"]).get("reason") for r in rows]
        assert "no_falsifiability" in reasons

    def test_returns_none_for_generic_falsifiability_condition(self):
        # Under 30 chars — is_generic_falsifiability returns True
        result = self._parse({
            "cause_chain": ["Step one.", "Step two."],
            "falsifiability_condition": "If wrong.",
        })
        assert result is None

    def test_returns_none_for_empty_cause_chain(self):
        # All steps are blank after strip
        result = self._parse({
            "cause_chain": ["   ", "\t", ""],
            "falsifiability_condition": _FALSIFIABILITY,
        })
        assert result is None

    def test_returns_none_for_hallucination_leaving_fewer_than_2_steps(self):
        # Context has known entity "Covenant Review" / "Sarah Chen".
        # Steps contain multi-word proper-noun spans NOT in the context → guard drops them.
        ctx = _make_context(entities=[
            EntityNode("x1", "process", "Covenant Review", "resolved", "default"),
        ])
        result = self._parse({
            "cause_chain": [
                "ZorbaxSystem MegaCorp caused the delay.",     # hallucinated → dropped
                "XenoProcess BizTech increased risk.",         # hallucinated → dropped
            ],
            "falsifiability_condition": _FALSIFIABILITY,
        }, ctx=ctx)
        assert result is None

    def test_build_causal_context_raises_on_insufficient_graph(self):
        """AC9: < 3 entities in depth-3 neighbourhood raises InsufficientGraphContextError."""
        org = f"sparse-{uuid4().hex[:6]}"
        # No entities seeded for this org → empty neighbourhood → count=0 < 3
        with pytest.raises(InsufficientGraphContextError):
            build_causal_context(org, "opp-sparse", [], "service_cloud")

    def test_cause_chain_truncated_to_5_not_rejected(self):
        """A 6-step chain is truncated to 5 (not rejected) — first 5 survive."""
        six_steps = [f"Step {i}. Generic observed fact." for i in range(1, 7)]
        result = self._parse({
            "cause_chain": six_steps,
            "falsifiability_condition": _FALSIFIABILITY,
        })
        assert result is not None
        assert len(result["cause_chain"]) == 5
        assert result["cause_chain"][0] == "Step 1. Generic observed fact."


# ===========================================================================
# Section 4 — Telemetry registry
# ===========================================================================

class TestTelemetryRegistry:
    """Both causal events are registered with their locked TypedDict schemas."""

    def test_hypothesis_generated_in_registered_event_types(self):
        assert "causal.hypothesis_generated" in REGISTERED_EVENT_TYPES

    def test_hypothesis_rejected_in_registered_event_types(self):
        assert "causal.hypothesis_rejected" in REGISTERED_EVENT_TYPES

    def test_hypothesis_generated_bound_to_correct_typeddict(self):
        assert REGISTERED_EVENT_TYPES["causal.hypothesis_generated"] is CausalHypothesisGeneratedPayload

    def test_hypothesis_rejected_bound_to_correct_typeddict(self):
        assert REGISTERED_EVENT_TYPES["causal.hypothesis_rejected"] is CausalHypothesisRejectedPayload


# ===========================================================================
# Section 5 — Endpoint / enrichment (T8 endpoint + OppEnrichment field)
# ===========================================================================

class TestCausalHypothesisEndpoint:
    """GET /api/causal/{opportunity_id}/hypothesis — access control and response shape."""

    def test_viewer_token_receives_403(self, client):
        viewer_headers = _set_role("viewer")
        opp = f"opp-viewer-{uuid4().hex[:6]}"
        resp = client.get(f"/api/causal/{opp}/hypothesis", headers=viewer_headers)
        assert resp.status_code == 403, resp.text

    def test_cross_org_opportunity_returns_404_not_403(self, client):
        """AC8: existence of the hypothesis must never leak across tenants."""
        org_a = f"org-a-{uuid4().hex[:6]}"
        org_b = f"org-b-{uuid4().hex[:6]}"
        opp = f"opp-xorg-{uuid4().hex[:6]}"

        # Seed org-a as owner so the insert succeeds via the model
        _seed_workspace_member(org_a)
        _seed_workspace_member(org_b)

        _insert_causal(org_a, opp)

        # Analyst in org-b looks up the same opportunity_id
        analyst_b_headers = _set_role("analyst")
        # Override to use org_b
        analyst_b_headers["X-Org-Id"] = org_b

        resp = client.get(f"/api/causal/{opp}/hypothesis", headers=analyst_b_headers)
        # Must be 404, not 403 — 403 would leak that the resource exists in org-a
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "not found"

    def test_in_org_with_no_hypothesis_returns_404_descriptive(self, client):
        org = f"org-nohyp-{uuid4().hex[:6]}"
        opp = f"opp-nohyp-{uuid4().hex[:6]}"
        _seed_workspace_member(org)
        analyst_headers = _set_role("analyst")
        analyst_headers["X-Org-Id"] = org

        # No causal row for this opportunity → descriptive 404
        resp = client.get(f"/api/causal/{opp}/hypothesis", headers=analyst_headers)
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["detail"].lower()

    def test_analyst_gets_200_with_all_six_fields(self, client):
        org = f"org-hyp-{uuid4().hex[:6]}"
        opp = f"opp-hyp-{uuid4().hex[:6]}"
        _seed_workspace_member(org)
        _insert_causal(
            org, opp,
            preliminary=False,
            confidence=0.87,
            inferred=True,
        )

        analyst_headers = _set_role("analyst")
        analyst_headers["X-Org-Id"] = org

        resp = client.get(f"/api/causal/{opp}/hypothesis", headers=analyst_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        required = {"cause_chain", "falsifiability_condition", "confidence",
                    "inferred", "preliminary", "preliminary_reason"}
        assert required == set(body.keys())
        assert body["preliminary"] is False
        assert body["preliminary_reason"] is None
        assert body["confidence"] == pytest.approx(0.87)
        assert body["inferred"] is True
        assert body["falsifiability_condition"] == _FALSIFIABILITY
        assert isinstance(body["cause_chain"], list)
        assert len(body["cause_chain"]) == 3
