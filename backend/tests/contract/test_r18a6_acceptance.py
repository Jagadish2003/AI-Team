"""R18-A6 / AT-612 (T7) — acceptance contract tests for Section 3 (AC1–AC7).

The authoritative, per-Section-3 verification of the "Java & .NET code &
structure" story. Each AC is a labelled test (or small group):

  AC1 — deterministic Java extraction (components / versioned deps / endpoints /
        redacted config keys) with NO model call in the extraction path.
  AC2 — the same through the SHARED model for a .NET app.
  AC3 — structure enters the knowledge graph as OBSERVED entities/relationships
        carrying repo/path/SHA provenance.
  AC4 — a runtime (phase-one) entity resolves to its structural counterpart when
        the evidence supports it; ambiguous cases stay separate.
  AC5 — retrieval scoped to a component returns that component's code, not
        path-coincidental matches.
  AC6 — NO configuration VALUES appear in the graph or retrieval — only keys; a
        seeded secret is absent everywhere (this story's value redaction AND
        R18-A2's content redaction, both verified).
  AC7 — an end-to-end scenario produces a located-friction finding that JOINs a
        phase-one runtime signal with phase-two structure.

Deep per-module coverage already lives in the subtask suites
(``discovery/tests/test_enterprise_apps_structure*.py`` (T1/T2),
``test_enterprise_apps_runtime_resolution.py`` (T4),
``tests/contract/test_enterprise_apps_graph_ingest.py`` (T3),
``tests/contract/test_enterprise_apps_component_retrieval.py`` (T5)). This suite
is the cohesive Section-3 acceptance pass, and it OWNS the two integration ACs no
single subtask produced: AC6's "absent from the graph AND retrieval" and AC7's
end-to-end join.

Graph assertions use the shared contract Postgres harness (``sqlite3`` calls are
routed to Postgres by ``conftest``). Retrieval assertions need the pgvector
``retrieval_chunks`` store and are SKIPPED (never failed) where it is absent, and
drive a deterministic in-boundary fake embedding provider — the same pattern the
T5 suite uses.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import List
from unittest.mock import patch
from uuid import uuid4

import pytest

from app import db
from app.model_gateway import register_provider
from app.model_gateway._interface import GenerationRequest, GenerationResult, ModelProvider
from app.retrieval import embedder
from app.retrieval.ingest import ingest_content
from discovery.enterprise_apps import structure as structure_mod
from discovery.enterprise_apps.app_repo_map import AppRepoMapping
from discovery.enterprise_apps.component_retrieval import retrieve_component_code
from discovery.enterprise_apps.graph_ingest import GraphIngestResult, ingest_app_structure
from discovery.enterprise_apps.runtime_structure_resolution import (
    CONFIDENCE_NAME,
    CONFIDENCE_STABLE_ID,
    STATUS_AMBIGUOUS,
    STATUS_RESOLVED,
    STATUS_UNRESOLVED,
    resolve_runtime_entity,
    runtime_entity_from_operational,
    structural_entities_from_mappings,
)
from discovery.enterprise_apps.structure import RepoFile, extract_structure
from discovery.ingest.operational_signals import build_operational_signal
from discovery.ingest.secret_redaction import scan_and_redact


# ─────────────────────────────────────────────────────────────────────────────
# Graph harness helpers (routed to Postgres by conftest)
# ─────────────────────────────────────────────────────────────────────────────
def _db() -> str:
    return os.environ.get("DB_PATH", "")


def _entities(org_id: str, entity_type: str | None = None) -> list[dict]:
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        if entity_type:
            rows = conn.execute(
                "SELECT * FROM entities WHERE org_id=%s AND entity_type=%s",
                (org_id, entity_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM entities WHERE org_id=%s", (org_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def _relationships(org_id: str) -> list[dict]:
    with sqlite3.connect(_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM entity_relationships WHERE org_id=%s", (org_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def _metadata(row: dict) -> dict:
    raw = row.get("metadata")
    return json.loads(raw) if isinstance(raw, str) and raw else (raw or {})


def _org(tag: str) -> str:
    return f"org-{tag}-{uuid4().hex[:10]}"


def _mapping(app_id: str, name: str, platform: str, repo_ids: tuple[str, ...]) -> AppRepoMapping:
    return AppRepoMapping(
        app_id=app_id, name=name, platform=platform, repo_ids=repo_ids, metadata={}
    )


def _stub_provider(files_by_repo: dict[str, list[RepoFile]]):
    def provider(repo_id: str):
        return files_by_repo.get(repo_id, [])

    return provider


def _run_graph_ingest(mapping, provider, commit_sha_provider=None, *, org_id, run_id="run-001"):
    with patch(
        "discovery.enterprise_apps.graph_ingest.get_app_mapping", return_value=mapping
    ):
        return ingest_app_structure(
            org_id, run_id, mapping.app_id, provider, commit_sha_provider
        )


# ─────────────────────────────────────────────────────────────────────────────
# Fixture repos — a Java app and a .NET app, each with a SECRET in config
# ─────────────────────────────────────────────────────────────────────────────
JAVA_SECRET = "S3cr3t-Java-Value-9f3a"
DOTNET_SECRET = "S3cr3t-DotNet-Value-71bc"

JAVA_POM = """<?xml version="1.0"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>payments-api</artifactId>
  <version>1.0.0</version>
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-webmvc</artifactId>
      <version>6.1.3</version>
    </dependency>
  </dependencies>
