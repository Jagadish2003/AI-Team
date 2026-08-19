"""HP-2.6 — run-level surfacing of provider-caused degradation (story AC5).

**The failure being closed.** HP-2.3 resolves the provider posture at startup and
HP-2.5 reports it on ``GET /api/health`` — but a *run* said nothing. A discovery
run whose generation provider could not be reached still completed, still produced
findings, and polled as ``complete`` with no AI narrative; one whose embedding
provider could not be reached produced findings that could cite no indexed content
while every screen looked normal. That is the same silent degradation HP-2 exists
to remove, surviving on the surfaces a customer actually reads about their run.

What is asserted here, per sub-AC:

1. the run RECORD carries the provider NAME and the REASON;
2. the same run reports it on ``GET /api/runs/{runId}/status`` **and**
   ``GET /api/run-health/degradation``;
3. an unreachable EMBEDDING provider reports distinctly from generation;
4. the wording is composed ONCE, in ``run_completeness`` — asserted structurally
   (no other backend module contains the sentences) *and* behaviourally (the two
   surfaces return byte-identical component payloads);
5. a run with healthy providers is unchanged — no entry appears.

Two properties get extra attention because they are what make this trustworthy
rather than merely present:

*It does not cry wolf.* Only a REACHABILITY failure is reported. A missing
credential and an unconfigured endpoint already refuse boot under
``customer_hosted`` and are a supported configuration under ``saas`` (the shipped
dev/test setup has no key at all), so reporting them would flip every such
deployment's every run to "treat these findings as partial". The gate is proven to
be what suppresses them, not luck — ``TestTheGateIsRealNotAccidental`` widens the
rule and shows the entry appear.

*It leaks no network topology.* The endpoint host stays out of the run record
entirely, so it cannot escape through a run export, a reproducibility diff, or a
viewer-role status poll.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import db, degradation
from app.degradation import CANONICAL_STATUSES, COMPONENT_KINDS, COMPONENT_MODEL
from app.main import app
from app.model_gateway._interface import ROLE_EMBEDDING, ROLE_GENERATION
from app.model_gateway.probe import (
    CHECK_CREDENTIAL,
    CHECK_ENDPOINT_CONFIG,
    CHECK_NOT_RUN,
    CHECK_REACHABILITY,
    ProviderPosture,
    ProviderProbe,
)
from app.run_completeness import (
    POSTURE_SOURCE_RUN_RECORD,
    POSTURE_SOURCE_STARTUP,
    PROVIDER_POSTURE_RUN_FIELD,
    PROVIDER_POSTURE_SCHEMA_VERSION,
    ROLE_COMPONENT_IDS,
    build_run_completeness,
    provider_posture_record,
    stamp_provider_posture,
)

DEV_TOKEN = os.getenv("DEV_JWT", "dev-token-change-me")
VIEWER_TOKEN = os.getenv("VIEWER_JWT", "viewer-token")

BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]

#: Stands in for a customer's internal model host. Deliberately distinctive so a
#: leak-check can look for it in a serialised payload and find it if present.
_INTERNAL_HOST = "models.internal.customer.example"

#: The provider MODE names the gateway registers. No provider brand or endpoint
#: appears in this file — the R16-D1 no-bypass scanners sweep test files too.
_GEN_PROVIDER = "in_boundary"
_EMB_PROVIDER = "customer_tenant"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _auth(org_id: str, token: str = DEV_TOKEN) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Org-Id": org_id}


def _seed_member(org_id: str, user_id: str, role: str = "owner") -> None:
    from app.rbac import _ensure_members_table

    _ensure_members_table()
    with closing(db.connect()) as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO workspace_members (org_id, user_id, role, created_at) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (org_id, user_id, role, datetime.now(timezone.utc).isoformat()),
            )
        con.commit()


def _org() -> str:
    org_id = f"org-hp26-{uuid4().hex[:8]}"
    _seed_member(org_id, DEV_TOKEN)
    _seed_member(org_id, VIEWER_TOKEN, role="viewer")
    return org_id


def _probe(
    role: str,
    status: str,
    *,
    check: str = CHECK_REACHABILITY,
    provider: Optional[str] = None,
    probed: bool = True,
) -> ProviderProbe:
    resolved = provider or (_GEN_PROVIDER if role == ROLE_GENERATION else _EMB_PROVIDER)
    return ProviderProbe(
        role=role,
        provider=resolved,
        env_var=f"MODEL_{role.upper()}_PROVIDER",
        status=status,
        check=check,
        # The probe's own sentence embeds the host; carrying it onto the run would
        # be the leak these tests check for.
        detail=f"{role}: {_INTERNAL_HOST}:11434 is not reachable (connection refused).",
        endpoint_host=_INTERNAL_HOST,
        probed=probed,
    )


def _posture(
    gen: str,
    emb: str,
    *,
    check: str = CHECK_REACHABILITY,
) -> ProviderPosture:
    return ProviderPosture(
        roles=[
            _probe(ROLE_GENERATION, gen, check=check),
            _probe(ROLE_EMBEDDING, emb, check=check),
        ]
    )


def _with_posture(posture: Optional[ProviderPosture]):
    """Patch the gateway's CACHED posture. Never probes; never touches a socket."""
    return patch("app.model_gateway.provider_posture", return_value=posture)


