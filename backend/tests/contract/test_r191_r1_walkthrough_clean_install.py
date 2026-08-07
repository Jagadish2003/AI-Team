"""R191-R1 T7 - walkthrough and clean-install contracts for AC1-AC6.

This file is the end-to-end acceptance layer for Registry & Catalog Calibration:
every industry can walk through Stack Builder from registry fetch to launch, the
roadmap state shown by Stack Builder agrees with the Integration Hub catalog on
a clean seed, and the story stays out of discovery engine / template-model code.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psycopg2
import pytest
from psycopg2 import sql as pg_sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT, parse_dsn

from app import connector_roadmap
from discovery.packs.industry_registry import INDUSTRY_REGISTRY
from discovery.packs.pack_config import PACK_REGISTRY

AUTH = {"Authorization": "Bearer dev-token-change-me"}
BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
CATALOG_SEED = BACKEND_ROOT / "database" / "seed" / "connectors.json"
SEED_LOADER = BACKEND_ROOT / "database" / "seed_loader.py"

EXPECTED_INDUSTRIES = {
    "financial_services",
    "public_sector",
    "logistics_supply_chain",
    "retail_commerce",
    "healthcare",
    "energy_utilities",
    "manufacturing",
    "technology",
    # 2.0-D2 T4 added the ninth industry, Insurance, anchored on shipped
    # connectors only. Including it here extends the per-industry Stack Builder
    # walkthrough to Insurance as well.
    "insurance",
}

# 2.0-D2 T4: Insurance is the one industry deliberately WITHOUT a database
# anchor — it exists to mirror the Insurance TEMPLATE's shape exactly, and that
# template names no database source. The AC4 "database in every industry
# profile" invariant (R191-R1 T3) is therefore scoped to the eight industries it
# was written for; Insurance is asserted present-but-DB-free instead of silently
# widening the rule.
DATABASE_ANCHOR_EXEMPT_INDUSTRIES = {"insurance"}

VALID_ROLES = {
    "system_of_record",
    "workflow_system",
    "operational_signal_source",
    "documentation_system",
    "engineering_change_system",
}
VALID_PRIORITIES = {"primary", "secondary", "optional"}
VALID_WORKFLOW_TAGS = {
    "intake_requests",
    "service_casework",
    "approvals",
    "backlog_work_queues",
    "compliance_risk",
    "documents_knowledge",
    "handoffs_routing",
    "communications",
    "change_release",
    "data_analytics",
}

FORBIDDEN_ENGINE_OR_TEMPLATE_MODEL_PATHS = {
    "backend/app/routes_stack_builder_launch.py",
    "backend/discovery/runner.py",
}
FORBIDDEN_ENGINE_OR_TEMPLATE_MODEL_PREFIXES = (
    "backend/discovery/detectors/",
    "backend/discovery/enrichment/",
)
# template_registry.py is deliberately NOT in the forbidden set: it is BOTH the
# template model AND the registry, so 2.0-D1 (FSC) and 2.0-D2 (Insurance) add
# template CONFIG entries to it — a legitimate config-only change. The guard
# below still fails if a template-MODEL or public-API line changes, via
# _template_registry_model_or_api_lines_changed(); a dict entry passes.
TEMPLATE_REGISTRY_REL = "backend/discovery/packs/template_registry.py"

FRONTEND_REGISTRY_SOURCE_FILES = (
    REPO_ROOT / "frontend" / "src" / "api" / "stackBuilderApi.ts",
    REPO_ROOT / "frontend" / "src" / "pages" / "DiscoveryFocusPage.tsx",
    REPO_ROOT / "frontend" / "src" / "pages" / "StackBuilderPage.tsx",
    REPO_ROOT / "frontend" / "src" / "components" / "stack_builder" / "useSetupState.ts",
)
FORBIDDEN_FRONTEND_CACHE_PATTERNS = {
    "hardcoded industries": re.compile(r"\b(?:const|let|var)\s+INDUSTRIES\s*=\s*\["),
    "hardcoded templates": re.compile(r"\b(?:const|let|var)\s+TEMPLATES\s*=\s*\["),
    "hardcoded default assumptions": re.compile(
        r"\bSYSTEM_DEFAULT_ASSUMPTIONS\s*[:=]\s*[{[]"
    ),
}

CLEAN_INSTALL_API_ASSERTIONS = r"""
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from app import connector_roadmap
from app.main import app
from app.rbac import seed_owner

token = os.environ.get("DEV_JWT", "dev-token-change-me")
headers = {"Authorization": f"Bearer {token}", "X-Org-Id": "default"}

