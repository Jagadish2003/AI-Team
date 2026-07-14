"""R18-A6 / AT-607 (T2) — .NET application structure extraction.

Covers the subtask acceptance criteria, through the SAME shared model T1 (Java,
AT-606) established — no change to :func:`extract_structure`'s contract:

  * **AC2** — deterministic .NET extraction over repository content: components
    (ASP.NET Core controllers/services/repositories + solution/csproj modules),
    build-declared versioned dependencies (NuGet PackageReference + legacy
    packages.config), declared REST endpoints (``[Http*]``/``[Route]``,
    including the ``[controller]``/``[action]`` route-token convention), and
    configuration keys (``appsettings*.json``) — with NO model call anywhere in
    the extraction path.
  * **AC6** — configuration VALUES never surface: only keys are kept, every
    value is redacted, and a seeded secret in config is absent everywhere.

Pure/offline: :mod:`discovery.enterprise_apps.structure` touches no DB and no
``app`` package, so these tests run with the deterministic discovery suite and
need no fixtures beyond the in-memory sources below.
"""
from __future__ import annotations

import json

import pytest

from discovery.enterprise_apps import (
    AppStructure,
    Component,
    Dependency,
    Endpoint,
    RepoFile,
    extract_structure,
)
from discovery.enterprise_apps import structure as structure_mod


# ─────────────────────────────────────────────────────────────────────────────
# In-memory .NET application content
# ─────────────────────────────────────────────────────────────────────────────
SLN = r"""
Microsoft Visual Studio Solution File, Format Version 12.00
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Covenant.Api", "src\Covenant.Api\Covenant.Api.csproj", "{11111111-1111-1111-1111-111111111111}"
EndProject
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Covenant.Core", "src\Covenant.Core\Covenant.Core.csproj", "{22222222-2222-2222-2222-222222222222}"
EndProject
Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "Solution Items", "Solution Items", "{33333333-3333-3333-3333-333333333333}"
EndProject
"""

CSPROJ_API = """<Project Sdk="Microsoft.NET.Sdk.Web">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
    <PackageReference Include="Swashbuckle.AspNetCore" Version="6.5.0" />
    <PackageReference Include="Serilog.AspNetCore" />
  </ItemGroup>
</Project>
"""

PACKAGES_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<packages>
  <package id="Microsoft.Owin" version="4.2.2" targetFramework="net472" />
</packages>
"""

CONTROLLER_CS = """
namespace Acme.Covenant.Web;

using Microsoft.AspNetCore.Mvc;

/** Handles covenant CRUD. [HttpGet("/ghost")] in this doc comment must be ignored. */
[ApiController]
[Route("api/[controller]")]
public class CovenantController : ControllerBase
{
    [HttpGet("{id}")]
    public IActionResult GetCovenant(long id)
    {
        return Ok();
    }

    [HttpPost]
    public IActionResult CreateCovenant([FromBody] CovenantDto dto)
    {
        return Ok(dto);
    }

    [HttpGet]
    [Route("search")]
    public IActionResult Search(string q)
    {
        return Ok();
    }

    [HttpGet("[action]")]
    public IActionResult Ping()
    {
        return Ok();
    }

    // [HttpDelete("{id}")]
    // public IActionResult DeleteCovenant(long id) => NoContent();
}
"""

SERVICE_AND_REPOSITORY_CS = """
namespace Acme.Covenant.Core;

public class CovenantService : ICovenantService
{
}

public class CovenantRepository : IRepository<Covenant, Guid>
{
}

