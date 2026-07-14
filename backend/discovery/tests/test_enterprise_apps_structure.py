"""R18-A6 / AT-606 (T1) — Java application structure extraction.

Covers the subtask acceptance criteria:

  * **AC1** — deterministic Java extraction over repository content: components
    (Spring stereotypes + modules), build-declared versioned dependencies
    (Maven + Gradle), declared REST endpoints, and configuration keys — with NO
    model call anywhere in the extraction path (pinned structurally).
  * **AC6** — configuration VALUES never surface: only keys are kept, every value
    is redacted, and a seeded secret in config is absent everywhere.

Pure/offline: :mod:`discovery.enterprise_apps.structure` touches no DB and no
``app`` package, so these tests run with the deterministic discovery suite and
need no fixtures beyond the in-memory sources below.
"""
from __future__ import annotations

import json
from pathlib import Path

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
# In-memory Java application content
# ─────────────────────────────────────────────────────────────────────────────
POM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.acme</groupId>
  <artifactId>covenant-service</artifactId>
  <version>1.4.2</version>
  <packaging>pom</packaging>
  <properties>
    <spring.version>6.1.3</spring.version>
  </properties>
  <modules>
    <module>core</module>
    <module>web</module>
  </modules>
  <dependencies>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-webmvc</artifactId>
      <version>${spring.version}</version>
    </dependency>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.10.1</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
"""

BUILD_GRADLE = """
plugins { id 'org.springframework.boot' version '3.2.1' }
dependencies {
    implementation 'org.springframework.boot:spring-boot-starter-web:3.2.1'
    implementation("com.google.guava:guava:32.1.3-jre")
    implementation platform('io.micrometer:micrometer-bom:1.12.1')
    implementation group: 'commons-io', name: 'commons-io', version: '2.15.1'
    testImplementation 'org.mockito:mockito-core:5.8.0'
    implementation project(':core')
}
"""

CONTROLLER_JAVA = """
package com.acme.web;

import org.springframework.web.bind.annotation.*;

/** Handles covenant CRUD. @GetMapping("/ghost") in this javadoc must be ignored. */
@RestController
@RequestMapping("/api/covenants")
public class CovenantController {