</project>
"""

JAVA_CONTROLLER = """
package com.acme.payments;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/payments")
public class PaymentController {
    @GetMapping("/{id}")
    public String getPayment() { return null; }
}
"""

JAVA_APPLICATION_YML = f"""
server:
  port: 8443
spring:
  datasource:
    url: jdbc:postgresql://prod-db/payments
    password: {JAVA_SECRET}
"""


def _java_repo_files() -> list[RepoFile]:
    return [
        RepoFile("pom.xml", JAVA_POM),
        RepoFile("src/main/java/com/acme/payments/PaymentController.java", JAVA_CONTROLLER),
        RepoFile("src/main/resources/application.yml", JAVA_APPLICATION_YML),
    ]


DOTNET_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <ItemGroup>
    <PackageReference Include="Serilog.AspNetCore" Version="8.0.1" />
  </ItemGroup>
</Project>
"""

DOTNET_CONTROLLER = """
using Microsoft.AspNetCore.Mvc;

namespace Acme.Billing
{
    [ApiController]
    [Route("api/[controller]")]
    public class BillingController : ControllerBase
    {
        [HttpGet("{id}")]
        public IActionResult GetInvoice(int id) => Ok();
    }
}
"""

DOTNET_APPSETTINGS = f"""{{
  "ConnectionStrings": {{ "Default": "Server=prod;Password={DOTNET_SECRET}" }},
  "Logging": {{ "LogLevel": {{ "Default": "Information" }} }}
}}
"""


def _dotnet_repo_files() -> list[RepoFile]:
    return [
        RepoFile("BillingApi.csproj", DOTNET_CSPROJ),
        RepoFile("Controllers/BillingController.cs", DOTNET_CONTROLLER),
        RepoFile("appsettings.json", DOTNET_APPSETTINGS),
    ]


def _config_values_present(shape) -> set:
    """Every scalar VALUE in a config_shape (recursively) — should always be just
    the redaction placeholder, never a real value."""
    out: set = set()

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        else:
            out.add(node)

    walk(shape)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — deterministic Java extraction, no model call in the extraction path
