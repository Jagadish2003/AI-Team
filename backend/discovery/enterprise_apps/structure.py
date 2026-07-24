"""R18-A6 / AT-606 (T1) — deterministic application structure extraction.

Reads repository CONTENT already ingested by R18-A2 (:mod:`discovery.ingest.
git_content`) — build files, source files, and configuration — and extracts the
application's STRUCTURE: its components, build-declared dependencies, declared
REST endpoints, and configuration shape (keys kept, values redacted).

Two rules govern this module (R18-A6 §"Structure is observed, never inferred",
AC1/AC6):

  1. **Deterministic, no LLM in the extraction path.** Everything here is parsed
     from build files, annotations/attributes and directory convention. There is
     no model call anywhere in this file — application structure stays inside the
     observed-evidence class so the graph can arbitrate with it. (A structural
     test pins the no-model-import rule.)
  2. **Configuration values never surface — only keys.** Every scalar value in a
     parsed config file is replaced with :data:`REDACTED` before it enters the
     :class:`AppStructure`; only the key shape survives. Combined with R18-A2's
     secret redaction on the content path, a seeded secret in config is absent
     everywhere (AC6).

Shared model, platform parsers at the edges
-------------------------------------------
Following the share-the-extraction discipline from the operational phase
(:mod:`discovery.ingest.operational_signals`), the DATA MODEL
(:class:`Component` / :class:`Dependency` / :class:`Endpoint` /
:class:`AppStructure`) is platform-agnostic and lives here once;
platform-specific PARSERS sit at the edges. :func:`extract_structure` dispatches
on ``platform``. AT-606 (T1) implements the **Java** parser; AT-607 (T2) adds the
matching **.NET** parser against the same model with no change to this contract:
solutions/csproj projects and NuGet package references become ``module``
components and versioned :class:`Dependency` entries, ``[ApiController]``/
``ControllerBase``-derived (or ``*Controller``-named) classes become
``controller`` components with their ``[Http*]``/``[Route]``-declared
:class:`Endpoint` entries, ``*Service``/``*Repository``-named classes (and background
services) become ``service``/``repository`` components, and ``appsettings*.json``
contributes to ``config_shape`` with every value redacted (AC6).

This module is pure — no DB, no ``app`` import — so it is trivially testable and,
per the R18-A6 "rides A2, adds interpretation" note, reusable by any future code
mover (GitLab/Bitbucket) that hands it repository content.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

try:  # PyYAML is a hard dependency (requirements.txt); degrade loudly if absent.
    import yaml
except Exception:  # pragma: no cover — yaml is installed in every supported env
    yaml = None  # type: ignore

logger = logging.getLogger(__name__)

#: Placeholder written in place of every configuration VALUE (AC6). Only keys are
#: kept, so no config value — secret or otherwise — ever appears in the structure.
REDACTED = "***REDACTED***"

#: The platforms this module knows about. Only ``java`` has a parser in this
#: subtask (AT-606); ``dotnet`` is the T2 seam.
PLATFORM_JAVA = "java"
PLATFORM_DOTNET = "dotnet"


# ─────────────────────────────────────────────────────────────────────────────
# Input model — repository content handed in from R18-A2
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RepoFile:
    """One repository file's path + text content (R18-A2 already moved the body).

    ``path`` is the repo-relative path (forward-slashed); ``content`` is the
    decoded text. This is exactly the ``{path, content}`` shape the Git content
    ingestor reads, so A2-ingested files can be handed here unchanged.
    """

    path: str
    content: str


def _coerce_files(repo_content: Iterable[Any]) -> List[RepoFile]:
    """Coerce assorted file shapes into :class:`RepoFile` (tolerant of A2 shapes).

    Accepts :class:`RepoFile`, a ``{"path", "content"}`` mapping, or a
    ``(path, content)`` pair — whatever the caller has on hand. Entries without a
    usable path are dropped (a file with no path cannot carry provenance); missing
    content normalises to an empty string. Paths are normalised to forward slashes.
    """
    files: List[RepoFile] = []
    for item in repo_content or []:
        if isinstance(item, RepoFile):
            path, content = item.path, item.content
        elif isinstance(item, dict):
            path, content = item.get("path"), item.get("content")
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            path, content = item
        else:
            continue
        norm = _norm_path(str(path or ""))
        if not norm:
            continue
        files.append(RepoFile(path=norm, content=str(content or "")))
    return files


def _norm_path(path: str) -> str:
    """Repo-relative path: strip, collapse ``\\`` → ``/``, drop a leading ``/``."""
    return (path or "").strip().replace("\\", "/").lstrip("/")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if path else ""


# ─────────────────────────────────────────────────────────────────────────────
# Shared structure model (platform-agnostic)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Component:
    """A structural component — a service, controller, repository, or module.

    ``kind`` is the normalised role (``service`` / ``controller`` /
    ``controller_advice`` / ``repository`` / ``configuration`` / ``component`` /
    ``module``); ``qualified_name`` is the fully-qualified identifier
    (``package.Class`` for Java types). ``path`` is the source file the component
    was observed in — the code provenance the graph join (T3) rides on.
    ``annotations`` records the raw stereotype annotation/attribute names found.
    """

    name: str
    kind: str
    qualified_name: str
    path: str
    platform: str
    annotations: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["annotations"] = list(self.annotations)
        return d


@dataclass(frozen=True)
class Dependency:
    """A build-declared, versioned dependency.

    ``name`` is the artifact/package id, ``group`` the Maven groupId (``None`` for
    Gradle string-notation without a group, or NuGet), ``version`` the declared
    version (``None`` when the build file omits it or defers to a BOM/central
    version), ``scope`` the declared configuration/scope (``compile`` / ``test`` /
    ``implementation`` / …). ``manifest`` names the build system (``maven`` /
    ``gradle`` / ``nuget``) and ``path`` is the build file — the dependency's
    provenance.
    """

    name: str
    version: Optional[str]
    group: Optional[str]
    scope: Optional[str]
    manifest: str
    path: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Endpoint:
    """A declared REST endpoint (HTTP method + route).

    ``method`` is the HTTP verb (``GET`` / ``POST`` / … or ``ANY`` when the
    declaration does not pin one). ``path`` is the full route — the controller's
    base path joined with the handler mapping. ``component`` is the declaring
    controller's simple name and ``handler`` the handler method; ``source_path`` is
    the source file — the endpoint's provenance.
    """

    method: str
    path: str
    component: Optional[str]
    handler: Optional[str]
    platform: str
    source_path: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppStructure:
    """A single application's extracted structure (the shared model).

    All four layers the story names — ``components``, ``dependencies``,
    ``endpoints`` and ``config_shape`` (keys kept, values redacted) — plus the
    ``config_files`` that contributed to the shape (config provenance).
    Everything is deterministically ordered so two extractions of the same content
    are byte-identical.
    """

    platform: str
    components: Tuple[Component, ...] = ()
    dependencies: Tuple[Dependency, ...] = ()
    endpoints: Tuple[Endpoint, ...] = ()
    config_shape: Dict[str, Any] = field(default_factory=dict)
    config_files: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform,
            "components": [c.to_dict() for c in self.components],
            "dependencies": [d.to_dict() for d in self.dependencies],
            "endpoints": [e.to_dict() for e in self.endpoints],
            "config_shape": self.config_shape,
            "config_files": list(self.config_files),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Entry point — platform dispatch
# ─────────────────────────────────────────────────────────────────────────────
def extract_structure(repo_content: Iterable[Any], platform: str) -> AppStructure:
    """Extract an :class:`AppStructure` from repository content, deterministically.

    ``repo_content`` is the application's files (see :func:`_coerce_files` for the
    shapes accepted); ``platform`` selects the parser (``'java'`` | ``'dotnet'``).
    No model is ever consulted — structure is observed, not inferred (AC1/AC2).

    Raises :class:`ValueError` for an unknown platform, so a mis-wired caller
    fails loudly rather than silently returning nothing.
    """
    key = (platform or "").strip().lower()
    parser = _PARSERS.get(key)
    if parser is None:
        raise ValueError(
            f"unknown platform {platform!r}; expected one of {sorted(_PARSERS)!r}"
        )
    files = _coerce_files(repo_content)
    return parser(files)


# ═════════════════════════════════════════════════════════════════════════════
# Java parser (AT-606)
# ═════════════════════════════════════════════════════════════════════════════

# Spring stereotype annotation (simple name) → normalised component kind. Ordered
# by priority so a class carrying several stereotypes takes the most specific.
_JAVA_STEREOTYPE_KIND: Tuple[Tuple[str, str], ...] = (
    ("RestController", "controller"),
    ("Controller", "controller"),
    ("RestControllerAdvice", "controller_advice"),
    ("ControllerAdvice", "controller_advice"),
    ("Service", "service"),
    ("Repository", "repository"),
    ("Configuration", "configuration"),
    ("Component", "component"),
)
_JAVA_STEREOTYPES = {name for name, _ in _JAVA_STEREOTYPE_KIND}
_JAVA_CONTROLLER_STEREOTYPES = {"RestController", "Controller"}

# Method-mapping annotation (simple name) → HTTP verb. ``RequestMapping`` has no
# fixed verb; its verb comes from the ``method = RequestMethod.XXX`` attribute.
_JAVA_MAPPING_VERB: Dict[str, str] = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}
_JAVA_MAPPING_ANNOTATIONS = set(_JAVA_MAPPING_VERB) | {"RequestMapping"}

# package x.y.z;
_JAVA_PACKAGE_RE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)

# An annotated type declaration: a run of one or more annotations, optional
# modifiers, then class/interface/enum/record + name. Group 1 is the annotation
# run (parsed for stereotype names), group 3 the type name.
_JAVA_TYPE_DECL_RE = re.compile(
    r"((?:@[\w.]+\s*(?:\([^()]*\))?\s*)+)"
    r"(?:(?:public|final|abstract|sealed|non-sealed|private|protected|static)\s+)*"
    r"(class|interface|enum|record)\s+([A-Za-z_]\w*)"
)

# A single @Annotation with its (optional, non-nested) argument string.
_JAVA_ANNOTATION_RE = re.compile(r"@([\w.]+)\s*(?:\(([^()]*)\))?")

# RequestMethod.GET / RequestMethod.POST … inside a @RequestMapping arg string.
_JAVA_REQUEST_METHOD_RE = re.compile(r"RequestMethod\.([A-Za-z]+)")

# First string literal in an annotation arg (the route), preferring value=/path=.
_JAVA_VALUE_ATTR_RE = re.compile(r"(?:value|path)\s*=\s*\{?\s*\"([^\"]*)\"")
_JAVA_FIRST_STRING_RE = re.compile(r"\"([^\"]*)\"")


def _extract_java(files: List[RepoFile]) -> AppStructure:
    """Deterministic Java structure extraction (AC1)."""
    components: List[Component] = []
    endpoints: List[Endpoint] = []
    dependencies: List[Dependency] = []
    config_shape: Dict[str, Any] = {}
    config_files: List[str] = []

    for f in sorted(files, key=lambda x: x.path):
        name = _basename(f.path).lower()
        if name == "pom.xml":
            dependencies.extend(_parse_maven(f))
            components.extend(_maven_modules(f))
        elif name in ("build.gradle", "build.gradle.kts"):
            dependencies.extend(_parse_gradle(f))
            components.append(_gradle_module(f))
        elif f.path.endswith(".java"):
            comps, eps = _parse_java_source(f)
            components.extend(comps)
            endpoints.extend(eps)
        elif _is_config_file(name):
            shape = _parse_config_file(f)
            if shape:
                _deep_merge(config_shape, shape)
                config_files.append(f.path)

    return AppStructure(
        platform=PLATFORM_JAVA,
        components=tuple(_dedupe_sorted_components(components)),
        dependencies=tuple(
            sorted(
                _dedupe_dependencies(dependencies),
                key=lambda d: (d.path, d.group or "", d.name, d.version or "", d.scope or ""),
            )
        ),
        endpoints=tuple(
            sorted(
                _dedupe_endpoints(endpoints),
                key=lambda e: (e.source_path, e.path, e.method, e.handler or ""),
            )
        ),
        config_shape=config_shape,
        config_files=tuple(sorted(set(config_files))),
    )


# ── Java source: Spring components + REST endpoints ──────────────────────────
def _parse_java_source(f: RepoFile) -> Tuple[List[Component], List[Endpoint]]:
    """Parse one ``.java`` file for stereotyped components and REST endpoints."""
    text = _strip_c_style_comments(f.content)
    pkg_match = _JAVA_PACKAGE_RE.search(text)
    package = pkg_match.group(1) if pkg_match else ""

    # 1) Annotated type declarations → components; remember controllers + their
    #    base path (from a class-level @RequestMapping) for endpoint joining.
    components: List[Component] = []
    controllers: List[Tuple[int, str, str]] = []  # (decl_pos, class_name, base_path)
    for m in _JAVA_TYPE_DECL_RE.finditer(text):
        ann_names = [
            _simple_name(a) for a, _ in _JAVA_ANNOTATION_RE.findall(m.group(1))
        ]
        stereotypes = [a for a in ann_names if a in _JAVA_STEREOTYPES]
        if not stereotypes:
            continue
        class_name = m.group(3)
        kind = _java_kind_for(stereotypes)
        qualified = f"{package}.{class_name}" if package else class_name
        components.append(
            Component(
                name=class_name,
                kind=kind,
                qualified_name=qualified,
                path=f.path,
                platform=PLATFORM_JAVA,
                annotations=tuple(sorted(set(stereotypes))),
            )
        )
        if any(s in _JAVA_CONTROLLER_STEREOTYPES for s in stereotypes):
            base = _class_base_path(m.group(1))
            controllers.append((m.start(), class_name, base))

    # 2) Method-level mapping annotations → endpoints, joined to the nearest
    #    preceding controller class (robust for multi-controller files).
    endpoints: List[Endpoint] = []
    if controllers:
        controllers.sort(key=lambda c: c[0])
        class_ann_spans = {mm.start(1) for mm in _JAVA_TYPE_DECL_RE.finditer(text)}
        for am in _JAVA_ANNOTATION_RE.finditer(text):
            simple = _simple_name(am.group(1))
            if simple not in _JAVA_MAPPING_ANNOTATIONS:
                continue
            # Skip a @RequestMapping that is the class-level base mapping (already
            # consumed as the controller base path, not an endpoint of its own).
            if simple == "RequestMapping" and _within_class_annotation(
                am.start(), class_ann_spans, text
            ):
                continue
            owner = _nearest_controller(am.start(), controllers)
            if owner is None:
                continue
            _, class_name, base = owner
            args = am.group(2) or ""
            route = _route_from_args(args)
            full = _join_route(base, route)
            handler = _handler_after(text, am.end())
            for verb in _verbs_for(simple, args):
                endpoints.append(
                    Endpoint(
                        method=verb,
                        path=full,
                        component=class_name,
                        handler=handler,
                        platform=PLATFORM_JAVA,
                        source_path=f.path,
                    )
                )
    return components, endpoints


def _java_kind_for(stereotypes: List[str]) -> str:
    for name, kind in _JAVA_STEREOTYPE_KIND:
        if name in stereotypes:
            return kind
    return "component"


def _simple_name(annotation: str) -> str:
    """Simple annotation name — last dotted segment (``org.x.Service`` → ``Service``)."""
    return annotation.rsplit(".", 1)[-1]


def _class_base_path(annotation_run: str) -> str:
    """Base path from a class-level @RequestMapping in the class annotation run."""
    for ann, args in _JAVA_ANNOTATION_RE.findall(annotation_run):
        if _simple_name(ann) == "RequestMapping":
            return _route_from_args(args or "")
    return ""


def _route_from_args(args: str) -> str:
    """Route string from an annotation's args: prefer ``value=``/``path=``, else the
    first bare string literal, else ``""``."""
    if not args:
        return ""
    m = _JAVA_VALUE_ATTR_RE.search(args)
    if m:
        return m.group(1)
    m = _JAVA_FIRST_STRING_RE.search(args)
    return m.group(1) if m else ""


def _verbs_for(annotation_simple: str, args: str) -> List[str]:
    """HTTP verb(s) for a mapping annotation.

    ``@GetMapping`` → ``[GET]`` etc.; a ``@RequestMapping`` takes its verb(s) from
    ``method = RequestMethod.XXX`` (one endpoint per verb), or ``[ANY]`` when it
    pins no method.
    """
    if annotation_simple in _JAVA_MAPPING_VERB:
        return [_JAVA_MAPPING_VERB[annotation_simple]]
    verbs = sorted({v.upper() for v in _JAVA_REQUEST_METHOD_RE.findall(args or "")})
    return verbs or ["ANY"]


def _handler_after(text: str, pos: int) -> Optional[str]:
    """The handler method name declared just after a mapping annotation at ``pos``.

    Consumes any trailing annotations/modifiers, then reads the identifier
    immediately before the parameter-list ``(`` — the method name. Returns ``None``
    if none is found within a bounded window (never scans the whole file)."""
    window = text[pos : pos + 400]
    m = re.match(
        r"\s*(?:@[\w.]+\s*(?:\([^()]*\))?\s*)*"
        r"(?:(?:public|private|protected|static|final|synchronized|abstract|default|native)\s+)*"
        r"[\w.<>\[\],?\s]+?\b([A-Za-z_]\w*)\s*\(",
        window,
    )
    return m.group(1) if m else None


def _within_class_annotation(pos: int, class_ann_starts: set, text: str) -> bool:
    """True when the annotation at ``pos`` belongs to a class-level annotation run.

    A class-level @RequestMapping is part of the annotation block that immediately
    precedes a type declaration; those runs' start offsets are ``class_ann_starts``.
    We treat the mapping as class-level when it sits inside such a run — i.e. its
    position is at/after a run start and the run reaches the class keyword without
    an intervening type declaration.
    """
    for start in class_ann_starts:
        if start <= pos:
            # The run beginning at ``start`` ends at the type keyword; a mapping
            # inside it (before the class name) is class-level.
            decl = _JAVA_TYPE_DECL_RE.match(text, start)
            if decl and start <= pos < decl.start(2):
                return True
    return False


def _nearest_controller(
    pos: int, controllers: List[Tuple[int, str, str]]
) -> Optional[Tuple[int, str, str]]:
    """The controller whose declaration most closely precedes ``pos``."""
    owner = None
    for c in controllers:
        if c[0] <= pos:
            owner = c
        else:
            break
    # A mapping before the first controller decl still belongs to it (annotations
    # precede the class keyword), so fall back to the first controller.
    return owner or (controllers[0] if controllers else None)


def _join_route(base: str, route: str) -> str:
    """Join a controller base path with a handler route into one clean path."""
    parts = [p.strip("/") for p in (base, route) if p and p.strip("/")]
    joined = "/".join(parts)
    return "/" + joined if joined else (base or route or "/")


# ── Maven ────────────────────────────────────────────────────────────────────
def _parse_maven(f: RepoFile) -> List[Dependency]:
    """Parse ``pom.xml`` dependencies (groupId/artifactId/version/scope), resolving
    ``${property}`` versions from ``<properties>`` / ``project.version``."""
    import xml.etree.ElementTree as ET

    try:
        root = _strip_ns(ET.fromstring(f.content))
    except ET.ParseError as exc:
        logger.warning("enterprise_apps: could not parse Maven pom %s: %s", f.path, exc)
        return []

    props: Dict[str, str] = {}
    project_version = root.findtext("version") or root.findtext("parent/version") or ""
    if project_version:
        props["project.version"] = project_version.strip()
    props_el = root.find("properties")
    if props_el is not None:
        for child in list(props_el):
            if child.text:
                props[child.tag] = child.text.strip()

    deps: List[Dependency] = []
    # Both <dependencies> and <dependencyManagement>/<dependencies> declare versions.
    dep_els = list(root.findall("dependencies/dependency")) + list(
        root.findall("dependencyManagement/dependencies/dependency")
    )
    for dep in dep_els:
        group = (dep.findtext("groupId") or "").strip()
        artifact = (dep.findtext("artifactId") or "").strip()
        if not artifact:
            continue
        version = _resolve_maven_version((dep.findtext("version") or "").strip(), props)
        scope = (dep.findtext("scope") or "").strip() or "compile"
        deps.append(
            Dependency(
                name=artifact,
                version=version or None,
                group=group or None,
                scope=scope,
                manifest="maven",
                path=f.path,
            )
        )
    return deps


def _resolve_maven_version(raw: str, props: Dict[str, str]) -> str:
    """Resolve a ``${prop}`` version placeholder from the pom properties, else keep
    the literal (an unresolved placeholder is still what the build declares)."""
    if not raw:
        return ""
    m = re.fullmatch(r"\$\{([\w.]+)\}", raw)
    if m:
        return props.get(m.group(1), raw)
    return raw


def _maven_modules(f: RepoFile) -> List[Component]:
    """Module components from a pom: the project's own artifactId + any submodules
    declared in ``<modules>`` (multi-module reactor build)."""
    import xml.etree.ElementTree as ET

    try:
        root = _strip_ns(ET.fromstring(f.content))
    except ET.ParseError:
        return []
    directory = f.path.rsplit("/", 1)[0] if "/" in f.path else ""
    out: List[Component] = []
    artifact = (root.findtext("artifactId") or "").strip()
    if artifact:
        out.append(
            Component(
                name=artifact,
                kind="module",
                qualified_name=artifact,
                path=f.path,
                platform=PLATFORM_JAVA,
                annotations=(),
            )
        )
    for mod in root.findall("modules/module"):
        sub = (mod.text or "").strip()
        if sub:
            out.append(
                Component(
                    name=sub,
                    kind="module",
                    qualified_name=f"{directory}/{sub}" if directory else sub,
                    path=f.path,
                    platform=PLATFORM_JAVA,
                    annotations=(),
                )
            )
    return out


def _strip_ns(elem):
    """Strip XML namespaces in-place so ``findtext('groupId')`` works on a
    namespaced Maven pom, then return the element."""
    for el in elem.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return elem


# ── Gradle ─────────────────────────────────────────────────────────────────
#: Gradle dependency configurations we recognise (Groovy + Kotlin DSL). Anything
#: matching the suffix heuristic below is also accepted, so custom configurations
#: like ``integrationTestImplementation`` still resolve.
_GRADLE_CONFIGS = {
    "implementation", "api", "compileOnly", "compileOnlyApi", "runtimeOnly",
    "testImplementation", "testCompileOnly", "testRuntimeOnly", "annotationProcessor",
    "kapt", "developmentOnly", "providedRuntime", "providedCompile", "compile",
    "testCompile", "runtime", "testRuntime",
}
_GRADLE_CONFIG_SUFFIXES = (
    "Implementation", "Api", "CompileOnly", "RuntimeOnly", "AnnotationProcessor",
    "Compile", "Runtime",
)
# String-notation dep line: `config 'g:a:v'` or `config("g:a:v")` (also inside
# platform(...)); the first quoted GAV on the line wins.
_GRADLE_STRING_DEP_RE = re.compile(
    r"^\s*([A-Za-z]\w*)\b[^\n]*?['\"]([^'\"\n]+)['\"]", re.MULTILINE
)
# Map-notation dep line: `config group: 'g', name: 'a', version: 'v'`.
_GRADLE_MAP_DEP_RE = re.compile(
    r"^\s*([A-Za-z]\w*)\b[^\n]*?group\s*:\s*['\"]([^'\"]+)['\"][^\n]*?"
    r"name\s*:\s*['\"]([^'\"]+)['\"](?:[^\n]*?version\s*:\s*['\"]([^'\"]+)['\"])?",
    re.MULTILINE,
)


def _is_gradle_config(keyword: str) -> bool:
    return keyword in _GRADLE_CONFIGS or keyword.endswith(_GRADLE_CONFIG_SUFFIXES)


def _parse_gradle(f: RepoFile) -> List[Dependency]:
    """Parse ``build.gradle`` / ``.kts`` dependencies from ``dependencies { }`` blocks.

    Handles string notation (``implementation 'g:a:v'`` / ``implementation("g:a:v")``,
    including ``platform('g:a:v')``) and map notation (``group:/name:/version:``).
    Deterministic text parsing only — the build script is never executed."""
    deps: List[Dependency] = []
    for block in _gradle_dependency_blocks(f.content):
        seen_lines: set = set()
        # Map notation first (its lines also match the string regex, so claim them).
        for m in _GRADLE_MAP_DEP_RE.finditer(block):
            if not _is_gradle_config(m.group(1)):
                continue
            seen_lines.add(m.start())
            deps.append(
                Dependency(
                    name=m.group(3),
                    version=(m.group(4) or None),
                    group=(m.group(2) or None),
                    scope=m.group(1),
                    manifest="gradle",
                    path=f.path,
                )
            )
        for m in _GRADLE_STRING_DEP_RE.finditer(block):
            if m.start() in seen_lines or not _is_gradle_config(m.group(1)):
                continue
            gav = m.group(2).strip()
            if gav.startswith(":") or "/" in gav or gav.startswith("$"):
                continue  # project ref / file path / unresolved var — not a GAV
            group, artifact, version = _split_gav(gav)
            if not artifact:
                continue
            deps.append(
                Dependency(
                    name=artifact,
                    version=version,
                    group=group,
                    scope=m.group(1),
                    manifest="gradle",
                    path=f.path,
                )
            )
    return deps


def _gradle_dependency_blocks(content: str) -> List[str]:
    """The body text of every top-level ``dependencies { ... }`` block, via brace
    matching (so nested braces inside the block are handled)."""
    blocks: List[str] = []
    for m in re.finditer(r"\bdependencies\s*\{", content):
        start = m.end()
        depth = 1
        i = start
        while i < len(content) and depth:
            ch = content[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
        blocks.append(content[start : i - 1])
    return blocks


def _split_gav(gav: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Split a Gradle ``group:artifact:version`` string. Two parts → no version;
    one part → artifact only."""
    parts = gav.split(":")
    if len(parts) >= 3:
        return parts[0] or None, parts[1], parts[2] or None
    if len(parts) == 2:
        return parts[0] or None, parts[1], None
    return None, parts[0], None


def _gradle_module(f: RepoFile) -> Component:
    """One module component for a Gradle build file (module = its directory)."""
    directory = f.path.rsplit("/", 1)[0] if "/" in f.path else ""
    name = _basename(directory) if directory else "root"
    return Component(
        name=name,
        kind="module",
        qualified_name=directory or name,
        path=f.path,
        platform=PLATFORM_JAVA,
        annotations=(),
    )


# ── Configuration files (keys kept, values redacted — AC6) ───────────────────
_CONFIG_FILE_RE = re.compile(
    r"^(application|bootstrap)(-[\w.-]+)?\.(ya?ml|properties)$"
)


def _is_config_file(basename_lower: str) -> bool:
    return bool(_CONFIG_FILE_RE.match(basename_lower))


def _parse_config_file(f: RepoFile) -> Dict[str, Any]:
    """Parse a Spring config file into a redacted key shape (values → REDACTED)."""
    name = _basename(f.path).lower()
    if name.endswith(".properties"):
        return _parse_properties(f.content)
    return _parse_yaml(f.content, f.path)


def _parse_yaml(content: str, path: str) -> Dict[str, Any]:
    """YAML config → nested key shape with every scalar value redacted.

    Multi-document YAML (``---`` separators, common for Spring profiles) is folded
    into one shape. A parse failure degrades to an empty shape with a warning
    rather than raising — a malformed config never sinks extraction."""
    if yaml is None:  # pragma: no cover — yaml is installed in supported envs
        logger.warning("enterprise_apps: PyYAML unavailable; skipping %s", path)
        return {}
    shape: Dict[str, Any] = {}
    try:
        docs = list(yaml.safe_load_all(content))
    except yaml.YAMLError as exc:
        logger.warning("enterprise_apps: could not parse YAML config %s: %s", path, exc)
        return {}
    for doc in docs:
        if isinstance(doc, dict):
            _deep_merge(shape, _redact_values(doc))
    return shape


def _parse_properties(content: str) -> Dict[str, Any]:
    """``.properties`` config → nested key shape (dotted keys expanded), values
    redacted. Only the KEY is read; the value is discarded and replaced."""
    shape: Dict[str, Any] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line[0] in ("#", "!"):
            continue
        # Key ends at the first unescaped '=' or ':' (or whitespace separator).
        key = _property_key(line)
        if key:
            _expand_dotted(shape, key)
    return shape


def _property_key(line: str) -> str:
    """The key portion of a ``.properties`` line (left of the first ``=``/``:``)."""
    for i, ch in enumerate(line):
        if ch in ("=", ":"):
            return line[:i].strip()
        if ch in (" ", "\t") and i > 0:
            # A bare-whitespace separator (`key value`) — but only if no '='/':'
            # appears later; peek ahead.
            rest = line[i:]
            if "=" not in rest and ":" not in rest:
                return line[:i].strip()
    return line.strip()


def _expand_dotted(target: Dict[str, Any], dotted_key: str) -> None:
    """Set a redacted leaf at the nested location named by a dotted key."""
    parts = [p for p in dotted_key.split(".") if p]
    if not parts:
        return
    node = target
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    # Only set a leaf if a deeper structure has not already claimed this key.
    if not isinstance(node.get(parts[-1]), dict):
        node[parts[-1]] = REDACTED


def _redact_values(obj: Any) -> Any:
    """Recursively keep keys/structure but replace every scalar VALUE with
    :data:`REDACTED` (AC6). Dicts recurse; lists keep their shape; every leaf is
    redacted regardless of type (a port, a URL, or a secret — all values)."""
    if isinstance(obj, dict):
        return {str(k): _redact_values(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact_values(v) for v in obj]
    return REDACTED


def _deep_merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge ``incoming`` into ``base``, preferring nested structure over a
    scalar leaf so a key seen as both a leaf and a branch keeps its branch."""
    for k, v in incoming.items():
        existing = base.get(k)
        if isinstance(existing, dict) and isinstance(v, dict):
            _deep_merge(existing, v)
        elif isinstance(existing, dict) and not isinstance(v, dict):
            continue  # keep the richer structure
        else:
            base[k] = v
    return base


# ── C-style comment stripping (Java + C# share `//` and `/* */`) ─────────────
# So a commented-out ``@RestController``/``[ApiController]`` or a mapping
# annotation/attribute inside a doc comment is never mistaken for a real
# declaration. String literals may in theory contain ``//``; annotation/attribute
# parsing tolerates the rare false strip because it only ever reads
# annotation/attribute and class/type tokens.
_C_STYLE_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_C_STYLE_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _strip_c_style_comments(text: str) -> str:
    text = _C_STYLE_BLOCK_COMMENT_RE.sub(" ", text)
    text = _C_STYLE_LINE_COMMENT_RE.sub("", text)
    return text


# ── Dedupe helpers (determinism) ─────────────────────────────────────────────
def _dedupe_sorted_components(components: List[Component]) -> List[Component]:
    seen: set = set()
    out: List[Component] = []
    for c in sorted(components, key=lambda x: (x.path, x.kind, x.qualified_name, x.name)):
        key = (c.qualified_name, c.kind, c.path)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _dedupe_dependencies(deps: List[Dependency]) -> List[Dependency]:
    seen: set = set()
    out: List[Dependency] = []
    for d in deps:
        key = (d.path, d.group, d.name, d.version, d.scope, d.manifest)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _dedupe_endpoints(endpoints: List[Endpoint]) -> List[Endpoint]:
    seen: set = set()
    out: List[Endpoint] = []
    for e in endpoints:
        key = (e.method, e.path, e.component, e.handler, e.source_path)
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# .NET parser (AT-607 / T2) — same AppStructure model, .NET conventions
# ═════════════════════════════════════════════════════════════════════════════

# Base type (simple name) → normalised component kind, mirroring the Java
# stereotype table. ``ApiController``/``ControllerBase``/``Controller`` are the
# ASP.NET Core MVC conventions; a ``*Controller`` name is the fallback when a
# controller omits both (some minimal-hosting styles do).
_DOTNET_CONTROLLER_BASES = {"ControllerBase", "Controller"}
_DOTNET_SERVICE_BASES = {"BackgroundService", "IHostedService"}

# HTTP verb attribute (simple name) → HTTP verb.
_DOTNET_HTTP_VERB: Dict[str, str] = {
    "HttpGet": "GET",
    "HttpPost": "POST",
    "HttpPut": "PUT",
    "HttpDelete": "DELETE",
    "HttpPatch": "PATCH",
    "HttpHead": "HEAD",
    "HttpOptions": "OPTIONS",
}

# namespace Foo.Bar; (file-scoped, C# 10+) or namespace Foo.Bar { ... } (block).
_DOTNET_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w.]+)\s*[;{]", re.MULTILINE)

# `class Name` preceded by optional modifiers. The attribute run (if any) that
# precedes it is resolved separately via `_attribute_runs` — a naive
# `\[[^\[\]]*\]` block regex breaks on ASP.NET Core's own default convention
# `[Route("api/[controller]")]`, whose STRING ARGUMENT contains a nested
# `[controller]` bracket, so block boundaries are found with a string-aware scan
# (`_scan_attribute_blocks`) instead of a single regex.
_DOTNET_CLASS_KEYWORD_RE = re.compile(
    r"(?:(?:public|internal|private|protected|sealed|abstract|static|partial)\s+)*"
    r"class\s+([A-Za-z_]\w*)"
)

_DOTNET_ATTR_ITEM_RE = re.compile(r"^([\w.]+)\s*(?:\(([^()]*)\))?$")

# The handler method name declared just after an attribute run: consumes
# modifiers/return type, then reads the identifier immediately before the
# parameter-list ``(``. Starts right after the run, so no attributes to skip.
_DOTNET_HANDLER_RE = re.compile(
    r"\s*(?:(?:public|private|protected|internal|static|virtual|override|sealed|async|new)\s+)*"
    r"[\w.<>\[\],?]+?\s+([A-Za-z_]\w*)\s*\("
)

_DOTNET_CONFIG_FILE_RE = re.compile(r"^appsettings(\.[\w.-]+)?\.json$")
_DOTNET_PROJECT_EXTS = (".csproj", ".vbproj", ".fsproj")
_DOTNET_SLN_PROJECT_RE = re.compile(
    r'^Project\("\{[0-9A-Fa-f-]+\}"\)\s*=\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"\{[0-9A-Fa-f-]+\}"',
    re.MULTILINE,
)


def _extract_dotnet(files: List[RepoFile]) -> AppStructure:
    """Deterministic .NET structure extraction (AC2), through the shared model."""
    components: List[Component] = []
    endpoints: List[Endpoint] = []
    dependencies: List[Dependency] = []
    config_shape: Dict[str, Any] = {}
    config_files: List[str] = []

    for f in sorted(files, key=lambda x: x.path):
        name = _basename(f.path).lower()
        if name.endswith(".sln"):
            components.extend(_parse_solution(f))
        elif name.endswith(".csproj"):
            dependencies.extend(_parse_csproj_packages(f))
            components.append(_csproj_module(f))
        elif name == "packages.config":
            dependencies.extend(_parse_packages_config(f))
        elif f.path.endswith(".cs"):
            comps, eps = _parse_dotnet_source(f)
            components.extend(comps)
            endpoints.extend(eps)
        elif _DOTNET_CONFIG_FILE_RE.match(name):
            shape = _parse_appsettings(f)
            if shape:
                _deep_merge(config_shape, shape)
                config_files.append(f.path)

    return AppStructure(
        platform=PLATFORM_DOTNET,
        components=tuple(_dedupe_sorted_components(components)),
        dependencies=tuple(
            sorted(
                _dedupe_dependencies(dependencies),
                key=lambda d: (d.path, d.group or "", d.name, d.version or "", d.scope or ""),
            )
        ),
        endpoints=tuple(
            sorted(
                _dedupe_endpoints(endpoints),
                key=lambda e: (e.source_path, e.path, e.method, e.handler or ""),
            )
        ),
        config_shape=config_shape,
        config_files=tuple(sorted(set(config_files))),
    )


# ── Attribute scanning (string-aware — see `_DOTNET_CLASS_KEYWORD_RE` note) ───
def _scan_attribute_blocks(text: str) -> List[Tuple[int, int, str]]:
    """Every top-level ``[...]`` block in ``text`` as ``(start, end, inner)``.

    A ``[``/``]`` inside a double-quoted string literal does not open/close a
    block — only the OUTER bracket pair does — so ``[Route("api/[controller]")]``
    is one block, not a match truncated at the string's own ``[controller]``.
    A block is bounded to a generous length; a ``[`` that never finds its close
    within that bound is treated as a stray bracket (e.g. array syntax), not an
    unterminated attribute, so it can never swallow the rest of the file."""
    blocks: List[Tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "[":
            i += 1
            continue
        j = i + 1
        in_string = False
        limit = min(n, i + 2000)
        while j < limit:
            ch = text[j]
            if ch == '"' and text[j - 1] != "\\":
                in_string = not in_string
            elif ch == "]" and not in_string:
                break
            j += 1
        else:
            j = -1
        if j == -1:
            i += 1
            continue
        blocks.append((i, j + 1, text[i + 1 : j]))
        i = j + 1
    return blocks


def _attribute_runs(text: str) -> Dict[int, Tuple[int, List[str]]]:
    """Group consecutive attribute blocks (whitespace-only gaps) into runs.

    Keyed by each run's END position so a class/method declaration immediately
    following a run can be located by its own start position after stripping
    trailing whitespace back to that same position."""
    blocks = _scan_attribute_blocks(text)
    runs: Dict[int, Tuple[int, List[str]]] = {}
    i = 0
    n = len(blocks)
    while i < n:
        run_start = blocks[i][0]
        inner = [blocks[i][2]]
        prev_end = blocks[i][1]
        j = i + 1
        while j < n and text[prev_end : blocks[j][0]].strip() == "":
            inner.append(blocks[j][2])
            prev_end = blocks[j][1]
            j += 1
        runs[prev_end] = (run_start, inner)
        i = j
    return runs


def _run_attributes(inner_texts: List[str]) -> List[Tuple[str, str]]:
    """Parse a run's block contents into ``(name, args)`` pairs, splitting
    comma-grouped attributes (``[Route("x"), ApiController]``) within a block."""
    attrs: List[Tuple[str, str]] = []
    for block in inner_texts:
        for item in _split_top_level(block, ","):
            item = item.strip()
            if not item:
                continue
            m = _DOTNET_ATTR_ITEM_RE.match(item)
            if m:
                attrs.append((m.group(1), m.group(2) or ""))
    return attrs


# ── C# source: controllers/services/repositories + REST endpoints ────────────
def _parse_dotnet_source(f: RepoFile) -> Tuple[List[Component], List[Endpoint]]:
    """Parse one ``.cs`` file for stereotyped components and REST endpoints."""
    text = _strip_c_style_comments(f.content)
    ns_match = _DOTNET_NAMESPACE_RE.search(text)
    namespace = ns_match.group(1) if ns_match else ""
    runs = _attribute_runs(text)

    components: List[Component] = []
    endpoints: List[Endpoint] = []

    for m in _DOTNET_CLASS_KEYWORD_RE.finditer(text):
        class_name = m.group(1)
        prefix_end = len(text[: m.start()].rstrip())
        run = runs.get(prefix_end)
        attrs = _run_attributes(run[1]) if run else []
        attr_names = {_dotnet_attr_simple_name(n) for n, _ in attrs}

        body_open = text.find("{", m.end())
        if body_open == -1:
            continue
        base_str = _dotnet_base_list_from_header(text[m.end() : body_open])
        bases = {_dotnet_simple_base(b) for b in _split_top_level(base_str, ",")} if base_str else set()
        body_close = _match_brace(text, body_open)
        if body_close is None:
            continue

        kind = _dotnet_kind_for(class_name, attr_names, bases)
        if kind is None:
            continue
        qualified = f"{namespace}.{class_name}" if namespace else class_name
        components.append(
            Component(
                name=class_name,
                kind=kind,
                qualified_name=qualified,
                path=f.path,
                platform=PLATFORM_DOTNET,
                annotations=tuple(sorted(attr_names)),
            )
        )
        if kind != "controller":
            continue

        base_route = ""
        for n, args in attrs:
            if _dotnet_attr_simple_name(n) == "Route":
                base_route = _first_quoted(args)
                break

        body_text = text[body_open + 1 : body_close]
        for run_end, (run_start, inner_texts) in _attribute_runs(body_text).items():
            run_attrs = _run_attributes(inner_texts)
            verb: Optional[str] = None
            verb_route = ""
            route_attr = ""
            for n, args in run_attrs:
                simple = _dotnet_attr_simple_name(n)
                if simple in _DOTNET_HTTP_VERB and verb is None:
                    verb = _DOTNET_HTTP_VERB[simple]
                    verb_route = _first_quoted(args)
                elif simple == "Route":
                    route_attr = _first_quoted(args)
            if verb is None:
                continue
            handler = _dotnet_handler_after(body_text, run_end)
            full = _join_route(base_route, verb_route or route_attr)
            full = _substitute_dotnet_tokens(full, class_name, handler)
            endpoints.append(
                Endpoint(
                    method=verb,
                    path=full,
                    component=class_name,
                    handler=handler,
                    platform=PLATFORM_DOTNET,
                    source_path=f.path,
                )
            )

    return components, endpoints


def _dotnet_kind_for(class_name: str, attr_names: set, bases: set) -> Optional[str]:
    if "ApiController" in attr_names:
        return "controller"
    if bases & _DOTNET_CONTROLLER_BASES:
        return "controller"
    if class_name.endswith("Controller"):
        return "controller"
    if class_name.endswith("Service"):
        return "service"
    if bases & _DOTNET_SERVICE_BASES:
        return "service"
    if class_name.endswith("Repository"):
        return "repository"
    if class_name == "Startup":
        return "configuration"
    return None


def _dotnet_base_list_from_header(header_tail: str) -> str:
    """The base/interface list text between a class name and its ``{`` body.

    Strips a leading generic type-parameter clause (``<T>``) and a trailing
    generic constraint clause (``where T : ...``), neither of which is a base
    type, leaving just the comma-separated base/interface list (or ``""``)."""
    tail = re.sub(r"^\s*<.*?>", "", header_tail, count=1, flags=re.DOTALL)
    m = re.match(r"\s*:\s*(.*)$", tail, re.DOTALL)
    if not m:
        return ""
    bases = m.group(1)
    where_idx = bases.find(" where ")
    if where_idx != -1:
        bases = bases[:where_idx]
    return bases.strip()


def _dotnet_simple_base(name: str) -> str:
    """Simple base-type name: strip generic args and namespace qualification."""
    name = name.strip()
    lt = name.find("<")
    if lt != -1:
        name = name[:lt]
    return name.rsplit(".", 1)[-1].strip()


def _split_top_level(s: str, sep: str) -> List[str]:
    """Split ``s`` on ``sep`` at bracket depth 0 (``()``/``<>``/``[]`` all nest).

    Used for both C# base/interface lists and attribute argument lists, where a
    naive ``str.split`` would break on a comma inside a generic argument or a
    nested attribute call."""
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in s:
        if ch in "(<[":
            depth += 1
            current.append(ch)
        elif ch in ")>]":
            depth -= 1
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _dotnet_attr_simple_name(name: str) -> str:
    """Simple attribute name: strip namespace qualification and a trailing
    ``Attribute`` suffix (``[ApiController]`` and ``[ApiControllerAttribute]``
    are the same attribute)."""
    simple = name.rsplit(".", 1)[-1]
    if simple.endswith("Attribute") and len(simple) > len("Attribute"):
        simple = simple[: -len("Attribute")]
    return simple


def _first_quoted(args: str) -> str:
    """First string literal in an attribute's argument text, else ``""``."""
    m = _JAVA_FIRST_STRING_RE.search(args or "")
    return m.group(1) if m else ""


def _substitute_dotnet_tokens(path: str, controller_name: str, handler: Optional[str]) -> str:
    """Resolve ASP.NET Core's ``[controller]``/``[action]`` route tokens."""
    ctrl_token = controller_name
    if ctrl_token.lower().endswith("controller"):
        ctrl_token = ctrl_token[: -len("controller")]
    out = re.sub(r"\[controller\]", ctrl_token, path, flags=re.IGNORECASE)
    if handler:
        out = re.sub(r"\[action\]", handler, out, flags=re.IGNORECASE)
    return out


def _dotnet_handler_after(text: str, pos: int) -> Optional[str]:
    window = text[pos : pos + 400]
    m = _DOTNET_HANDLER_RE.match(window)
    return m.group(1) if m else None


def _match_brace(text: str, open_pos: int) -> Optional[int]:
    """Index of the ``}`` matching the ``{`` at ``open_pos``, via brace counting."""
    depth = 0
    i = open_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


# ── Solution + project files (modules) ────────────────────────────────────────
def _parse_solution(f: RepoFile) -> List[Component]:
    """Module components for each project declared in a ``.sln`` file.

    Solution folders (a project entry whose path has no project-file extension)
    are not modules and are skipped."""
    out: List[Component] = []
    for m in _DOTNET_SLN_PROJECT_RE.finditer(f.content):
        proj_name, proj_path = m.group(1), _norm_path(m.group(2))
        if not proj_path.lower().endswith(_DOTNET_PROJECT_EXTS):
            continue
        out.append(
            Component(
                name=proj_name,
                kind="module",
                qualified_name=proj_path,
                path=f.path,
                platform=PLATFORM_DOTNET,
                annotations=(),
            )
        )
    return out


def _csproj_module(f: RepoFile) -> Component:
    """One module component for a ``.csproj`` (module = the project itself)."""
    basename = _basename(f.path)
    name = basename[: -len(".csproj")] if basename.lower().endswith(".csproj") else basename
    return Component(
        name=name,
        kind="module",
        qualified_name=name,
        path=f.path,
        platform=PLATFORM_DOTNET,
        annotations=(),
    )


# ── NuGet package references ─────────────────────────────────────────────────
def _parse_csproj_packages(f: RepoFile) -> List[Dependency]:
    """Parse ``<PackageReference>`` elements (attribute or child-element version;
    ``Version`` is absent under central package management — kept as ``None``,
    matching a build's own declaration)."""
    import xml.etree.ElementTree as ET

    try:
        root = _strip_ns(ET.fromstring(f.content))
    except ET.ParseError as exc:
        logger.warning("enterprise_apps: could not parse csproj %s: %s", f.path, exc)
        return []

    deps: List[Dependency] = []
    for pkg in root.findall(".//PackageReference"):
        name = (pkg.get("Include") or pkg.get("Update") or "").strip()
        if not name:
            continue
        version = (pkg.get("Version") or pkg.findtext("Version") or "").strip() or None
        deps.append(
            Dependency(
                name=name, version=version, group=None, scope=None, manifest="nuget", path=f.path
            )
        )
    return deps


def _parse_packages_config(f: RepoFile) -> List[Dependency]:
    """Parse legacy ``packages.config`` ``<package id="" version="" />`` entries."""
    import xml.etree.ElementTree as ET

    try:
        root = _strip_ns(ET.fromstring(f.content))
    except ET.ParseError as exc:
        logger.warning("enterprise_apps: could not parse packages.config %s: %s", f.path, exc)
        return []

    deps: List[Dependency] = []
    for pkg in root.findall("package"):
        pkg_id = (pkg.get("id") or "").strip()
        if not pkg_id:
            continue
        version = (pkg.get("version") or "").strip() or None
        deps.append(
            Dependency(
                name=pkg_id, version=version, group=None, scope=None, manifest="nuget", path=f.path
            )
        )
    return deps


# ── appsettings*.json (keys kept, values redacted — AC6) ─────────────────────
def _parse_appsettings(f: RepoFile) -> Dict[str, Any]:
    """Parse an ``appsettings*.json`` file into a redacted key shape.

    A parse failure degrades to an empty shape with a warning rather than
    raising — a malformed config never sinks extraction."""
    try:
        doc = json.loads(f.content)
    except ValueError as exc:
        logger.warning("enterprise_apps: could not parse appsettings %s: %s", f.path, exc)
        return {}
    if isinstance(doc, dict):
        return _redact_values(doc)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# Platform parser registry
# ─────────────────────────────────────────────────────────────────────────────
_PARSERS: Dict[str, Callable[[List[RepoFile]], AppStructure]] = {
    PLATFORM_JAVA: _extract_java,
    PLATFORM_DOTNET: _extract_dotnet,
}