    @GetMapping("/{id}")
    public ResponseEntity<CovenantDto> getCovenant(@PathVariable Long id) {
        return ResponseEntity.ok(null);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public CovenantDto createCovenant(@RequestBody CovenantDto dto) {
        return dto;
    }

    @RequestMapping(value = "/search", method = RequestMethod.GET)
    public List<CovenantDto> search(@RequestParam String q) {
        return null;
    }

    // @DeleteMapping("/{id}")  <- commented out, must NOT become an endpoint
}
"""

SERVICE_JAVA = """
package com.acme.core;

import org.springframework.stereotype.Service;
import org.springframework.stereotype.Repository;

@Service
public class CovenantService {
}

@Repository
class CovenantRepository {
}
"""

APPLICATION_YML = """
server:
  port: 8443
spring:
  datasource:
    url: jdbc:postgresql://prod-db:5432/covenants
    username: covenant_app
    password: S3cr3t-Yaml-P@ss
  jpa:
    show-sql: true
"""

APPLICATION_PROPERTIES = """
# Spring application config
app.name=Covenant Service
app.api.key=SUPERSECRET-PROP-KEY-9876
logging.level.root=INFO
"""

# Every config VALUE that must never appear anywhere in the extracted structure.
SEEDED_CONFIG_VALUES = (
    "S3cr3t-Yaml-P@ss",
    "SUPERSECRET-PROP-KEY-9876",
    "covenant_app",
    "jdbc:postgresql://prod-db:5432/covenants",
    "Covenant Service",
    "8443",
    "5432",
)


def _java_app_files():
    return [
        RepoFile("pom.xml", POM_XML),
        RepoFile("build.gradle", BUILD_GRADLE),
        RepoFile("web/src/main/java/com/acme/web/CovenantController.java", CONTROLLER_JAVA),
        RepoFile("core/src/main/java/com/acme/core/CovenantService.java", SERVICE_JAVA),
        RepoFile("src/main/resources/application.yml", APPLICATION_YML),
        RepoFile("src/main/resources/application.properties", APPLICATION_PROPERTIES),
    ]


# ═════════════════════════════════════════════════════════════════════════════
# AC1 — deterministic Java extraction
# ═════════════════════════════════════════════════════════════════════════════
def test_extract_returns_shared_app_structure_for_java():
    s = extract_structure(_java_app_files(), "java")
    assert isinstance(s, AppStructure)
    assert s.platform == "java"
    assert all(isinstance(c, Component) for c in s.components)
    assert all(isinstance(d, Dependency) for d in s.dependencies)
    assert all(isinstance(e, Endpoint) for e in s.endpoints)
    assert isinstance(s.config_shape, dict)


def test_components_capture_spring_stereotypes_with_provenance():
    s = extract_structure(_java_app_files(), "java")
    by_qn = {c.qualified_name: c for c in s.components}

    ctrl = by_qn["com.acme.web.CovenantController"]
    assert ctrl.kind == "controller"
    assert ctrl.name == "CovenantController"
    assert "RestController" in ctrl.annotations
    # Code provenance: the component knows the source file it was observed in.
    assert ctrl.path == "web/src/main/java/com/acme/web/CovenantController.java"

    assert by_qn["com.acme.core.CovenantService"].kind == "service"
    assert by_qn["com.acme.core.CovenantRepository"].kind == "repository"


def test_modules_are_extracted_from_build_files():
    s = extract_structure(_java_app_files(), "java")
    modules = {c.name for c in s.components if c.kind == "module"}
    # Maven artifactId + declared submodules + the Gradle module (root dir).
    assert {"covenant-service", "core", "web"} <= modules


def test_maven_dependencies_versioned_and_property_resolved():
    s = extract_structure(_java_app_files(), "java")
    maven = {d.name: d for d in s.dependencies if d.manifest == "maven"}

    web = maven["spring-webmvc"]
    assert web.group == "org.springframework"
    assert web.version == "6.1.3"  # ${spring.version} resolved from <properties>
    assert web.scope == "compile"  # default scope

    junit = maven["junit-jupiter"]
    assert junit.version == "5.10.1"
    assert junit.scope == "test"


def test_gradle_dependencies_string_and_map_notation():
    s = extract_structure(_java_app_files(), "java")
    gradle = {d.name: d for d in s.dependencies if d.manifest == "gradle"}

    assert gradle["spring-boot-starter-web"].version == "3.2.1"
    assert gradle["guava"].version == "32.1.3-jre"
    assert gradle["micrometer-bom"].version == "1.12.1"      # platform('g:a:v')
    assert gradle["commons-io"].version == "2.15.1"          # map notation
    assert gradle["mockito-core"].scope == "testImplementation"
    # A Gradle project(':core') ref is not a versioned artifact — never a dep.
    assert "core" not in gradle


def test_rest_endpoints_join_base_path_and_verbs():
    s = extract_structure(_java_app_files(), "java")
    routes = {(e.method, e.path): e for e in s.endpoints}

    assert ("GET", "/api/covenants/{id}") in routes
    assert routes[("GET", "/api/covenants/{id}")].handler == "getCovenant"
    assert routes[("GET", "/api/covenants/{id}")].component == "CovenantController"

    assert ("POST", "/api/covenants") in routes           # no method path → base
    assert routes[("POST", "/api/covenants")].handler == "createCovenant"

    # @RequestMapping(method = RequestMethod.GET) resolves to a GET verb.
    assert ("GET", "/api/covenants/search") in routes
    assert routes[("GET", "/api/covenants/search")].handler == "search"


def test_commented_out_mappings_are_not_endpoints():
    s = extract_structure(_java_app_files(), "java")
    # No DELETE endpoint (the only DELETE mapping is commented out), and the
    # javadoc @GetMapping("/ghost") never appears.
    assert not any(e.method == "DELETE" for e in s.endpoints)
    assert not any(e.path.endswith("/ghost") for e in s.endpoints)


def test_extraction_is_deterministic():
    files = _java_app_files()
    first = extract_structure(files, "java").to_dict()
    second = extract_structure(list(reversed(files)), "java").to_dict()
    assert first == second  # order of input files must not change the result


def test_no_model_call_in_extraction_path():
    """AC1: structure is observed, never inferred — no LLM in the extraction path.