# ═════════════════════════════════════════════════════════════════════════════
class TestAC1JavaExtraction:
    def test_ac1_components_versioned_deps_endpoints_redacted_config(self):
        s = extract_structure(_java_repo_files(), "java")

        # components (a Spring controller among them)
        assert any(c.kind == "controller" for c in s.components)
        # build-declared, VERSIONED dependency
        deps = {d.name: d for d in s.dependencies}
        assert deps["spring-webmvc"].version == "6.1.3"
        # declared REST endpoint
        assert ("GET", "/api/payments/{id}") in {(e.method, e.path) for e in s.endpoints}
        # config keys kept, values redacted
        assert "server" in s.config_shape and "spring" in s.config_shape
        assert _config_values_present(s.config_shape) <= {structure_mod.REDACTED}

    def test_ac1_extraction_is_deterministic(self):
        files = _java_repo_files()
        assert (
            extract_structure(files, "java").to_dict()
            == extract_structure(list(reversed(files)), "java").to_dict()
        )

    def test_ac1_no_model_call_in_extraction_path(self):
        # Tokens built by concatenation so this test file does not itself trip the
        # repo-wide no-bypass scanner (tests/contract/test_model_gateway_no_bypass.py).
        src = Path(structure_mod.__file__).read_text(encoding="utf-8")
        for token in ("model" + "_gateway", "llm" + "_enrichment", "anthro" + "pic", "open" + "ai"):
            assert token not in src


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — same for .NET through the shared structure model
# ═════════════════════════════════════════════════════════════════════════════
class TestAC2DotNetExtraction:
    def test_ac2_dotnet_via_shared_model(self):
        s = extract_structure(_dotnet_repo_files(), "dotnet")

        # Shared model — same dataclasses as Java.
        assert s.platform == "dotnet"
        assert any(c.name == "BillingController" for c in s.components)
        deps = {d.name: d for d in s.dependencies}
        assert "Serilog.AspNetCore" in deps
        assert deps["Serilog.AspNetCore"].version == "8.0.1"
        # declared endpoint (route-token convention resolved) + verb
        assert any(e.method == "GET" for e in s.endpoints)
        # appsettings shape — keys kept, values redacted
        assert s.config_shape
        assert _config_values_present(s.config_shape) <= {structure_mod.REDACTED}


# ═════════════════════════════════════════════════════════════════════════════
# AC3 — structure enters the graph as observed, with repo/path/SHA provenance
# ═════════════════════════════════════════════════════════════════════════════
class TestAC3StructureInGraph:
    def test_ac3_observed_entities_and_relationships_with_provenance(self):
        org_id = _org("ac3")
        mapping = _mapping("payments-api", "Payments API", "java", ("payments-repo",))
        provider = _stub_provider({"payments-repo": _java_repo_files()})

        result = _run_graph_ingest(
            mapping, provider, lambda repo_id: "abc1234", org_id=org_id
        )
        assert isinstance(result, GraphIngestResult)

        # The application is a 'system' entity; components/deps/endpoints 'object'.
        systems = _entities(org_id, "system")
        assert any(r["source_record_id"] == "payments-api" for r in systems)

        objects = _entities(org_id, "object")
        assert objects, "structure must enter the graph as observed object entities"
        for row in objects:
            md = _metadata(row)
            pointer = md["evidence_pointer"]
            assert pointer["origin"] == "observed"  # never inferred
            # repo/path/SHA provenance (AC3)
            assert "payments-repo" in pointer["source_artifact"]
            assert "abc1234" in pointer["source_artifact"]
            assert md["repo_id"] == "payments-repo"
            assert md["commit_sha"] == "abc1234"
            assert md["path"]

        rel_types = {r["relationship_type"] for r in _relationships(org_id)}
        assert {"owns", "routes_to"} <= rel_types  # app owns component, comp routes_to endpoint