public class CovenantDto
{
    public string Name { get; set; }
}
"""

APPSETTINGS_JSON = """{
  "ConnectionStrings": {
    "Default": "Server=prod-db;Database=covenants;User Id=covenant_app;Password=S3cr3t-Json-P@ss;"
  },
  "Logging": {
    "LogLevel": {
      "Default": "Information"
    }
  },
  "AllowedHosts": "*"
}
"""

APPSETTINGS_PROD_JSON = """{
  "ApiKeys": {
    "External": "SUPERSECRET-DOTNET-KEY-1234"
  }
}
"""

# Every config VALUE that must never appear anywhere in the extracted structure.
SEEDED_CONFIG_VALUES = (
    "S3cr3t-Json-P@ss",
    "covenant_app",
    "Server=prod-db;Database=covenants;User Id=covenant_app;Password=S3cr3t-Json-P@ss;",
    "SUPERSECRET-DOTNET-KEY-1234",
)


def _dotnet_app_files():
    return [
        RepoFile("Covenant.sln", SLN),
        RepoFile("src/Covenant.Api/Covenant.Api.csproj", CSPROJ_API),
        RepoFile("src/Covenant.Api/packages.config", PACKAGES_CONFIG),
        RepoFile("src/Covenant.Api/Controllers/CovenantController.cs", CONTROLLER_CS),
        RepoFile("src/Covenant.Core/CovenantService.cs", SERVICE_AND_REPOSITORY_CS),
        RepoFile("src/Covenant.Api/appsettings.json", APPSETTINGS_JSON),
        RepoFile("src/Covenant.Api/appsettings.Production.json", APPSETTINGS_PROD_JSON),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# AC2 — deterministic .NET extraction, through the shared model
# ═════════════════════════════════════════════════════════════════════════════
def test_extract_returns_shared_app_structure_for_dotnet():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    assert isinstance(s, AppStructure)
    assert s.platform == "dotnet"
    assert all(isinstance(c, Component) for c in s.components)
    assert all(isinstance(d, Dependency) for d in s.dependencies)
    assert all(isinstance(e, Endpoint) for e in s.endpoints)
    assert isinstance(s.config_shape, dict)


def test_dotnet_no_longer_raises_not_implemented():
    """AT-607 lands the .NET parser — the T1-era seam no longer applies."""
    s = extract_structure([], "dotnet")
    assert isinstance(s, AppStructure) and s.platform == "dotnet"


def test_modules_from_solution_and_csproj():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    modules = {c.name for c in s.components if c.kind == "module"}
    # .sln projects (solution folder excluded) + each project's own csproj.
    assert {"Covenant.Api", "Covenant.Core"} <= modules
    assert "Solution Items" not in modules


def test_controller_component_with_provenance():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    by_qn = {c.qualified_name: c for c in s.components}
    ctrl = by_qn["Acme.Covenant.Web.CovenantController"]
    assert ctrl.kind == "controller"
    assert ctrl.name == "CovenantController"
    assert "ApiController" in ctrl.annotations
    assert ctrl.path == "src/Covenant.Api/Controllers/CovenantController.cs"


def test_service_and_repository_components_from_naming_convention():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    by_qn = {c.qualified_name: c for c in s.components}
    assert by_qn["Acme.Covenant.Core.CovenantService"].kind == "service"
    assert by_qn["Acme.Covenant.Core.CovenantRepository"].kind == "repository"
    # A plain DTO with no recognised stereotype/base/suffix is not a component.
    assert "Acme.Covenant.Core.CovenantDto" not in by_qn


def test_nuget_dependencies_versioned():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    nuget = {d.name: d for d in s.dependencies if d.manifest == "nuget"}

    assert nuget["Newtonsoft.Json"].version == "13.0.3"
    assert nuget["Swashbuckle.AspNetCore"].version == "6.5.0"
    assert nuget["Microsoft.Owin"].version == "4.2.2"  # legacy packages.config
    # Central-package-management style reference: no version pinned locally.
    assert nuget["Serilog.AspNetCore"].version is None


def test_rest_endpoints_with_controller_and_action_tokens():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    routes = {(e.method, e.path): e for e in s.endpoints}

    # [Route("api/[controller]")] resolves [controller] -> "Covenant".
    assert ("GET", "/api/Covenant/{id}") in routes
    assert routes[("GET", "/api/Covenant/{id}")].handler == "GetCovenant"
    assert routes[("GET", "/api/Covenant/{id}")].component == "CovenantController"

    assert ("POST", "/api/Covenant") in routes  # [HttpPost] with no route -> base
    assert routes[("POST", "/api/Covenant")].handler == "CreateCovenant"

    # Split [HttpGet] + [Route("search")] attributes combine into one endpoint.
    assert ("GET", "/api/Covenant/search") in routes
    assert routes[("GET", "/api/Covenant/search")].handler == "Search"

    # [action] token resolves to the handler method name.
    assert ("GET", "/api/Covenant/Ping") in routes
    assert routes[("GET", "/api/Covenant/Ping")].handler == "Ping"


def test_commented_out_mappings_are_not_endpoints():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    assert not any(e.method == "DELETE" for e in s.endpoints)
    assert not any(e.path.endswith("/ghost") for e in s.endpoints)


def test_dotnet_extraction_is_deterministic():
    files = _dotnet_app_files()
    first = extract_structure(files, "dotnet").to_dict()
    second = extract_structure(list(reversed(files)), "dotnet").to_dict()
    assert first == second  # order of input files must not change the result


def test_platform_is_case_insensitive_for_dotnet():
    assert extract_structure(_dotnet_app_files(), "DOTNET").platform == "dotnet"


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — configuration values never surface (keys kept, values redacted)
# ═════════════════════════════════════════════════════════════════════════════
def test_appsettings_shape_keeps_keys_redacts_values():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    shape = s.config_shape

    assert set(shape) >= {"ConnectionStrings", "Logging", "AllowedHosts", "ApiKeys"}
    assert "Default" in shape["ConnectionStrings"]
    assert shape["ConnectionStrings"]["Default"] == structure_mod.REDACTED
    assert shape["Logging"]["LogLevel"]["Default"] == structure_mod.REDACTED
    assert shape["ApiKeys"]["External"] == structure_mod.REDACTED


def test_seeded_appsettings_secrets_absent_everywhere():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    blob = json.dumps(s.to_dict())
    for value in SEEDED_CONFIG_VALUES:
        assert value not in blob, f"config value leaked into structure: {value!r}"


def test_appsettings_files_recorded_as_provenance():
    s = extract_structure(_dotnet_app_files(), "dotnet")
    assert "src/Covenant.Api/appsettings.json" in s.config_files
    assert "src/Covenant.Api/appsettings.Production.json" in s.config_files


# ═════════════════════════════════════════════════════════════════════════════
# Robustness / seams
# ═════════════════════════════════════════════════════════════════════════════
def test_empty_content_yields_empty_structure_for_dotnet():
    s = extract_structure([], "dotnet")
    assert s.components == () and s.dependencies == () and s.endpoints == ()
    assert s.config_shape == {}


def test_malformed_csproj_degrades_without_raising():
    files = [RepoFile("Broken.csproj", "<Project><ItemGroup><PackageReference")]
    s = extract_structure(files, "dotnet")  # must not raise
    assert isinstance(s, AppStructure)
    assert not any(d.manifest == "nuget" for d in s.dependencies)


def test_malformed_appsettings_degrades_without_raising():
    files = [RepoFile("appsettings.json", "{not valid json")]
    s = extract_structure(files, "dotnet")  # must not raise
    assert isinstance(s, AppStructure)
    assert s.config_shape == {}


def test_nested_controller_token_bracket_does_not_break_attribute_scanning():
    """The ASP.NET Core default `[Route("api/[controller]")]` nests a `[...]`
    token INSIDE the attribute's own string argument — a naive bracket-matching
    regex truncates on the inner bracket. This pins that the string-aware scan
    handles it (also exercised implicitly by the full fixture above)."""
    files = [
        RepoFile(
            "OnlyController.cs",
            "namespace Acme;\n"
            "[ApiController]\n"
            '[Route("api/[controller]")]\n'
            "public class WidgetController : ControllerBase\n"
            "{\n"
            '    [HttpGet("{id}")]\n'
            "    public IActionResult Get(int id) { return Ok(); }\n"
            "}\n",
        )
    ]
    s = extract_structure(files, "dotnet")
    assert [c for c in s.components if c.kind == "controller"][0].name == "WidgetController"
    assert ("GET", "/api/Widget/{id}") in {(e.method, e.path) for e in s.endpoints}


def test_unknown_platform_still_raises_value_error():
    with pytest.raises(ValueError):
        extract_structure([], "cobol")