    Pinned structurally (mirrors the retrieval→llm_enrichment structural test):
    the module must not import the model gateway, an LLM enrichment path, or a
    provider SDK.
    """
    src = Path(structure_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("model_gateway", "llm_enrichment", "anthropic", "openai"):
        assert forbidden not in src, f"extraction path references {forbidden!r}"


# ═════════════════════════════════════════════════════════════════════════════
# AC6 — configuration values never surface (keys kept, values redacted)
# ═════════════════════════════════════════════════════════════════════════════
def test_config_shape_keeps_keys_redacts_values():
    s = extract_structure(_java_app_files(), "java")
    shape = s.config_shape

    # Keys are preserved (nested for YAML, dotted-expanded for properties)…
    assert set(shape) >= {"server", "spring", "app", "logging"}
    assert "port" in shape["server"]
    assert set(shape["spring"]["datasource"]) == {"url", "username", "password"}
    assert "key" in shape["app"]["api"]

    # …but every leaf value is the redaction placeholder, never a real value.
    assert shape["server"]["port"] == structure_mod.REDACTED
    assert shape["spring"]["datasource"]["password"] == structure_mod.REDACTED
    assert shape["app"]["api"]["key"] == structure_mod.REDACTED


def test_seeded_config_secrets_absent_everywhere():
    """No seeded config value (secret or otherwise) appears anywhere in the
    serialised structure — config_shape, components, deps, or endpoints (AC6)."""
    s = extract_structure(_java_app_files(), "java")
    blob = json.dumps(s.to_dict())
    for value in SEEDED_CONFIG_VALUES:
        assert value not in blob, f"config value leaked into structure: {value!r}"


def test_config_files_recorded_as_provenance():
    s = extract_structure(_java_app_files(), "java")
    assert "src/main/resources/application.yml" in s.config_files
    assert "src/main/resources/application.properties" in s.config_files


def test_profile_and_bootstrap_config_files_are_parsed():
    files = [
        RepoFile("application-prod.yaml", "feature:\n  flag: enabled\n"),
        RepoFile("bootstrap.properties", "spring.cloud.config.uri=http://config\n"),
    ]
    s = extract_structure(files, "java")
    assert s.config_shape["feature"]["flag"] == structure_mod.REDACTED
    assert s.config_shape["spring"]["cloud"]["config"]["uri"] == structure_mod.REDACTED
    assert "enabled" not in json.dumps(s.to_dict())
    assert "http://config" not in json.dumps(s.to_dict())


# ═════════════════════════════════════════════════════════════════════════════
# Robustness / seams
# ═════════════════════════════════════════════════════════════════════════════
def test_empty_content_yields_empty_structure():
    s = extract_structure([], "java")
    assert s.components == () and s.dependencies == () and s.endpoints == ()
    assert s.config_shape == {}


def test_malformed_pom_degrades_without_raising():
    files = [RepoFile("pom.xml", "<project><dependencies><dependency>oops")]
    s = extract_structure(files, "java")  # must not raise
    assert isinstance(s, AppStructure)
    assert s.dependencies == ()


def test_accepts_dict_and_tuple_file_shapes():
    dict_files = [{"path": "pom.xml", "content": POM_XML}]
    tuple_files = [("pom.xml", POM_XML)]
    assert extract_structure(dict_files, "java").to_dict() == (
        extract_structure(tuple_files, "java").to_dict()
    )


def test_dotnet_platform_is_the_t2_seam():
    with pytest.raises(NotImplementedError):
        extract_structure([], "dotnet")


def test_unknown_platform_raises_value_error():
    with pytest.raises(ValueError):
        extract_structure([], "python")


def test_platform_is_case_insensitive():
    assert extract_structure(_java_app_files(), "JAVA").platform == "java"
