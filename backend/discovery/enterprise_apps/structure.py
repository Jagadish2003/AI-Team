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
on ``platform``. This subtask (AT-606) implements the **Java** parser; the
matching **.NET** parser (T2) registers against the same model with no change to
this contract.

This module is pure — no DB, no ``app`` import — so it is trivially testable and,
per the R18-A6 "rides A2, adds interpretation" note, reusable by any future code
mover (GitLab/Bitbucket) that hands it repository content.
"""

from __future__ import annotations

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
    No model is ever consulted — structure is observed, not inferred (AC1).

    Raises :class:`ValueError` for an unknown platform, and
    :class:`NotImplementedError` for a known platform whose parser has not landed
    yet (``dotnet`` — T2), so a mis-wired caller fails loudly rather than silently
    returning nothing.
    """
    key = (platform or "").strip().lower()
    parser = _PARSERS.get(key)
    if parser is None:
        if key == PLATFORM_DOTNET:
            raise NotImplementedError(
                "R18-A6 T1 (AT-606) implements the Java structure parser; the .NET "
                "parser arrives in T2. Call extract_structure(..., platform='java')."
            )
        raise ValueError(
            f"unknown platform {platform!r}; expected one of "
            f"{sorted(_PARSERS) + [PLATFORM_DOTNET]!r}"
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
    text = _strip_java_comments(f.content)
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


# ── Java comment stripping (so annotations in comments are never parsed) ─────
_JAVA_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_JAVA_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _strip_java_comments(text: str) -> str:
    """Remove block and line comments so a commented-out ``@RestController`` or an
    ``@GetMapping`` inside Javadoc is never mistaken for a real declaration.
    String literals may in theory contain ``//``; annotation parsing tolerates the
    rare false strip because it only ever reads annotation/class tokens."""
    text = _JAVA_BLOCK_COMMENT_RE.sub(" ", text)
    text = _JAVA_LINE_COMMENT_RE.sub("", text)
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


# ─────────────────────────────────────────────────────────────────────────────
# Platform parser registry — the T2 (.NET) seam plugs in here.
# ─────────────────────────────────────────────────────────────────────────────
_PARSERS: Dict[str, Callable[[List[RepoFile]], AppStructure]] = {
    PLATFORM_JAVA: _extract_java,
}