def _record_for(posture: Optional[ProviderPosture]) -> Optional[Dict[str, Any]]:
    with _with_posture(posture):
        return provider_posture_record()


def _opp(index: int) -> Dict[str, Any]:
    return {
        "id": f"opp_{index:03d}",
        "opportunity_identity": f"hp26_ident_{index:03d}",
        "title": f"Finding {index}",
        "category": "Workflow",
        "tier": "Quick Win",
        "impact": 7,
        "effort": 3,
        "confidence": "HIGH",
        "aiRationale": "seeded",
        "evidenceIds": [f"ev_{index}"],
        "decision": "UNREVIEWED",
        "override": {
            "isLocked": False,
            "rationaleOverride": "",
            "overrideReason": "",
            "updatedAt": None,
        },
        "packId": "service_cloud",
        "_debug": {"detector_id": "HANDOFF_FRICTION"},
    }


def _seed_run(posture: Optional[ProviderPosture]) -> str:
    """A materialised, otherwise-CLEAN run that recorded ``posture``.

    Every source succeeded, so any degradation the surfaces report can only have
    come from the provider posture — nothing else is wrong with this run.
    """
    from app.db import run_get, run_kv_set, run_set
    from app.run_store import start_run_

    run_id = start_run_({"pack": "service_cloud"})["runId"]
    record = run_get(run_id) or {}
    record.update(
        {
            "inputs": {"systems": ["salesforce", "servicenow", "jira"]},
            "succeeded": ["salesforce", "servicenow", "jira"],
            "ingestErrors": {},
            "status": "complete",
        }
    )
    if posture is not None:
        with _with_posture(posture):
            stamp_provider_posture(record)
    run_set(run_id, record)
    run_kv_set("opps", run_id, [_opp(i) for i in range(3)])
    return run_id


UNAVAILABLE = degradation.STATUS_UNAVAILABLE
OK = degradation.STATUS_OK

GEN_DOWN = _posture(UNAVAILABLE, OK)
EMB_DOWN = _posture(OK, UNAVAILABLE)
BOTH_DOWN = _posture(UNAVAILABLE, UNAVAILABLE)
ALL_HEALTHY = _posture(OK, OK)

GEN_COMPONENT = ROLE_COMPONENT_IDS[ROLE_GENERATION]
EMB_COMPONENT = ROLE_COMPONENT_IDS[ROLE_EMBEDDING]