# ═════════════════════════════════════════════════════════════════════════════
# AC4 — conservative runtime→structure resolution
# ═════════════════════════════════════════════════════════════════════════════
class TestAC4RuntimeResolution:
    def _structural(self):
        return structural_entities_from_mappings(
            [
                _mapping("payments-api", "Payments API", "java", ("payments-repo",)),
                _mapping("billing", "Billing", "java", ("billing-repo",)),
            ]
        )

    def test_ac4_confident_match_resolves(self):
        runtime = runtime_entity_from_operational(
            {"app_id": "payments-api", "service": "payments-api", "source_system": "java_app"}
        )
        outcome = resolve_runtime_entity(runtime, self._structural())
        assert outcome.status == STATUS_RESOLVED
        assert outcome.matched.app_id == "payments-api"
        assert outcome.confidence == CONFIDENCE_STABLE_ID

    def test_ac4_ambiguous_stays_separate(self):
        structural = structural_entities_from_mappings(
            [
                _mapping("app-a", "A", "java", ("a",)),
                _mapping("app-b", "B", "java", ("b",)),
            ]
        )
        # Both structural apps share the service name "shared".
        structural = [type(s)(**{**s.__dict__, "service": "shared"}) for s in structural]
        runtime = runtime_entity_from_operational(
            {"app_id": "runtime-x", "service": "shared", "source_system": "java_app"}
        )
        outcome = resolve_runtime_entity(runtime, structural)
        assert outcome.status == STATUS_AMBIGUOUS
        assert outcome.matched is None  # never force-merged

    def test_ac4_no_candidate_stays_unresolved(self):
        runtime = runtime_entity_from_operational(
            {"app_id": "ghost", "service": "ghost", "source_system": "java_app"}
        )
        outcome = resolve_runtime_entity(runtime, self._structural())
        assert outcome.status == STATUS_UNRESOLVED
        assert outcome.matched is None


# ═════════════════════════════════════════════════════════════════════════════
# AC6 (offline halves) — config value redaction + R18-A2 content redaction
# ═════════════════════════════════════════════════════════════════════════════
class TestAC6RedactionOffline:
    def test_ac6_config_values_redacted_in_extraction_java_and_dotnet(self):
        for files, platform, secret in (
            (_java_repo_files(), "java", JAVA_SECRET),
            (_dotnet_repo_files(), "dotnet", DOTNET_SECRET),
        ):
            s = extract_structure(files, platform)
            blob = json.dumps(s.to_dict())
            assert secret not in blob  # seeded config value never surfaces
            assert _config_values_present(s.config_shape) <= {structure_mod.REDACTED}

    def test_ac6_a2_content_redaction_verified(self):
        # R18-A2's content redaction (the "A2 redaction" half of AC6): a secret in
        # code content is redacted before it can ever be indexed.
        leaked = 'String apiKey = "AKIAIOSFODNN7EXAMPLE";'
        outcome = scan_and_redact(leaked)
        assert outcome.redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in outcome.text


# ═════════════════════════════════════════════════════════════════════════════
# AC6 (graph half) — no config value appears in the graph
# ═════════════════════════════════════════════════════════════════════════════
class TestAC6NoConfigValueInGraph:
    def test_ac6_seeded_config_secret_absent_from_all_graph_entities(self):
        org_id = _org("ac6")
        mapping = _mapping("payments-api", "Payments API", "java", ("payments-repo",))
        provider = _stub_provider({"payments-repo": _java_repo_files()})
        _run_graph_ingest(mapping, provider, lambda r: "abc1234", org_id=org_id)

        # The application.yml secret (and its other config VALUES) must appear in
        # NO graph entity — structure carries keys, never config values.
        blob = json.dumps([dict(r) for r in _entities(org_id)], default=str)
        assert JAVA_SECRET not in blob
        assert "jdbc:postgresql://prod-db/payments" not in blob


# ═════════════════════════════════════════════════════════════════════════════
# Retrieval-backed ACs (AC5, AC6-retrieval, AC7-with-code) — need pgvector.
# ═════════════════════════════════════════════════════════════════════════════
def _retrieval_store_available() -> bool:
    try:
        con = db.connect()
        try:
            cur = con.cursor()
            cur.execute("SELECT to_regclass('public.retrieval_chunks')")
            return cur.fetchone()[0] is not None
        finally:
            con.close()
    except Exception:
        return False


_RETRIEVAL = pytest.mark.skipif(
    not _retrieval_store_available(),
    reason="retrieval_chunks store (pgvector) not present in this environment",
)


class _AccFakeProvider(ModelProvider):
    """Deterministic embeddings: 1.0 on the marker token, else 0.0 — unique
    provider name so registration never collides with other suites."""

    emits_own_telemetry = True

    def __init__(self, name: str, identity):
        self.name = name
        self._identity = identity

    def generate(self, req: GenerationRequest) -> GenerationResult:  # pragma: no cover
        return GenerationResult(text=None, provider=self.name, ok=False)

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [[1.0 if "payments" in t.lower() else 0.0, 0.01] for t in texts]

    def embedding_identity(self):
        return self._identity


