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
from pathlib import Path
from typing import Any

import pytest

from app import connector_roadmap
from discovery.packs.industry_registry import INDUSTRY_REGISTRY
from discovery.packs.pack_config import PACK_REGISTRY

AUTH = {"Authorization": "Bearer dev-token-change-me"}
BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
CATALOG_SEED = BACKEND_ROOT / "database" / "seed" / "connectors.json"

EXPECTED_INDUSTRIES = {
    "financial_services",
    "public_sector",
    "logistics_supply_chain",
    "retail_commerce",
    "healthcare",
    "energy_utilities",
    "manufacturing",
    "technology",
}

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
    "backend/discovery/packs/template_registry.py",
}
FORBIDDEN_ENGINE_OR_TEMPLATE_MODEL_PREFIXES = (
    "backend/discovery/detectors/",
    "backend/discovery/enrichment/",
)

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


def _story_changed_paths() -> set[str]:
    # If dev has already been merged into this feature branch, ignore dev's own
    # changes for the R191-R1 config-only guard and inspect only this branch's
    # story delta on top of dev. Otherwise fall back to the R1 target branch.
    if _is_ancestor("origin/dev"):
        return _changed_paths_against_ref("origin/dev")

    candidates = [
        os.environ.get("R191_R1_BASE_REF"),
        "origin/R-1.9.1-R1",
        "R-1.9.1-R1",
    ]
    for ref in [c for c in candidates if c]:
        try:
            _git(["rev-parse", "--verify", ref])
            return _changed_paths_against_ref(ref)
        except RuntimeError:
            continue
    pytest.skip("No R191-R1 base ref available for diff guard")


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
    focus_page = (REPO_ROOT / "frontend" / "src" / "pages" / "DiscoveryFocusPage.tsx").read_text(
        encoding="utf-8"
    )
    assert "roadmap_systems" in focus_page


def test_registry_story_diff_avoids_engine_and_template_model_code():
    changed = _story_changed_paths()
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