def _model_components(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The provider-degradation entries out of a ``completeness`` payload.

    Filtered to the posture-derived ones by ``detail.postureSource``, so the
    pre-existing "the hosted mode has no embeddings endpoint" configuration entry
    (a different fact, deliberately still reported) never stands in for one.
    """
    return [
        c
        for c in payload.get("components", [])
        if c.get("kind") == COMPONENT_MODEL
        and (c.get("detail") or {}).get("postureSource")
    ]


def _status_completeness(client: TestClient, org: str, run_id: str) -> Dict[str, Any]:
    body = client.get(f"/api/runs/{run_id}/status", headers=_auth(org))
    assert body.status_code == 200, body.text
    return body.json()["completeness"]


def _degradation_completeness(
    client: TestClient, org: str, run_id: str
) -> Dict[str, Any]:
    body = client.get(
        "/api/run-health/degradation", params={"run_id": run_id}, headers=_auth(org)
    )
    assert body.status_code == 200, body.text
    return body.json()["completeness"]


# ---------------------------------------------------------------------------
# Sub-AC 1 — the run record carries the provider name and the reason
# ---------------------------------------------------------------------------


class TestTheRunRecordCarriesProviderAndReason:
    def test_the_stamp_names_the_provider_and_the_variable_that_selected_it(self):
        record = _record_for(GEN_DOWN)
        assert record is not None
        gen = record["roles"][ROLE_GENERATION]
        assert gen["provider"] == _GEN_PROVIDER
        assert gen["variable"] == "MODEL_GENERATION_PROVIDER"
        assert gen["status"] == UNAVAILABLE
        assert gen["check"] == CHECK_REACHABILITY

    def test_the_stamp_declares_a_schema_version(self):
        """A stored shape with no version cannot be changed safely later."""
        assert _record_for(GEN_DOWN)["schemaVersion"] == PROVIDER_POSTURE_SCHEMA_VERSION

    def test_the_stamp_rolls_up_as_badly_as_its_worst_role(self):
        assert _record_for(GEN_DOWN)["status"] == UNAVAILABLE
        assert _record_for(ALL_HEALTHY)["status"] == OK

    def test_the_reason_is_on_the_run_record_as_a_readable_sentence(self):
        """AC5 asks for the reason, not just a status code."""
        run = {"runId": "r", PROVIDER_POSTURE_RUN_FIELD: _record_for(GEN_DOWN)}
        component = next(
            c
            for c in build_run_completeness(run, include_environment=False).components
            if c.component == GEN_COMPONENT
        )
        reason = component.reason or ""
        assert _GEN_PROVIDER in reason
        assert "MODEL_GENERATION_PROVIDER" in reason
        assert "unreachable" in reason.lower()

    def test_reading_the_posture_never_probes(self):
        """A materializer that opened a socket per run would make this platform
        something that hammers the customer's own model server — the same reason
        HP-2.5 refuses to probe on a health read."""
        with patch("app.model_gateway.probe.tcp_connect") as connect:
            with _with_posture(GEN_DOWN):
                provider_posture_record()
        assert connect.call_count == 0

    def test_an_unevaluated_posture_writes_no_field_at_all(self):
        """Absent must stay absent. An empty record would read as
        'recorded, and fine' — the exact false reassurance HP-2 removes."""
        run: Dict[str, Any] = {"runId": "r"}
        with _with_posture(None):
            assert stamp_provider_posture(run) is None
        assert PROVIDER_POSTURE_RUN_FIELD not in run

    def test_a_run_recorded_before_hp26_is_never_described(self):
        """No backfill: a run with no stamp is reported on by nothing, rather than
        being described using today's environment."""
        completeness = build_run_completeness(
            {"runId": "old", "inputs": {"systems": ["salesforce"]},
             "succeeded": ["salesforce"]},
            include_environment=False,
        )
        assert completeness.complete is True
        assert not _model_components(completeness.to_dict())

    def test_the_stamp_carries_no_endpoint_host(self):
        """The host is internal network topology, and a run record is served to
        viewers, exported, and diffed."""
        blob = json.dumps(_record_for(BOTH_DOWN))
        assert _INTERNAL_HOST not in blob
        assert "11434" not in blob

    def test_a_stamp_failure_never_breaks_a_run(self):
        run: Dict[str, Any] = {"runId": "r"}
        with patch(
            "app.model_gateway.provider_posture", side_effect=RuntimeError("boom")
        ):
            assert stamp_provider_posture(run) is None
        assert PROVIDER_POSTURE_RUN_FIELD not in run


class TestBothMaterializersRecordThePosture:
    """The stamp has to be written by the path the product actually uses.

    ``POST /api/runs/start`` runs ``materialize_t2``; the product journey is
    ``POST /api/stack-builder/launch`` then ``POST /api/runs/{id}/compute``, which
    runs ``routes_sprint4_t1``. 2.0-D4 T3's reproducibility record was wired into
    the first only — so stamping one would have left the real journey silent.
    """

    @pytest.mark.parametrize(
        "module_name,function_name",
        [
            ("app.materialize_t2", "run_trackb_and_persist"),
            ("app.routes_sprint4_t1", "_run_trackb_and_persist"),
        ],
    )
    def test_the_materializer_stamps_the_posture(self, module_name, function_name):
        import importlib

        module = importlib.import_module(module_name)
        source = inspect.getsource(getattr(module, function_name))
        assert "stamp_provider_posture" in source, (
            f"{module_name}.{function_name} does not record the provider posture, "
            "so runs materialised through it report no provider degradation"
        )

    @pytest.mark.parametrize(
        "module_name,function_name",
        [
            ("app.materialize_t2", "run_trackb_and_persist"),
            ("app.routes_sprint4_t1", "_run_trackb_and_persist"),
        ],
    )
    def test_the_stamp_precedes_the_pipeline(self, module_name, function_name):
        """Stamped before the ingest, so the 'no data ingested' exit carries it
        too — a run that failed still ran under some provider posture."""
        import importlib

        module = importlib.import_module(module_name)
        source = inspect.getsource(getattr(module, function_name))
        assert source.index("stamp_provider_posture") < source.index("trackb_run"), (
            "the posture is stamped after the pipeline starts, so an early exit "
            "loses it"
        )


class TestARealRunRecordsThePosture:
    """Empirical proof rather than a source assertion: drive a real run.

    Everything above establishes the mechanism. This establishes that the
    mechanism is actually reached when a run happens for real, which a source
    inspection cannot show.
    """

    def test_a_real_run_persists_the_posture_on_its_record(self, client):
        org = _org()
        with _with_posture(GEN_DOWN):
            started = client.post(
                "/api/runs/start",
                headers=_auth(org),
                json={
                    "mode": "offline",
                    "systems": ["salesforce", "servicenow", "jira"],
                    "connectedSources": ["salesforce", "servicenow", "jira"],
                    "uploadedFiles": [],
                    "sampleWorkspaceEnabled": True,
                },
            )
            assert started.status_code in (200, 201), started.text
            run_id = started.json()["runId"]

            deadline = time.time() + 120
            status = "running"
            while time.time() < deadline:
                body = client.get(f"/api/runs/{run_id}/status", headers=_auth(org))
                if body.status_code == 200:
                    status = body.json().get("status", "running")
                    if status in ("complete", "partial", "failed"):
                        break
                time.sleep(1)

        assert status in ("complete", "partial", "failed"), status

        record = db.run_get(run_id) or {}
        stamp = record.get(PROVIDER_POSTURE_RUN_FIELD)
        assert isinstance(stamp, dict), (
            "a real run did not persist the provider posture — the stamp is not "
            f"reached on the live path (run status was {status!r})"
        )
        assert stamp["roles"][ROLE_GENERATION]["provider"] == _GEN_PROVIDER
        assert stamp["roles"][ROLE_GENERATION]["status"] == UNAVAILABLE
        assert _INTERNAL_HOST not in json.dumps(stamp)

        # And it surfaces, on the run it belongs to, with no patch in effect.
        completeness = _status_completeness(client, org, run_id)
        assert any(
            c["component"] == GEN_COMPONENT for c in _model_components(completeness)
        ), "the stamped posture did not reach the run's own status surface"


# ---------------------------------------------------------------------------
# Sub-AC 2 — both surfaces report it, for the same run
# ---------------------------------------------------------------------------


class TestBothSurfacesReportTheSameRun:
    def test_the_run_status_surface_names_the_provider(self, client):
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(GEN_DOWN))
        component = next(
            c for c in _model_components(completeness) if c["component"] == GEN_COMPONENT
        )
        assert _GEN_PROVIDER in (component["reason"] or "")
        assert completeness["complete"] is False

    def test_the_degradation_surface_names_the_provider(self, client):
        org = _org()
        completeness = _degradation_completeness(client, org, _seed_run(GEN_DOWN))
        component = next(
            c for c in _model_components(completeness) if c["component"] == GEN_COMPONENT
        )
        assert _GEN_PROVIDER in (component["reason"] or "")
        assert completeness["complete"] is False

    def test_the_two_surfaces_return_byte_identical_components(self, client):
        """The strongest available form of 'composed once'.

        Equal payloads cannot be produced by two independent compositions that
        happen to agree today.
        """
        org = _org()
        run_id = _seed_run(BOTH_DOWN)
        from_status = _model_components(_status_completeness(client, org, run_id))
        from_health = _model_components(_degradation_completeness(client, org, run_id))
        assert from_status == from_health
        assert len(from_status) == 2

    def test_the_headline_names_the_provider_component(self, client):
        """The one sentence a surface shows must not omit the cause."""
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(GEN_DOWN))
        assert GEN_COMPONENT in completeness["headline"]
        assert "INCOMPLETE" in completeness["headline"]

    def test_the_missing_summary_lists_it(self, client):
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(EMB_DOWN))
        assert any(EMB_COMPONENT in line for line in completeness["missing"])

    def test_the_executive_report_says_so_too(self, client):
        """The artifact most likely to reach someone who never opens a health
        panel. It reads the same RunCompleteness, so it inherits this for free —
        asserted because 'for free' is a claim, not a guarantee."""
        org = _org()
        run_id = _seed_run(GEN_DOWN)
        report = client.get(
            f"/api/runs/{run_id}/executive-report", headers=_auth(org)
        ).json()
        assert report["runCompleteness"]["complete"] is False
        assert any(
            c["component"] == GEN_COMPONENT
            for c in _model_components(report["runCompleteness"])
        )

    def test_no_surface_leaks_the_endpoint_host(self, client):
        org = _org()
        run_id = _seed_run(BOTH_DOWN)
        for payload in (
            _status_completeness(client, org, run_id),
            _degradation_completeness(client, org, run_id),
            client.get(
                f"/api/runs/{run_id}/executive-report", headers=_auth(org)
            ).json(),
        ):
            assert _INTERNAL_HOST not in json.dumps(payload)

    def test_a_viewer_polling_status_sees_the_degradation(self, client):
        """The status route is viewer+. A degradation only an analyst can see is
        not surfaced to the person watching the run."""
        org = _org()
        run_id = _seed_run(GEN_DOWN)
        body = client.get(
            f"/api/runs/{run_id}/status", headers=_auth(org, VIEWER_TOKEN)
        )
        assert body.status_code == 200, body.text
        assert body.json()["completeness"]["complete"] is False

    def test_the_run_scoped_surfaces_report_what_the_run_recorded_not_today(
        self, client
    ):
        """A historical run must not be re-described by the current environment.

        The run's own record is authoritative for that run; otherwise today's
        outage rewrites last month's report and nobody can reconcile the two.
        """
        org = _org()
        run_id = _seed_run(GEN_DOWN)
        with _with_posture(ALL_HEALTHY):
            components = _model_components(
                _degradation_completeness(client, org, run_id)
            )
        assert [c["component"] for c in components] == [GEN_COMPONENT]
        assert components[0]["detail"]["postureSource"] == POSTURE_SOURCE_RUN_RECORD

    def test_a_request_with_no_run_answers_from_the_live_posture(self, client):
        """``/api/run-health/degradation`` with no run id is a question about NOW,
        so the cached startup posture is the only answer there is."""
        org = _org()
        with _with_posture(BOTH_DOWN):
            body = client.get(
                "/api/run-health/degradation", headers=_auth(org)
            ).json()
        components = _model_components(body["completeness"])
        assert {c["component"] for c in components} == {GEN_COMPONENT, EMB_COMPONENT}
        assert all(
            c["detail"]["postureSource"] == POSTURE_SOURCE_STARTUP for c in components
        )

    def test_a_stamped_run_is_not_double_reported(self, client):
        """The record wins over the live posture, so one problem is one entry."""
        org = _org()
        run_id = _seed_run(BOTH_DOWN)
        with _with_posture(BOTH_DOWN):
            components = _model_components(
                _degradation_completeness(client, org, run_id)
            )
        ids = [c["component"] for c in components]
        assert sorted(ids) == [EMB_COMPONENT, GEN_COMPONENT], ids