_ACC_PROVIDER = _AccFakeProvider("r18a6_acc_embed", ("r18a6-acc:model", "1"))
register_provider(_ACC_PROVIDER)


def _cleanup_retrieval(org_id: str) -> None:
    con = db.connect()
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM retrieval_chunks WHERE org_id = %s", (org_id,))
        con.commit()
    finally:
        con.close()


@pytest.fixture
def retrieval_org(request, monkeypatch):
    monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", _ACC_PROVIDER.name)
    name = f"r18a6acc_{request.node.name}"[:60]
    _cleanup_retrieval(name)
    yield name
    _cleanup_retrieval(name)


_CONTROLLER_ARTIFACT = "payments-repo:src/main/java/com/acme/payments/PaymentController.java"
_SERVICE_ARTIFACT = "payments-repo:src/main/java/com/acme/payments/PaymentService.java"


@_RETRIEVAL
class TestAC5ComponentScopedRetrieval:
    def _ingest(self, org_id: str) -> None:
        ingest_content(
            org_id,
            [
                dict(
                    source_system="git",
                    source_artifact=_CONTROLLER_ARTIFACT,
                    content="payments controller handles the payment request",
                    content_type="code",
                ),
                dict(
                    source_system="git",
                    source_artifact=_SERVICE_ARTIFACT,
                    content="payments service business logic decoy",
                    content_type="code",
                ),
            ],
        )
        embedder.embed_pending_for_org(org_id)

    def test_ac5_scoped_to_component_excludes_path_coincidental(self, retrieval_org):
        self._ingest(retrieval_org)
        mapping = _mapping("payments-api", "Payments API", "java", ("payments-repo",))
        provider = _stub_provider({"payments-repo": _java_repo_files()})
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            hits = retrieve_component_code(
                retrieval_org, "payments-api", "PaymentController", provider,
                query_text="payments", k=10,
            )
        assert hits
        # Only the controller's own file — the same-signal service file is excluded.
        assert {h.source_artifact for h in hits} == {_CONTROLLER_ARTIFACT}


@_RETRIEVAL
class TestAC6NoSecretInRetrieval:
    def test_ac6_a2_redacted_secret_absent_from_retrieval(self, retrieval_org):
        # Route content through R18-A2's redaction BEFORE indexing (what the git
        # content ingestor does), then confirm the secret is not retrievable.
        raw = 'payments config token = "AKIAIOSFODNN7EXAMPLE" for the service'
        redacted = scan_and_redact(raw).text
        ingest_content(
            retrieval_org,
            [dict(source_system="git", source_artifact=_CONTROLLER_ARTIFACT,
                  content=redacted, content_type="code")],
        )
        embedder.embed_pending_for_org(retrieval_org)

        from app.retrieval.api import retrieve

        hits = retrieve(retrieval_org, "payments", k=10, source_filter=["git"])
        assert hits  # content is indexed…
        assert all("AKIAIOSFODNN7EXAMPLE" not in h.content for h in hits)  # …but redacted


# ═════════════════════════════════════════════════════════════════════════════
# AC7 — end-to-end: phase-one runtime signal JOINs phase-two structure
# ═════════════════════════════════════════════════════════════════════════════
def _payments_friction_records():
    """Phase-one operational records for 'payments-api' showing 'error rate rising'."""
    return [
        {
            "service": "payments-api", "app_id": "payments-api",
            "source_system": "java_app", "artifact_kind": "metrics",
            "observed_ts": "2026-07-14T10:00:00Z", "health": "UP",
            "error_rate": 0.12, "latency_p95_ms": 300.0, "throughput_rpm": 100.0,
            "memory_used_ratio": 0.5, "cpu_usage": 0.5,
        },
        {
            "service": "payments-api", "app_id": "payments-api",
            "source_system": "java_app", "artifact_kind": "log",
            "observed_ts": "2026-07-14T10:00:05Z", "level": "ERROR",
            "exception_type": "PaymentProcessingException", "retry": False,
        },
    ]


