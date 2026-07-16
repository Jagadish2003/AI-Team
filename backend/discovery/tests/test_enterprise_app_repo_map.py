"""R18-A6 / AT-611 (T6) — configured app→repo mapping per org.

The mapping has no dedicated AC of its own; it DEFINES the configured app→repo
scope that AC1/AC2 (extraction), AC5 (component-scoped retrieval) and AC7 (the
end-to-end join) operate over. These tests pin the behaviours those criteria
depend on:

  * configured, never auto-discovered — offline fixture / ``ENTERPRISE_APP_REPOS``
    env only; a missing source yields no apps (no network, no guessing);
  * per org — an org-keyed config selects the calling org's apps, with a
    ``default`` fallback; a plain array applies to every org;
  * the mapping drives extraction — :func:`extract_app_structure` parses the
    UNION of an application's configured repos through the T1 parser;
  * discipline consistent with phase one — no secret in config (rejected), a
    duplicate app kept-first, and a repo belongs to exactly one application.

Offline/pure: no DB, no live credentials. ``is_live`` is monkeypatched per test so
behaviour never depends on the ambient ``INGEST_MODE``.
"""
from __future__ import annotations

import json

import pytest

from discovery.enterprise_apps import app_repo_map as arm
from discovery.enterprise_apps.app_repo_map import (
    AppRepoMapping,
    EnterpriseAppConfigError,
    app_for_repo,
    extract_app_structure,
    get_app_mapping,
    load_app_repo_mappings,
    repo_content_for_app,
    repo_ids_for_app,
)


@pytest.fixture
def offline(monkeypatch):
    """Force offline mode so loading reads the deterministic fixture."""
    monkeypatch.setattr(arm, "is_live", lambda: False)


@pytest.fixture
def live(monkeypatch):
    """Force live mode; the test supplies ENTERPRISE_APP_REPOS."""
    monkeypatch.setattr(arm, "is_live", lambda: True)
    monkeypatch.delenv(arm._CONFIG_ENV, raising=False)
    return monkeypatch


# ─────────────────────────────────────────────────────────────────────────────
# Offline fixture — configured apps per org
# ─────────────────────────────────────────────────────────────────────────────
def test_offline_fixture_loads_configured_apps(offline):
    mappings = load_app_repo_mappings("default")
    by_id = {m.app_id: m for m in mappings}

    cov = by_id["covenant-service"]
    assert cov.platform == "java"
    assert cov.repo_ids == ("covenant-web", "covenant-core")
    assert cov.service == "covenant-service"

    billing = by_id["billing-api"]
    assert billing.platform == "dotnet"
    assert billing.repo_ids == ("billing-api",)


def test_lookups_forward_and_reverse(offline):
    assert get_app_mapping("default", "covenant-service").platform == "java"
    assert repo_ids_for_app("default", "covenant-service") == (
        "covenant-web",
        "covenant-core",
    )
    # Reverse: which application a repo belongs to.
    assert app_for_repo("default", "covenant-core").app_id == "covenant-service"
    assert app_for_repo("default", "billing-api").app_id == "billing-api"
    # Unknowns resolve to None, never a guess.
    assert get_app_mapping("default", "ghost") is None
    assert app_for_repo("default", "unmapped-repo") is None
    assert repo_ids_for_app("default", "ghost") == ()


def test_unknown_org_falls_back_to_default(offline):
    # An org with no explicit entry inherits the 'default' declaration.
    assert get_app_mapping("acme-corp", "covenant-service").platform == "java"


# ─────────────────────────────────────────────────────────────────────────────
# Per-org selection + configured-not-discovered
# ─────────────────────────────────────────────────────────────────────────────
def test_org_keyed_config_selects_calling_org(live):
    config = {
        "org-a": [
            {"app_id": "app-a", "platform": "java", "repos": ["a-repo"]},
        ],
        "org-b": [
            {"app_id": "app-b", "platform": "dotnet", "repos": ["b-repo"]},
        ],
    }
    live.setenv(arm._CONFIG_ENV, json.dumps(config))
    assert [m.app_id for m in load_app_repo_mappings("org-a")] == ["app-a"]
    assert [m.app_id for m in load_app_repo_mappings("org-b")] == ["app-b"]
    # An org absent from an org-keyed config with no default gets nothing.
    assert load_app_repo_mappings("org-c") == []


def test_array_config_applies_to_every_org(live):
    config = [{"app_id": "shared", "platform": "java", "repos": ["r1"]}]
    live.setenv(arm._CONFIG_ENV, json.dumps(config))
    assert repo_ids_for_app("any-org", "shared") == ("r1",)
    assert repo_ids_for_app("other-org", "shared") == ("r1",)


def test_missing_config_yields_no_apps(live):
    # Live mode, no ENTERPRISE_APP_REPOS set → nothing configured (never discovered).
    assert load_app_repo_mappings("default") == []


def test_malformed_env_json_raises(live):
    live.setenv(arm._CONFIG_ENV, "{not json")
    with pytest.raises(EnterpriseAppConfigError):
        load_app_repo_mappings("default")