# ---------------------------------------------------------------------------
# Sub-AC 3 — embedding is reported distinctly from generation
# ---------------------------------------------------------------------------


class TestEmbeddingIsDistinctFromGeneration:
    def test_the_two_roles_use_different_component_ids(self):
        """Structural, not a wording convention: a consumer can filter on the id."""
        assert GEN_COMPONENT != EMB_COMPONENT
        assert set(ROLE_COMPONENT_IDS) == {ROLE_GENERATION, ROLE_EMBEDDING}

    def test_an_unreachable_embedding_provider_reports_alone(self, client):
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(EMB_DOWN))
        assert [c["component"] for c in _model_components(completeness)] == [
            EMB_COMPONENT
        ]

    def test_an_unreachable_generation_provider_reports_alone(self, client):
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(GEN_DOWN))
        assert [c["component"] for c in _model_components(completeness)] == [
            GEN_COMPONENT
        ]

    def test_both_down_reports_two_entries(self, client):
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(BOTH_DOWN))
        assert {c["component"] for c in _model_components(completeness)} == {
            GEN_COMPONENT,
            EMB_COMPONENT,
        }

    def test_each_role_names_its_own_provider_and_variable(self, client):
        """The shipped configuration deliberately MIXES the two providers, so one
        collapsed 'AI unavailable' entry would be wrong for the default
        deployment — and would name the wrong variable to fix."""
        org = _org()
        by_id = {
            c["component"]: c
            for c in _model_components(
                _status_completeness(client, org, _seed_run(BOTH_DOWN))
            )
        }
        assert _GEN_PROVIDER in by_id[GEN_COMPONENT]["reason"]
        assert "MODEL_GENERATION_PROVIDER" in by_id[GEN_COMPONENT]["reason"]
        assert _EMB_PROVIDER in by_id[EMB_COMPONENT]["reason"]
        assert "MODEL_EMBEDDING_PROVIDER" in by_id[EMB_COMPONENT]["reason"]

    def test_what_each_role_costs_the_run_is_stated_differently(self, client):
        """Losing narrative and losing citations are different losses. Collapsing
        them tells a reader nothing about which half of the product they lost."""
        org = _org()
        by_id = {
            c["component"]: c
            for c in _model_components(
                _status_completeness(client, org, _seed_run(BOTH_DOWN))
            )
        }
        gen_missing = by_id[GEN_COMPONENT]["missing"].lower()
        emb_missing = by_id[EMB_COMPONENT]["missing"].lower()
        assert gen_missing != emb_missing
        assert "narrative" in gen_missing
        assert "cite" in emb_missing or "retrieval" in emb_missing

    def test_the_role_ids_are_pinned_to_the_gateway_role_names(self):
        """A rename in the gateway must not silently orphan a component id."""
        assert ROLE_COMPONENT_IDS[ROLE_GENERATION] == "generation_provider"
        assert ROLE_COMPONENT_IDS[ROLE_EMBEDDING] == "embedding_provider"