with TestClient(app) as client:
    seed_owner("default", token)

    connectors_resp = client.get("/api/connectors", headers=headers)
    assert connectors_resp.status_code == 200, connectors_resp.text
    industries_resp = client.get("/api/stack-builder/industries", headers=headers)
    assert industries_resp.status_code == 200, industries_resp.text

    connectors = {row["id"]: row for row in connectors_resp.json()}
    seed_ids = {
        row["id"]
        for row in json.loads(
            (Path("database") / "seed" / "connectors.json").read_text(encoding="utf-8")
        )
    }
    assert seed_ids <= set(connectors), sorted(seed_ids - set(connectors))

    for connector_id in seed_ids:
        tile = connectors[connector_id]
        expected_roadmap = connector_roadmap.is_roadmap(connector_id)
        assert tile["roadmap"] is expected_roadmap, connector_id
        assert tile["roadmapTarget"] == (
            connector_roadmap.roadmap_target(connector_id)
            if expected_roadmap
            else None
        ), connector_id

    assert connectors["sap"]["roadmap"] is True
    assert connectors["sap"]["roadmapTarget"] == "2.0.1"
    assert connectors["dynamics365"]["roadmap"] is True
    assert connectors["dynamics365"]["roadmapTarget"] == "2.0.1"
    assert connectors["salesforce"]["roadmap"] is False

    industries = {row["industry_id"]: row for row in industries_resp.json()}
    for industry_id in ("manufacturing", "logistics_supply_chain"):
        roadmap = {
            row["system_id"]: row
            for row in industries[industry_id]["roadmap_systems"]
        }
        assert roadmap["sap"]["target_release"] == "2.0.1"
        assert roadmap["dynamics365"]["target_release"] == "2.0.1"

    technology = industries["technology"]
    assert "github_engineering" in technology["pack_hints"]
    assert "cloud_ops" in technology["pack_hints"]
    assert "security_ops" in technology["pack_hints"]
    tech_roadmap = {
        row["system_id"]: row for row in technology["roadmap_systems"]
    }
    assert tech_roadmap["gitlab"]["target_release"] == "unscheduled"