# ─────────────────────────────────────────────────────────────────────────────
# The mapping DRIVES extraction (T6 → T1 bridge)
# ─────────────────────────────────────────────────────────────────────────────
_COVENANT_CONTENT = {
    "covenant-web": [
        {
            "path": "web/src/main/java/com/acme/web/CovenantController.java",
            "content": (
                "package com.acme.web;\n"
                "import org.springframework.web.bind.annotation.*;\n"
                "@RestController\n@RequestMapping(\"/covenants\")\n"
                "public class CovenantController {\n"
                "  @GetMapping(\"/{id}\")\n"
                "  public Object get(@PathVariable Long id) { return null; }\n"
                "}\n"
            ),
        },
        {
            "path": "web/pom.xml",
            "content": (
                "<project xmlns='http://maven.apache.org/POM/4.0.0'>"
                "<artifactId>covenant-web</artifactId><dependencies><dependency>"
                "<groupId>org.springframework</groupId><artifactId>spring-web</artifactId>"
                "<version>6.1.3</version></dependency></dependencies></project>"
            ),
        },
    ],
    "covenant-core": [
        {
            "path": "core/src/main/java/com/acme/core/CovenantService.java",
            "content": (
                "package com.acme.core;\n"
                "import org.springframework.stereotype.Service;\n"
                "@Service\npublic class CovenantService {}\n"
            ),
        },
    ],
}


def _provider(repo_id):
    return _COVENANT_CONTENT.get(repo_id, [])


def test_repo_content_for_app_gathers_configured_repos_in_order(offline):
    files = repo_content_for_app("default", "covenant-service", _provider)
    paths = [f["path"] for f in files]
    # covenant-web listed before covenant-core (configured order), all its files.
    assert paths[0].startswith("web/")
    assert any(p.startswith("core/") for p in paths)


def test_extract_app_structure_parses_union_of_repos(offline):
    s = extract_app_structure("default", "covenant-service", _provider)
    assert s.platform == "java"
    qns = {c.qualified_name for c in s.components}
    # Component from covenant-web AND component from covenant-core — one application.
    assert "com.acme.web.CovenantController" in qns
    assert "com.acme.core.CovenantService" in qns
    assert ("GET", "/covenants/{id}") in {(e.method, e.path) for e in s.endpoints}
    assert any(d.name == "spring-web" and d.version == "6.1.3" for d in s.dependencies)


def test_extract_app_structure_unconfigured_app_raises(offline):
    with pytest.raises(EnterpriseAppConfigError):
        extract_app_structure("default", "ghost-app", _provider)


# ─────────────────────────────────────────────────────────────────────────────
# Validation + phase-one discipline
# ─────────────────────────────────────────────────────────────────────────────
def test_mapping_validation():
    with pytest.raises(EnterpriseAppConfigError):
        AppRepoMapping(app_id="", name="x", platform="java", repo_ids=("r",))
    with pytest.raises(EnterpriseAppConfigError):
        AppRepoMapping(app_id="x", name="x", platform="python", repo_ids=("r",))
    with pytest.raises(EnterpriseAppConfigError):
        AppRepoMapping(app_id="x", name="x", platform="java", repo_ids=())


def test_inline_secret_in_config_is_rejected(live):
    config = [
        {"app_id": "leaky", "platform": "java", "repos": ["r1"], "token": "sk-SECRET"},
        {"app_id": "clean", "platform": "java", "repos": ["r2"]},
    ]
    live.setenv(arm._CONFIG_ENV, json.dumps(config))
    ids = {m.app_id for m in load_app_repo_mappings("default")}
    assert "leaky" not in ids  # rejected — a mapping must carry no secret
    assert "clean" in ids  # one bad entry does not sink the rest


def test_duplicate_app_id_keeps_first(live):
    config = [
        {"app_id": "dup", "platform": "java", "repos": ["first"]},
        {"app_id": "dup", "platform": "dotnet", "repos": ["second"]},
    ]
    live.setenv(arm._CONFIG_ENV, json.dumps(config))
    mappings = load_app_repo_mappings("default")
    assert len(mappings) == 1
    assert mappings[0].platform == "java"
    assert mappings[0].repo_ids == ("first",)


def test_repo_belongs_to_exactly_one_app(live):
    # 'shared-repo' is claimed by two apps; the first wins, keeping app_for_repo
    # unambiguous. The second app keeps only its unclaimed repos.
    config = [
        {"app_id": "first-app", "platform": "java", "repos": ["shared-repo", "a"]},
        {"app_id": "second-app", "platform": "java", "repos": ["shared-repo", "b"]},
    ]
    live.setenv(arm._CONFIG_ENV, json.dumps(config))
    assert app_for_repo("default", "shared-repo").app_id == "first-app"
    assert repo_ids_for_app("default", "second-app") == ("b",)


def test_app_with_no_unclaimed_repos_is_skipped(live):
    config = [
        {"app_id": "first-app", "platform": "java", "repos": ["only-repo"]},
        {"app_id": "second-app", "platform": "java", "repos": ["only-repo"]},
    ]
    live.setenv(arm._CONFIG_ENV, json.dumps(config))
    ids = {m.app_id for m in load_app_repo_mappings("default")}
    assert ids == {"first-app"}  # second-app had only an already-claimed repo


def test_service_falls_back_to_app_id():
    m = AppRepoMapping(app_id="svc", name="Svc", platform="java", repo_ids=("r",))
    assert m.service == "svc"
    m2 = AppRepoMapping(
        app_id="svc",
        name="Svc",
        platform="java",
        repo_ids=("r",),
        metadata={"service": "billing"},
    )
    assert m2.service == "billing"