# ---------------------------------------------------------------------------
# Sub-AC 4 — the wording is composed once
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset(
    {".venv", "node_modules", "__pycache__", ".git", "build", "dist", "tests"}
)

#: Distinctive phrases from the composed provider-degradation copy. A phrase in a
#: second module means a second surface has started composing its own wording.
_COMPOSED_PHRASES = (
    "was unreachable at ",
    "AI-assisted narrative (summaries",
    "Restore network reach to the configured ",
)

_WORDING_OWNER = BACKEND_ROOT / "app" / "run_completeness.py"


def _production_python_files():
    for path in BACKEND_ROOT.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _offending_composers(paths) -> List[str]:
    """Files other than the owner that contain the composed copy.

    Extracted so the negative control below exercises the REAL sweep rather than
    a restatement of it.
    """
    offenders: List[str] = []
    for path in paths:
        if path.resolve() == _WORDING_OWNER.resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover
            continue
        for phrase in _COMPOSED_PHRASES:
            if phrase in text:
                offenders.append(f"{path.name} :: {phrase!r}")
    return offenders


class TestTheWordingIsComposedOnce:
    def test_only_run_completeness_contains_the_composed_copy(self):
        offenders = _offending_composers(_production_python_files())
        assert offenders == [], (
            "these modules compose provider-degradation wording of their own; it "
            f"must come from RunCompleteness: {offenders}"
        )

    def test_the_owner_actually_contains_the_copy(self):
        """Guard against a vacuous pass — if the phrases moved, the sweep above
        would be checking for strings nothing produces."""
        text = _WORDING_OWNER.read_text(encoding="utf-8")
        for phrase in _COMPOSED_PHRASES:
            assert phrase in text, phrase

    def test_the_guard_goes_red_for_a_second_composer(self, tmp_path):
        """Negative control: a guard never observed failing is not a guard."""
        offending = tmp_path / "routes_something.py"
        offending.write_text(
            'MESSAGE = "the provider was unreachable at the last check"\n',
            encoding="utf-8",
        )
        assert _offending_composers([offending]) != []
        # And the owner itself is never an offender, or the sweep would be broken
        # in the other direction.
        assert _offending_composers([_WORDING_OWNER]) == []

    def test_neither_route_module_composes_provider_copy(self):
        """The two AC-named surfaces render the payload; they do not write it."""
        for module in ("app/routes_sprint4_t2.py", "app/routes_run_health.py"):
            text = (BACKEND_ROOT / module).read_text(encoding="utf-8")
            assert "build_run_completeness" in text
            for phrase in _COMPOSED_PHRASES:
                assert phrase not in text, f"{module} composes {phrase!r}"

    def test_one_rule_decides_what_counts_as_a_degradation(self):
        """HP-2.5 already decided which provider conditions are a real problem.
        A second copy of that rule here would be free to drift — the defect HP-1
        exists to remove, in a different guise."""
        from app import run_completeness

        source = inspect.getsource(run_completeness)
        assert "role_degrades_health" in source, (
            "run_completeness re-implements the degradation rule instead of "
            "reusing app.model_provider_health.role_degrades_health"
        )

    def test_the_stamp_writer_and_reader_live_in_one_module(self):
        """A shape written elsewhere and parsed here could disagree."""
        from app import run_completeness

        assert hasattr(run_completeness, "stamp_provider_posture")
        assert hasattr(run_completeness, "provider_posture_record")
        for module in ("app/materialize_t2.py", "app/routes_sprint4_t1.py"):
            text = (BACKEND_ROOT / module).read_text(encoding="utf-8")
            assert PROVIDER_POSTURE_RUN_FIELD not in text, (
                f"{module} names the run field directly instead of calling "
                "stamp_provider_posture — two places to change, one to forget"
            )