"""


def _dsn_parts(url: str) -> dict[str, str]:
    return parse_dsn(url)


def _db_name_of(url: str) -> str:
    return _dsn_parts(url).get("dbname", "")


def _with_db_name(url: str, new_db: str) -> str:
    parts = _dsn_parts(url)
    user = parts.get("user", "")
    password = parts.get("password", "")
    host = parts.get("host", "localhost")
    port = parts.get("port", "5432")
    auth = ""
    if user:
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return f"postgresql://{auth}{host}:{port}/{new_db}"


def _maintenance_connection(template_url: str):
    last_exc: Exception | None = None
    for maint_db in ("postgres", "template1"):
        try:
            con = psycopg2.connect(_with_db_name(template_url, maint_db))
            con.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            return con
        except psycopg2.Error as exc:
            last_exc = exc
    raise RuntimeError(f"could not connect to maintenance database: {last_exc}")


def _create_database(template_url: str, database_name: str) -> None:
    con = _maintenance_connection(template_url)
    try:
        with con.cursor() as cur:
            cur.execute(
                pg_sql.SQL("CREATE DATABASE {}").format(
                    pg_sql.Identifier(database_name)
                )
            )
    finally:
        con.close()


def _drop_database(template_url: str, database_name: str) -> None:
    con = _maintenance_connection(template_url)
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (database_name,),
            )
            cur.execute(
                pg_sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    pg_sql.Identifier(database_name)
                )
            )
    finally:
        con.close()


def _seed_catalog_ids() -> set[str]:
    return {row["id"] for row in json.loads(CATALOG_SEED.read_text(encoding="utf-8"))}


def _as_roadmap_api_rows(config) -> list[dict[str, str]]:
    return [
        {
            "system_id": item.system_id,
            "label": item.label,
            "target_release": item.target_release,
            "reason": item.reason,
        }
        for item in config.roadmap_systems
    ]


def _default_weightings(config) -> dict[str, dict[str, Any]]:
    return {
        system_id: {
            "systemId": system_id,
            "role": defaults.role,
            "priority": defaults.priority,
            "workflowFocus": defaults.workflow_focus,
            "confirmed": True,
        }
        for system_id, defaults in config.system_defaults.items()
    }


def _first_focus(config) -> str:
    for defaults in config.system_defaults.values():
        for focus in defaults.workflow_focus:
            return focus
    return "core_operations"


def _catalog_by_id(client) -> dict[str, dict[str, Any]]:
    resp = client.get("/api/connectors", headers=AUTH)
    assert resp.status_code == 200, resp.text
    return {row["id"]: row for row in resp.json()}


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def _changed_paths_against_ref(ref: str) -> set[str]:
    output = _git(["diff", "--name-only", f"{ref}...HEAD"])
    return {line.replace("\\", "/") for line in output.splitlines() if line}


def _is_ancestor(ref: str, descendant: str = "HEAD") -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ref, descendant],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0


def _story_base_ref() -> str:
    # If dev has already been merged into this feature branch, ignore dev's own
    # changes for the R191-R1 config-only guard and inspect only this branch's
    # story delta on top of dev. Otherwise fall back to the R1 target branch.
    if _is_ancestor("origin/dev"):
        return "origin/dev"

    candidates = [
        os.environ.get("R191_R1_BASE_REF"),
        "origin/R-1.9.1-R1",
        "R-1.9.1-R1",
    ]
    for ref in [c for c in candidates if c]:
        try:
            _git(["rev-parse", "--verify", ref])
            return ref
        except RuntimeError:
            continue
    pytest.skip("No R191-R1 base ref available for diff guard")


def _story_changed_paths() -> set[str]:
    return _changed_paths_against_ref(_story_base_ref())


def _template_registry_model_or_api_lines_changed(ref: str) -> list[str]:
    """Added/removed lines in template_registry.py that touch the template MODEL
    or public API (not a config dict entry). 2.0-D1/D2 legitimately add template
    ENTRIES to this file, so its mere appearance in the diff is not a violation —
    only a model/API-definition change is. Same forbidden-token discipline as
    test_insurance_template.py::test_no_template_model_or_api_line_changed."""
    forbidden_tokens = (
        "class TemplateDefinition", "class FocusDefaults",
        "def register_template", "def unregister_template", "def get_template",
        "def list_templates", "def resolve_launch_config",
        "def template_defaults_snapshot", "def normalize_template_ids",
    )
    diff = _git(["diff", "-U0", f"{ref}...HEAD", "--", TEMPLATE_REGISTRY_REL])
    offenders: list[str] = []
    for line in diff.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        if any(token in line for token in forbidden_tokens):
            offenders.append(line.strip())
    return offenders


@pytest.mark.parametrize("industry_id", sorted(EXPECTED_INDUSTRIES))
def test_each_industry_has_coherent_stack_builder_walkthrough(client, industry_id):
    config = INDUSTRY_REGISTRY[industry_id]

    industries_resp = client.get("/api/stack-builder/industries", headers=AUTH)
    assert industries_resp.status_code == 200, industries_resp.text
    industry_rows = {row["industry_id"]: row for row in industries_resp.json()}
    assert industry_id in industry_rows
    assert industry_rows[industry_id]["pack_hints"] == config.pack_hints
    assert industry_rows[industry_id]["recommended_systems"] == config.recommended_systems
    assert industry_rows[industry_id]["roadmap_systems"] == _as_roadmap_api_rows(config)

    defaults_resp = client.get(
        f"/api/stack-builder/industries/{industry_id}/system-defaults",
        headers=AUTH,
    )
    assert defaults_resp.status_code == 200, defaults_resp.text
    defaults_rows = {row["system_id"]: row for row in defaults_resp.json()}
    assert set(defaults_rows) == set(config.system_defaults)

    roadmap_ids = {item.system_id for item in config.roadmap_systems}
    selected_system_ids = list(config.system_defaults)
    assert selected_system_ids, f"{industry_id}: no selectable systems"
    assert not (roadmap_ids & set(selected_system_ids)), (
        f"{industry_id}: roadmap systems are selectable defaults"
    )

    primary_ids = [
        system_id
        for system_id, defaults in config.system_defaults.items()
        if defaults.priority == "primary"
    ]
    assert primary_ids, f"{industry_id}: no primary system"
    assert any(
        config.system_defaults[system_id].role in {"system_of_record", "workflow_system"}
        for system_id in primary_ids
    ), f"{industry_id}: primary system is not an honest business/workflow anchor"

    for system_id, row in defaults_rows.items():
        defaults = config.system_defaults[system_id]
        assert row == {
            "system_id": system_id,
            "role": defaults.role,
            "priority": defaults.priority,
            "workflow_focus": defaults.workflow_focus,
        }
        assert defaults.role in VALID_ROLES
        assert defaults.priority in VALID_PRIORITIES
        assert 0 < len(defaults.workflow_focus) <= 3
        assert set(defaults.workflow_focus) <= VALID_WORKFLOW_TAGS

    for pack_id in config.pack_hints:
        assert pack_id in PACK_REGISTRY, (industry_id, pack_id)

    payload = {
        "org_id": "default",
        "focus_id": _first_focus(config),
        "industry_id": industry_id,
        "template_id": None,
        "selected_system_ids": selected_system_ids,
        "pack_id": config.pack_hints[0],
        "weightings": _default_weightings(config),
    }
    launch_resp = client.post(
        "/api/stack-builder/launch",
        headers=AUTH,
        json=payload,
    )
    assert launch_resp.status_code == 200, launch_resp.text
    launch = launch_resp.json()
    assert launch["industryId"] == industry_id
    assert launch["packId"] == config.pack_hints[0]
    assert launch["systemCount"] == len(selected_system_ids)

    run_resp = client.get(f"/api/runs/{launch['runId']}", headers=AUTH)
    assert run_resp.status_code == 200, run_resp.text
    run = run_resp.json()
    assert run["source"] == "stack_builder"
    assert run["industryId"] == industry_id
    assert run["selectedSystemIds"] == selected_system_ids
    assert run["weightings"] == payload["weightings"]


def test_ac4_registry_calibration_invariants_hold():
    assert set(INDUSTRY_REGISTRY) == EXPECTED_INDUSTRIES

    for industry_id, config in INDUSTRY_REGISTRY.items():
        if industry_id not in DATABASE_ANCHOR_EXEMPT_INDUSTRIES:
            assert "sqlserver" in config.system_defaults, (
                f"{industry_id}: missing database default"
            )
        if "slack" in config.system_defaults:
            assert "teams" in config.system_defaults, (
                f"{industry_id}: Slack default without Teams parity"
            )
            slack = config.system_defaults["slack"]
            teams = config.system_defaults["teams"]
            assert teams.role == slack.role
            assert teams.priority == slack.priority
            assert teams.workflow_focus == slack.workflow_focus

    technology = INDUSTRY_REGISTRY["technology"]
    github = technology.system_defaults["github"]
    assert github.role == "engineering_change_system"
    assert github.priority == "optional"
    assert "sqlserver_opsignal" in technology.pack_hints
    assert "github_engineering" in technology.pack_hints
    assert "cloud_ops" in technology.pack_hints
    assert "security_ops" in technology.pack_hints
    assert all(pack_id in PACK_REGISTRY for pack_id in technology.pack_hints)


def test_stack_builder_roadmap_matches_hub_catalog_on_clean_seed(client):
    industries_resp = client.get("/api/stack-builder/industries", headers=AUTH)
    assert industries_resp.status_code == 200, industries_resp.text
    industries = {row["industry_id"]: row for row in industries_resp.json()}
    catalog = _catalog_by_id(client)

    assert {row["system_id"] for row in industries["manufacturing"]["roadmap_systems"]} == {
        "sap",
        "dynamics365",
    }
    assert {row["system_id"] for row in industries["logistics_supply_chain"]["roadmap_systems"]} == {
        "sap",
        "dynamics365",
    }
    assert {row["system_id"] for row in industries["energy_utilities"]["roadmap_systems"]} == {
        "sap",
    }

    for industry_id, row in industries.items():
        registry_row = INDUSTRY_REGISTRY[industry_id]
        assert row["roadmap_systems"] == _as_roadmap_api_rows(registry_row)
        for roadmap in row["roadmap_systems"]:
            connector_id = roadmap["system_id"]
            if connector_id not in catalog:
                continue
            tile = catalog[connector_id]
            assert tile["roadmap"] is True, (industry_id, connector_id)
            assert tile["roadmapTarget"] == roadmap["target_release"]

    for connector_id in ("sap", "dynamics365"):
        assert catalog[connector_id]["roadmap"] is True
        assert catalog[connector_id]["roadmapTarget"] == "2.0.1"


def test_clean_install_catalog_seed_receives_runtime_roadmap_overlay(client):
    catalog = _catalog_by_id(client)
    seed_ids = _seed_catalog_ids()

    missing_seed_tiles = sorted(seed_ids - set(catalog))
    assert missing_seed_tiles == [], (
        "Fresh install must expose every connectors.json catalog tile: "
        f"{missing_seed_tiles}"
    )

    for connector_id in seed_ids:
        tile = catalog[connector_id]
        expected_roadmap = connector_roadmap.is_roadmap(connector_id)
        assert tile["roadmap"] is expected_roadmap, connector_id
        assert tile["roadmapTarget"] == (
            connector_roadmap.roadmap_target(connector_id)
            if expected_roadmap
            else None
        )


def test_seed_loader_blank_database_serves_catalog_and_stack_builder_roadmap_flags():
    base_url = os.environ["DATABASE_URL"]
    base_db_name = _db_name_of(base_url)
    temp_db_name = f"{base_db_name}_r191_seed_{uuid.uuid4().hex[:8]}"
    temp_url = _with_db_name(base_url, temp_db_name)

    _create_database(base_url, temp_db_name)
    try:
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": temp_url,
                "TEST_DATABASE_URL": temp_url,
                "DEV_JWT": "dev-token-change-me",
                "INGEST_MODE": "offline",
                "ANTHROPIC_API_KEY": "",
                "AGENTIQ_DISABLE_BACKGROUND_JOBS": "1",
                "EMAIL_PROVIDER": "noop",
                "NETWORK_PROFILE": "standard",
                "OAUTH_CALLBACK_ALLOW_UNAUTH": "",
                "SEED_DIR": str(BACKEND_ROOT / "database" / "seed"),
            }
        )
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(BACKEND_ROOT),
                str(REPO_ROOT),
                env.get("PYTHONPATH", ""),
            ]
        )

        seed_result = subprocess.run(
            [sys.executable, str(SEED_LOADER)],
            cwd=BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert seed_result.returncode == 0, seed_result.stderr or seed_result.stdout

        api_result = subprocess.run(
            [sys.executable, "-c", CLEAN_INSTALL_API_ASSERTIONS],
            cwd=BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert api_result.returncode == 0, api_result.stderr or api_result.stdout
    finally:
        _drop_database(base_url, temp_db_name)


def test_frontend_stack_builder_has_no_cached_registry_arrays():
    for path in FRONTEND_REGISTRY_SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        for label, pattern in FORBIDDEN_FRONTEND_CACHE_PATTERNS.items():
            assert pattern.search(text) is None, f"{path}: {label}"

    stack_builder_api = (REPO_ROOT / "frontend" / "src" / "api" / "stackBuilderApi.ts").read_text(
        encoding="utf-8"
    )
    assert "/api/stack-builder/industries" in stack_builder_api
    assert "/api/stack-builder/templates" in stack_builder_api

    # The roadmap-systems surface used to be asserted against DiscoveryFocusPage,
    # which rendered a "Coming soon" pill per API-supplied roadmap system. That UI
    # was deliberately removed on dev (PR #560, "Restore disabled connect posture
    # for unavailable catalog items") in favour of a disabled posture on the
    # connector tiles, so the old assertion pinned a component that no longer
    # exists.
    #
    # It is re-pointed rather than deleted, because what R191-R1 needs to hold is
    # that the roadmap surface stays part of the FRONTEND CONTRACT: the backend
    # serves `roadmap_systems` on every industry row, and if the type is dropped
    # the field is orphaned and the anchor-on-shipped labelling has no consumer at
    # all. This is a weaker guard than the render assertion it replaces, and that
    # is worth knowing — re-strengthen it against whichever component renders the
    # roadmap labelling when one exists again.
    stack_builder_types = (
        REPO_ROOT / "frontend" / "src" / "types" / "stack_builder.ts"
    ).read_text(encoding="utf-8")
    assert "roadmap_systems" in stack_builder_types


def test_registry_story_diff_avoids_engine_and_template_model_code():
    base_ref = _story_base_ref()
    changed = _changed_paths_against_ref(base_ref)
    forbidden = sorted(
        path
        for path in changed
        if path in FORBIDDEN_ENGINE_OR_TEMPLATE_MODEL_PATHS
        or path.startswith(FORBIDDEN_ENGINE_OR_TEMPLATE_MODEL_PREFIXES)
    )
    assert forbidden == [], (
        "R191-R1 registry/catalog calibration must not change discovery engine "
        f"or template-model code. Forbidden changed paths: {forbidden}"
    )

    # template_registry.py may change (2.0-D1/D2 add template CONFIG entries),
    # but ONLY as config — a template-model or public-API line must not move.
    if TEMPLATE_REGISTRY_REL in changed:
        model_changes = _template_registry_model_or_api_lines_changed(base_ref)
        assert model_changes == [], (
            "template_registry.py changed the template MODEL/API, not just a "
            f"config entry: {model_changes}"
        )