class TestAC7EndToEndJoin:
    def test_ac7_runtime_signal_joins_structure_in_graph(self):
        """The core located-friction join (graph + resolution): a phase-one
        runtime friction signal for a service resolves to the phase-two structural
        application that was loaded into the graph."""
        org_id = _org("ac7")
        mapping = _mapping("payments-api", "Payments API", "java", ("payments-repo",))
        provider = _stub_provider({"payments-repo": _java_repo_files()})

        # Phase two: structure into the graph (T3).
        _run_graph_ingest(mapping, provider, lambda r: "abc1234", org_id=org_id)
        app_systems = _entities(org_id, "system")
        assert any(r["source_record_id"] == "payments-api" for r in app_systems)

        # Phase one: a runtime friction signal (T-R17-A3), "error rate rising".
        signal = build_operational_signal(_payments_friction_records())
        friction = signal["operational_friction"]
        assert friction["fired"] is True
        assert "payments-api" in friction["services"]
        assert "elevated error rate" in friction["reasons"]

        # The JOIN (T4): the service emitting errors IS this structural application.
        runtime = runtime_entity_from_operational(
            {"app_id": "payments-api", "service": "payments-api", "source_system": "java_app"}
        )
        outcome = resolve_runtime_entity(
            runtime, structural_entities_from_mappings([mapping])
        )
        assert outcome.is_resolved
        assert outcome.matched.app_id == "payments-api"

        # The located-friction finding joins both phases.
        finding = {
            "runtime_signal": {
                "service": "payments-api",
                "reasons": friction["reasons"],
            },
            "structural_app_id": outcome.matched.app_id,
            "resolution_confidence": outcome.confidence,
        }
        assert finding["runtime_signal"]["reasons"]  # phase one present
        assert finding["structural_app_id"] == "payments-api"  # phase two present

    @_RETRIEVAL
    def test_ac7_located_friction_opportunity_has_retrievable_code(self, retrieval_org):
        """The full located-friction opportunity: runtime friction + resolved
        structural component + the component's RETRIEVABLE code as evidence."""
        org_id = retrieval_org
        mapping = _mapping("payments-api", "Payments API", "java", ("payments-repo",))
        provider = _stub_provider({"payments-repo": _java_repo_files()})

        # Phase two into the graph.
        _run_graph_ingest(mapping, provider, lambda r: "abc1234", org_id=org_id)

        # A2-ingested component code (retrievable evidence).
        ingest_content(
            org_id,
            [dict(source_system="git", source_artifact=_CONTROLLER_ARTIFACT,
                  content="payments controller processes the payment request",
                  content_type="code")],
        )
        embedder.embed_pending_for_org(org_id)

        # Phase one friction → resolve to the structural app (T4).
        friction = build_operational_signal(_payments_friction_records())["operational_friction"]
        runtime = runtime_entity_from_operational(
            {"app_id": "payments-api", "service": "payments-api", "source_system": "java_app"}
        )
        outcome = resolve_runtime_entity(
            runtime, structural_entities_from_mappings([mapping])
        )
        assert outcome.is_resolved

        # Component-scoped retrieval (T5) yields the code evidence.
        with patch(
            "discovery.enterprise_apps.component_retrieval.get_app_mapping",
            return_value=mapping,
        ):
            code = retrieve_component_code(
                org_id, "payments-api", "PaymentController", provider,
                query_text="payments", k=10,
            )
        assert code, "the located component's code must be retrievable as evidence"

        # AC7: one finding joins the phase-one signal, the resolved structure, and
        # the retrievable code — the opportunity no single phase could produce.
        opportunity = {
            "friction_reasons": friction["reasons"],
            "located_app": outcome.matched.app_id,
            "code_evidence": [h.source_artifact for h in code],
        }
        assert "elevated error rate" in opportunity["friction_reasons"]
        assert opportunity["located_app"] == "payments-api"
        assert _CONTROLLER_ARTIFACT in opportunity["code_evidence"]