# ---------------------------------------------------------------------------
# Sub-AC 5 — a healthy run is unchanged
# ---------------------------------------------------------------------------


class TestAHealthyRunIsUnchanged:
    def test_a_healthy_posture_produces_no_entry(self, client):
        org = _org()
        run_id = _seed_run(ALL_HEALTHY)
        for payload in (
            _status_completeness(client, org, run_id),
            _degradation_completeness(client, org, run_id),
        ):
            assert not _model_components(payload)

    def test_a_healthy_run_still_reads_complete(self, client):
        """A degradation surface that cries wolf is one people stop reading."""
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(ALL_HEALTHY))
        assert completeness["complete"] is True
        assert completeness["status"] == degradation.STATUS_OK

    def test_the_healthy_posture_is_still_recorded_on_the_run(self):
        """Nothing is hidden: the run records the full posture either way. What is
        narrow is which conditions become a DEGRADATION."""
        record = _record_for(ALL_HEALTHY)
        assert record["status"] == OK
        assert set(record["roles"]) == {ROLE_GENERATION, ROLE_EMBEDDING}

    @pytest.mark.parametrize(
        "check,status",
        [
            (CHECK_CREDENTIAL, degradation.STATUS_UNAVAILABLE),
            (CHECK_ENDPOINT_CONFIG, degradation.STATUS_UNAVAILABLE),
            (CHECK_NOT_RUN, degradation.STATUS_UNKNOWN),
        ],
    )
    def test_a_non_reachability_condition_does_not_degrade_a_run(
        self, client, check, status
    ):
        """HP-2.5's rule, applied here.

        A missing credential or an unconfigured endpoint already refuses boot
        under ``customer_hosted``, and under ``saas`` is a supported configuration
        (LLM enrichment is optional by design; the shipped dev/test setup has no
        key). ``unknown`` means nobody looked. Reporting any of them would make
        every such run read 'treat these findings as partial'.
        """
        org = _org()
        posture = _posture(status, status, check=check)
        completeness = _status_completeness(client, org, _seed_run(posture))
        assert not _model_components(completeness)
        assert completeness["complete"] is True

    def test_probing_disabled_is_not_reported_as_a_failure(self, client):
        """The default in tests and in any deployment that sets the timeout to 0."""
        org = _org()
        posture = ProviderPosture(
            roles=[
                _probe(
                    ROLE_GENERATION,
                    degradation.STATUS_UNKNOWN,
                    check=CHECK_NOT_RUN,
                    probed=False,
                ),
                _probe(
                    ROLE_EMBEDDING,
                    degradation.STATUS_UNKNOWN,
                    check=CHECK_NOT_RUN,
                    probed=False,
                ),
            ]
        )
        assert _status_completeness(client, org, _seed_run(posture))["complete"] is True

    def test_an_unrelated_run_is_untouched_by_this_change(self, client):
        """A run seeded exactly as the 2.0-D4 suite seeds a clean one still reads
        clean — HP-2.6 is additive, not a new source of noise."""
        org = _org()
        completeness = _status_completeness(client, org, _seed_run(None))
        assert completeness["complete"] is True
        assert completeness["components"] == []


class TestTheGateIsRealNotAccidental:
    """A suppression nobody has watched suppress is not known to suppress."""

    def test_widening_the_rule_makes_the_credential_case_appear(self):
        """Proves the reachability gate is WHY a credential failure is silent —
        not that the code path is simply never reached."""
        run = {
            "runId": "r",
            PROVIDER_POSTURE_RUN_FIELD: _record_for(
                _posture(UNAVAILABLE, OK, check=CHECK_CREDENTIAL)
            ),
        }
        gated = build_run_completeness(run, include_environment=False)
        assert not _model_components(gated.to_dict())

        with patch(
            "app.model_provider_health.role_degrades_health", return_value=True
        ):
            widened = build_run_completeness(run, include_environment=False)
        components = _model_components(widened.to_dict())
        # The generation role's CREDENTIAL failure now reports, which is the
        # claim: the entry is suppressed by the rule, not by an unreached path.
        # (The widened stub admits the healthy embedding role too — irrelevant
        # here, and asserting an exact list would test the stub, not the gate.)
        generation = [c for c in components if c["component"] == GEN_COMPONENT]
        assert generation, [c["component"] for c in components]
        assert generation[0]["detail"]["check"] == CHECK_CREDENTIAL

    def test_narrowing_the_rule_makes_the_reachability_case_vanish(self):
        """The mirror control: the entry exists BECAUSE the rule admits it."""
        run = {"runId": "r", PROVIDER_POSTURE_RUN_FIELD: _record_for(GEN_DOWN)}
        assert _model_components(
            build_run_completeness(run, include_environment=False).to_dict()
        )
        with patch(
            "app.model_provider_health.role_degrades_health", return_value=False
        ):
            assert not _model_components(
                build_run_completeness(run, include_environment=False).to_dict()
            )


# ---------------------------------------------------------------------------
# Uniformity, robustness, and the pre-existing configuration entry
# ---------------------------------------------------------------------------


class TestItUsesTheCanonicalVocabulary:
    def test_every_entry_is_a_model_component_with_a_canonical_status(self, client):
        org = _org()
        for c in _model_components(
            _status_completeness(client, org, _seed_run(BOTH_DOWN))
        ):
            assert c["kind"] in COMPONENT_KINDS
            assert c["kind"] == COMPONENT_MODEL
            assert c["status"] in CANONICAL_STATUSES

    def test_every_entry_carries_all_four_mandatory_parts(self, client):
        """A degradation report missing any of them cannot be acted on."""
        org = _org()
        for c in _model_components(
            _status_completeness(client, org, _seed_run(BOTH_DOWN))
        ):
            assert c["attempted"], c
            assert c["missing"], c
            assert c["reason"], c
            assert c["remedy"], c

    def test_the_remedy_names_the_variable_and_where_the_host_is(self, client):
        """Withholding the host is only acceptable if the remedy says where it
        went — otherwise the omission is just a missing fact."""
        org = _org()
        component = next(
            c
            for c in _model_components(
                _status_completeness(client, org, _seed_run(GEN_DOWN))
            )
            if c["component"] == GEN_COMPONENT
        )
        remedy = component["remedy"]
        assert "MODEL_GENERATION_PROVIDER" in remedy
        assert "startup log" in remedy.lower()

    def test_the_payload_is_json_serialisable(self, client):
        org = _org()
        payload = _status_completeness(client, org, _seed_run(BOTH_DOWN))
        assert json.loads(json.dumps(payload)) == payload

    def test_an_unrecognised_status_is_not_reported_as_a_failure(self):
        """``canonical_status`` maps an unknown word to ``unknown``, which does not
        degrade — a word this module was not taught is not evidence of breakage."""
        run = {
            "runId": "r",
            PROVIDER_POSTURE_RUN_FIELD: {
                "schemaVersion": PROVIDER_POSTURE_SCHEMA_VERSION,
                "status": "wobbly",
                "roles": {
                    ROLE_GENERATION: {
                        "provider": _GEN_PROVIDER,
                        "variable": "MODEL_GENERATION_PROVIDER",
                        "status": "wobbly",
                        "check": CHECK_REACHABILITY,
                        "probed": True,
                    }
                },
            },
        }
        completeness = build_run_completeness(run, include_environment=False)
        assert not _model_components(completeness.to_dict())

    @pytest.mark.parametrize(
        "stamp",
        [
            "not-a-mapping",
            {"roles": "not-a-mapping"},
            {"roles": {ROLE_GENERATION: "not-a-mapping"}},
            {"roles": {}},
            {},
        ],
    )
    def test_a_malformed_stamp_never_raises(self, stamp):
        """Completeness must answer for any run, including one with a mangled
        record — a check that fell over leaves the surface saying nothing, which
        defaults to looking fine."""
        completeness = build_run_completeness(
            {"runId": "r", PROVIDER_POSTURE_RUN_FIELD: stamp},
            include_environment=False,
        )
        assert not _model_components(completeness.to_dict())

    def test_an_unknown_role_still_reports_rather_than_being_dropped(self):
        """Silence for a role this module has not been taught would be exactly the
        silent degradation HP-2 removes."""
        run = {
            "runId": "r",
            PROVIDER_POSTURE_RUN_FIELD: {
                "schemaVersion": PROVIDER_POSTURE_SCHEMA_VERSION,
                "status": UNAVAILABLE,
                "roles": {
                    "reranking": {
                        "provider": _GEN_PROVIDER,
                        "variable": "MODEL_RERANKING_PROVIDER",
                        "status": UNAVAILABLE,
                        "check": CHECK_REACHABILITY,
                        "probed": True,
                    }
                },
            },
        }
        components = _model_components(
            build_run_completeness(run, include_environment=False).to_dict()
        )
        assert [c["component"] for c in components] == ["reranking_provider"]
        assert components[0]["reason"] and components[0]["remedy"]

    def test_entries_are_ordered_deterministically(self):
        """A payload whose order varies makes diffing two runs useless."""
        run = {"runId": "r", PROVIDER_POSTURE_RUN_FIELD: _record_for(BOTH_DOWN)}
        first = build_run_completeness(run, include_environment=False).to_dict()
        second = build_run_completeness(run, include_environment=False).to_dict()
        assert first == second
        assert [c["component"] for c in _model_components(first)] == [
            EMB_COMPONENT,
            GEN_COMPONENT,
        ]


class TestTheInertEmbeddingModeEntryIsUnaffected:
    """The pre-existing 2.0-D4 entry is a DIFFERENT fact and stays.

    "This mode has no embeddings endpoint at all" (a configuration fact, silent by
    construction) is not "we could not reach the endpoint it does have". Both can
    be true at once, and both are reported: suppressing either would hide a real
    problem, and this module's whole thesis is report-never-suppress.
    """

    def test_the_configuration_entry_still_fires(self, client, monkeypatch):
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
        org = _org()
        with _with_posture(None):
            body = client.get(
                "/api/run-health/degradation", headers=_auth(org)
            ).json()
        inert = [
            c
            for c in body["completeness"]["components"]
            if c["component"] == EMB_COMPONENT
            and not (c.get("detail") or {}).get("postureSource")
        ]
        assert inert, "the inert-embedding-mode entry was lost"
        assert inert[0]["status"] == degradation.STATUS_UNAVAILABLE

    def test_both_facts_can_be_reported_and_stay_distinguishable(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
        org = _org()
        with _with_posture(EMB_DOWN):
            body = client.get(
                "/api/run-health/degradation", headers=_auth(org)
            ).json()
        embedding = [
            c
            for c in body["completeness"]["components"]
            if c["component"] == EMB_COMPONENT
        ]
        assert len(embedding) == 2, [c["reason"] for c in embedding]
        sources = {(c.get("detail") or {}).get("postureSource") for c in embedding}
        assert sources == {None, POSTURE_SOURCE_STARTUP}
        assert len({c["reason"] for c in embedding}) == 2


# ---------------------------------------------------------------------------
# The stamp reaches storage as declared
# ---------------------------------------------------------------------------


class TestTheStampSurvivesPersistence:
    def test_the_field_round_trips_through_the_run_record(self):
        run_id = _seed_run(BOTH_DOWN)
        stored = db.run_get(run_id) or {}
        stamp = stored.get(PROVIDER_POSTURE_RUN_FIELD)
        assert isinstance(stamp, dict)
        assert stamp["roles"][ROLE_EMBEDDING]["provider"] == _EMB_PROVIDER
        assert stamp["schemaVersion"] == PROVIDER_POSTURE_SCHEMA_VERSION

    def test_the_addition_is_additive_only(self):
        """Preserve API response shapes: the completeness payload gains entries,
        never a renamed or removed key."""
        run_id = _seed_run(GEN_DOWN)
        payload = build_run_completeness(
            db.run_get(run_id) or {}, include_environment=False
        ).to_dict()
        assert set(payload) >= {
            "schemaVersion",
            "runId",
            "status",
            "complete",
            "headline",
            "missing",
            "degradedCount",
            "components",
        }
        for component in payload["components"]:
            assert set(component) == {
                "kind",
                "component",
                "status",
                "nativeStatus",
                "attempted",
                "delivered",
                "missing",
                "reason",
                "remedy",
                "detail",
            }
