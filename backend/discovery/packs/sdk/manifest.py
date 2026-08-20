"""Pack manifest schema — 2.0-C3 T1 (AT-836).

The declarative definition of a pack: identity, compatibility (2.0-C1),
certification placeholder (2.0-C2), detectors composed from primitives with
parameters, scorer calibration, terminology, template defaults, and the required
normalised concepts.

The constraint this schema exists to enforce
--------------------------------------------
2.0-C3's governing rule is that **no partner-supplied executable code runs inside
a customer deployment**. A manifest is therefore a *closed* document:

* every field is enumerated — an unknown key is a hard error, never ignored.
  A schema that ignores what it does not understand is a schema an author can
  smuggle through;
* every detector names a primitive from the closed library
  (:mod:`.primitives`) and every parameter is checked against that primitive's
  typed, bounded contract;
* every concept a detector reads is a normalised concept the platform declares
  (:mod:`..platform_capabilities`) — the same vocabulary 2.0-B4 generalises;
* code-shaped content is refused outright: a key like ``module`` / ``script`` /
  ``eval`` anywhere in the document, or a value carrying an import statement, a
  dotted Python module path, or an executable file suffix.

The first-party registry (``pack_config.PACK_REGISTRY``) lists detectors as
importable module paths. A manifest deliberately CANNOT: that field does not
exist in this schema, and a manifest may not claim a first-party pack id.

Certification is a placeholder, and that is load-bearing
--------------------------------------------------------
A manifest may state the level its author will *request*. It may not state the
level it *holds*, and it may not carry a signature — those are issued by
CloudFulcrum after review (2.0-C2 AT-831/AT-832). If an author could write
``"level": "certified"`` into their own manifest the signature would no longer be
the trust root, which is the exact hole 2.0-C2 closes. A manifest supplying
either is refused by name.

Validation is total, not first-failure
--------------------------------------
:func:`validate_manifest` reports EVERY problem it finds, each with a JSON path
and a machine-readable code, because an author fixing a manifest one error per
round trip is an author who gives up. :func:`parse_manifest` is the raising
variant for callers that want the object or nothing.

Dependency-free of ``app`` (no DB, no I/O beyond an explicit file read), matching
the rest of ``discovery/packs``, so offline authoring tooling can use it.

Scope note
----------
This task owns the SCHEMA. The authoring toolkit (scaffold, harness, lint) and
packaging/installation are separate tasks that build on it — they consume
:func:`validate_manifest`, :func:`manifest_fingerprint`, and
:func:`manifest_to_pack_config` rather than re-deriving the rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..pack_certification import (
    LEVEL_CERTIFIED,
    LEVEL_COMMUNITY,
    LEVEL_PARTNER,
    canonical_payload_bytes,
)
from ..pack_config import PACK_REGISTRY
from ..platform_capabilities import (
    compare_versions,
    describe_concept,
    get_concept,
    is_concept_known,
    parse_version,
)
from .primitives import (
    KIND_BOOLEAN,
    KIND_ENUM,
    KIND_INTEGER,
    PRIMITIVE_LIBRARY_VERSION,
    ParameterSpec,
    PrimitiveSpec,
    describe_primitive,
    get_primitive,
    primitive_ids,
)

# ── Schema identity ───────────────────────────────────────────────────────────

#: The manifest schema tag. Part of the document AND of the fingerprint, so a
#: future schema revision can never be silently read under this one's rules.
MANIFEST_VERSION = "agentiq-pack-manifest-v1"

# ── Error codes ───────────────────────────────────────────────────────────────

CODE_UNKNOWN_FIELD = "unknown_field"
CODE_MISSING_FIELD = "missing_field"
CODE_INVALID_TYPE = "invalid_type"
CODE_INVALID_VALUE = "invalid_value"
CODE_DUPLICATE = "duplicate_value"
CODE_RESERVED_PACK_ID = "reserved_pack_id"
CODE_RESERVED_FIELD = "reserved_field"
CODE_UNKNOWN_PRIMITIVE = "unknown_primitive"
CODE_UNKNOWN_PARAMETER = "unknown_parameter"
CODE_MISSING_PARAMETER = "missing_parameter"
CODE_PARAMETER_OUT_OF_RANGE = "parameter_out_of_range"
CODE_UNKNOWN_CONCEPT = "unknown_concept"
CODE_UNDECLARED_CONCEPT = "undeclared_concept"
CODE_CONCEPT_REQUIRES_NEWER_PLATFORM = "concept_requires_newer_platform"
CODE_EXECUTABLE_CODE_FORBIDDEN = "executable_code_forbidden"
CODE_CONFIDENCE_CEILING = "confidence_ceiling_exceeded"
CODE_WEIGHTS_INVALID = "impact_weights_invalid"

# ── Field vocabularies ────────────────────────────────────────────────────────

TOP_LEVEL_FIELDS = (
    "manifestVersion",
    "primitiveLibraryVersion",
    "pack",
    "compatibility",
    "certification",
    "detectors",
    "scorerCalibration",
    "terminology",
    "templateDefaults",
)
REQUIRED_TOP_LEVEL_FIELDS = ("manifestVersion", "pack", "compatibility", "detectors")

PACK_FIELDS = ("packId", "packName", "packVersion", "domain", "description", "author")
REQUIRED_PACK_FIELDS = ("packId", "packName", "packVersion", "description", "author")

AUTHOR_FIELDS = ("name", "contact", "url")
REQUIRED_AUTHOR_FIELDS = ("name", "contact")

COMPATIBILITY_FIELDS = (
    "minPlatformVersion",
    "maxPlatformVersion",
    "requiredConcepts",
    "optionalConcepts",
)
REQUIRED_COMPATIBILITY_FIELDS = ("minPlatformVersion", "requiredConcepts")

CERTIFICATION_FIELDS = ("requestedLevel", "contact", "notes")
#: Certification fields only CloudFulcrum may write (2.0-C2). Present in a
#: manifest ⇒ refusal naming the field, never a silent drop.
CERTIFICATION_ISSUED_FIELDS = (
    "level",
    "signature",
    "certifyingEntity",
    "reviewDate",
    "reviewedAgainstPlatformVersion",
    "scope",
)
REQUESTABLE_LEVELS = (LEVEL_CERTIFIED, LEVEL_PARTNER, LEVEL_COMMUNITY)

DETECTOR_FIELDS = (
    "detectorId",
    "title",
    "primitive",
    "concepts",
    "parameters",
    "labels",
    "enabledByDefault",
)
REQUIRED_DETECTOR_FIELDS = ("detectorId", "title", "primitive", "concepts")
DETECTOR_LABEL_KEYS = ("summary", "whyItMatters", "recommendation", "evidenceHint")

SCORER_FIELDS = ("impactWeights", "confidence", "dimensions")
#: The impact dimensions a manifest may weight — the same four the first-party
#: ops-impact scorer calibrates, so a partner pack ranks on the platform's
#: scoring engine rather than shipping one.
IMPACT_DIMENSIONS = (
    "effort_concentration",
    "breadth",
    "recurrence_stability",
    "automation_shape",
)
#: Weights must sum to 1 within this tolerance — floats, not a demand for exactness.
WEIGHT_SUM_TOLERANCE = 0.001

CONFIDENCE_FIELDS = ("singleSourceCap", "corroboratedMax", "conversationSourceCap")
CONFIDENCE_LEVELS = ("LOW", "MEDIUM", "HIGH")
#: Standing platform ceilings a pack config may lower but never raise: a single
#: source and conversation-derived content cannot reach HIGH, whatever a manifest
#: would prefer (the R16/1.9 confidence discipline, enforced at authoring time).
CONFIDENCE_MAX_ALLOWED: Dict[str, str] = {
    "singleSourceCap": "MEDIUM",
    "conversationSourceCap": "MEDIUM",
    "corroboratedMax": "HIGH",
}
_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

TERMINOLOGY_FIELDS = ("glossary", "languageMap", "llmContext")

TEMPLATE_FIELDS = (
    "industry",
    "systems",
    "recommendedSystems",
    "workflowFocus",
    "roles",
)

# ── Code-shape defences ───────────────────────────────────────────────────────

#: Keys that would carry (or point at) executable content. Refused ANYWHERE in the
#: document, at any depth, whatever their value — a manifest has no legitimate use
#: for them, and enumerating them makes the refusal specific rather than a vague
#: "invalid manifest".
FORBIDDEN_KEYS = frozenset(
    {
        "code",
        "command",
        "detectormodule",
        "entrypoint",
        "eval",
        "exec",
        "expression",
        "hook",
        "import",
        "module",
        "modulepath",
        "plugin",
        "pluginpath",
        "python",
        "script",
        "shell",
        "source_code",
        "sourcecode",
    }
)

#: Value patterns that indicate code rather than configuration.
_CODE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # Deliberately anchored on the `import` keyword itself: a bare `from ...`
    # match would flag ordinary prose ("findings from ServiceNow queues").
    (
        "an import statement",
        re.compile(r"(?:^|[\s;])(?:import\s+[A-Za-z_][\w.]*|from\s+[A-Za-z_][\w.]*\s+import\b)"),
    ),
    ("a lambda expression", re.compile(r"\blambda\b\s*[\w,]*\s*:")),
    ("a dynamic evaluation call", re.compile(r"\b(?:eval|exec|compile|__import__)\s*\(")),
    ("a subprocess or shell invocation", re.compile(r"\b(?:subprocess|os\.system|popen)\b", re.I)),
    ("a dunder attribute", re.compile(r"__\w+__")),
    ("a shebang line", re.compile(r"^#!\s*/")),
    (
        "an executable file reference",
        re.compile(r"\.(?:py|pyc|pyo|sh|bash|ps1|bat|cmd|exe|dll|so|jar|js|rb)\b", re.I),
    ),
)

#: Fields whose values must be plain identifiers, so a dotted module path there is
#: caught even though prose fields legitimately contain full stops.
_IDENTIFIER_FIELDS = ("primitive", "detectorId", "packId", "domain", "industry")
_MODULE_PATH_RE = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*){2,}$")

# ── Identifier shapes ─────────────────────────────────────────────────────────

_PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_DETECTOR_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,79}$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_TERM_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManifestError:
    """One validation failure.

    ``code``    machine-readable (the ``CODE_*`` constants) so tooling can group.
    ``path``    JSON path into the manifest — what the author has to go and edit.
    ``message`` one sentence, used verbatim in a refusal.
    """

    code: str
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.path}: {self.message}"

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class ManifestValidationError(ValueError):
    """Raised by :func:`parse_manifest` when a manifest is not valid.

    ``str(exc)`` lists every error, so an installer can pass it straight into a
    refusal that reports specific failures rather than "invalid manifest".
    """

    def __init__(self, errors: Sequence[ManifestError]) -> None:
        self.errors: List[ManifestError] = list(errors)
        detail = "; ".join(str(error) for error in self.errors) or "unknown error"
        super().__init__(f"Pack manifest is not valid: {detail}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": "pack_manifest_invalid",
            "message": str(self),
            "errors": [error.to_dict() for error in self.errors],
        }


# ── Manifest model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PackAuthor:
    name: str
    contact: str
    url: str = ""

    def to_dict(self) -> Dict[str, str]:
        out = {"name": self.name, "contact": self.contact}
        if self.url:
            out["url"] = self.url
        return out


@dataclass(frozen=True)
class DetectorDeclaration:
    """One detector, composed from a primitive.

    ``parameters`` are the AUTHOR-supplied values only. :meth:`resolved_parameters`
    applies the primitive's declared defaults — kept separate so a manifest's
    fingerprint covers what the author wrote, not what the library defaulted.
    """

    detector_id: str
    title: str
    primitive: str
    concepts: Tuple[str, ...]
    parameters: Mapping[str, Any] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    enabled_by_default: bool = True

    def resolved_parameters(self) -> Dict[str, Any]:
        spec = get_primitive(self.primitive)
        resolved: Dict[str, Any] = {}
        if spec is not None:
            for parameter in spec.parameters:
                if parameter.default is not None:
                    resolved[parameter.name] = parameter.default
        resolved.update(dict(self.parameters))
        return resolved

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "detectorId": self.detector_id,
            "title": self.title,
            "primitive": self.primitive,
            "concepts": list(self.concepts),
            "parameters": dict(self.parameters),
            "enabledByDefault": self.enabled_by_default,
        }
        if self.labels:
            out["labels"] = dict(self.labels)
        return out


@dataclass(frozen=True)
class PackManifest:
    """A validated pack manifest.

    Only ever produced by :func:`parse_manifest` / :func:`validate_manifest`, so
    holding one means the document passed every rule in this module.
    """

    manifest_version: str
    pack_id: str
    pack_name: str
    pack_version: str
    domain: str
    description: str
    author: PackAuthor
    min_platform_version: str
    max_platform_version: Optional[str]
    required_concepts: Tuple[str, ...]
    optional_concepts: Tuple[str, ...]
    detectors: Tuple[DetectorDeclaration, ...]
    requested_certification_level: str = LEVEL_COMMUNITY
    certification_contact: str = ""
    certification_notes: str = ""
    impact_weights: Mapping[str, float] = field(default_factory=dict)
    confidence_caps: Mapping[str, str] = field(default_factory=dict)
    scorer_dimensions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    glossary: Mapping[str, str] = field(default_factory=dict)
    language_map: Mapping[str, str] = field(default_factory=dict)
    llm_context: str = ""
    template_defaults: Mapping[str, Any] = field(default_factory=dict)
    primitive_library_version: str = PRIMITIVE_LIBRARY_VERSION

    def detector(self, detector_id: str) -> Optional[DetectorDeclaration]:
        for declaration in self.detectors:
            if declaration.detector_id == detector_id:
                return declaration
        return None

    @property
    def declared_concepts(self) -> Tuple[str, ...]:
        """Required + optional concepts, order-preserved and de-duplicated."""
        seen: List[str] = []
        for concept in tuple(self.required_concepts) + tuple(self.optional_concepts):
            if concept not in seen:
                seen.append(concept)
        return tuple(seen)

    def to_dict(self) -> Dict[str, Any]:
        """The normalised manifest document — the fingerprinted shape.

        Round-trips through :func:`parse_manifest`: optional blocks are emitted
        only when the author declared something, so re-parsing this output yields
        an equal manifest.
        """
        document: Dict[str, Any] = {
            "manifestVersion": self.manifest_version,
            "primitiveLibraryVersion": self.primitive_library_version,
            "pack": {
                "packId": self.pack_id,
                "packName": self.pack_name,
                "packVersion": self.pack_version,
                "domain": self.domain,
                "description": self.description,
                "author": self.author.to_dict(),
            },
            "compatibility": {
                "minPlatformVersion": self.min_platform_version,
                "maxPlatformVersion": self.max_platform_version,
                "requiredConcepts": list(self.required_concepts),
                "optionalConcepts": list(self.optional_concepts),
            },
            "certification": {
                "requestedLevel": self.requested_certification_level,
            },
            "detectors": [
                declaration.to_dict() for declaration in self.detectors
            ],
        }
        if self.certification_contact:
            document["certification"]["contact"] = self.certification_contact
        if self.certification_notes:
            document["certification"]["notes"] = self.certification_notes

        scorer: Dict[str, Any] = {}
        if self.impact_weights:
            scorer["impactWeights"] = dict(self.impact_weights)
        if self.confidence_caps:
            scorer["confidence"] = dict(self.confidence_caps)
        if self.scorer_dimensions:
            scorer["dimensions"] = {
                name: dict(values) for name, values in self.scorer_dimensions.items()
            }
        if scorer:
            document["scorerCalibration"] = scorer

        terminology: Dict[str, Any] = {}
        if self.glossary:
            terminology["glossary"] = dict(self.glossary)
        if self.language_map:
            terminology["languageMap"] = dict(self.language_map)
        if self.llm_context:
            terminology["llmContext"] = self.llm_context
        if terminology:
            document["terminology"] = terminology

        if self.template_defaults:
            document["templateDefaults"] = dict(self.template_defaults)
        return document


# ── Validation ────────────────────────────────────────────────────────────────


@dataclass
class ManifestValidation:
    """The verdict for one manifest document. Never raises — the result IS the report."""

    errors: List[ManifestError] = field(default_factory=list)
    manifest: Optional[PackManifest] = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.ok,
            "errors": [error.to_dict() for error in self.errors],
        }


class _Validator:
    """Collects every error rather than stopping at the first."""

    def __init__(self) -> None:
        self.errors: List[ManifestError] = []

    # -- error helpers --------------------------------------------------------

    def add(self, code: str, path: str, message: str) -> None:
        self.errors.append(ManifestError(code, path, message))

    def unknown_fields(
        self, path: str, block: Mapping[str, Any], allowed: Iterable[str]
    ) -> None:
        permitted = set(allowed)
        for key in block:
            if key not in permitted:
                self.add(
                    CODE_UNKNOWN_FIELD,
                    f"{path}.{key}",
                    (
                        f"unknown field {key!r}; the manifest schema is closed, "
                        f"permitted fields here are: {', '.join(sorted(permitted))}"
                    ),
                )

    def required_fields(
        self, path: str, block: Mapping[str, Any], required: Iterable[str]
    ) -> None:
        for key in required:
            if block.get(key) in (None, "", [], {}):
                self.add(
                    CODE_MISSING_FIELD,
                    f"{path}.{key}",
                    f"required field {key!r} is missing or empty",
                )

    def mapping(self, path: str, value: Any) -> Dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.add(
                CODE_INVALID_TYPE, path, f"expected an object, got {type(value).__name__}"
            )
            return {}
        return value

    def text(self, path: str, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            self.add(
                CODE_INVALID_TYPE, path, f"expected a string, got {type(value).__name__}"
            )
            return ""
        return value.strip()

    def string_map(self, path: str, value: Any) -> Dict[str, str]:
        block = self.mapping(path, value)
        out: Dict[str, str] = {}
        for key, raw in block.items():
            if not isinstance(key, str) or not _TERM_KEY_RE.match(key):
                self.add(
                    CODE_INVALID_VALUE,
                    f"{path}.{key}",
                    "keys must be lower_snake_case identifiers",
                )
                continue
            text = self.text(f"{path}.{key}", raw)
            if text:
                out[key] = text
        return out


# -- code-shape sweep ---------------------------------------------------------


def _scan_for_code(node: Any, path: str, validator: _Validator) -> None:
    """Refuse code-shaped keys and values anywhere in the document.

    Runs over the RAW document before anything else, so a manifest carrying
    executable content is refused by name even if it is otherwise well-formed —
    2.0-C3's governing constraint is not a field-level concern.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{path}.{key}" if isinstance(key, str) else path
            if isinstance(key, str) and key.strip().lower().replace(
                "-", ""
            ).replace("_", "") in {
                candidate.replace("_", "") for candidate in FORBIDDEN_KEYS
            }:
                validator.add(
                    CODE_EXECUTABLE_CODE_FORBIDDEN,
                    child,
                    (
                        f"field {key!r} is not permitted: a pack manifest is "
                        f"declarative configuration and may not supply, reference, "
                        f"or point at executable code"
                    ),
                )
            _scan_for_code(value, child, validator)
        return
    if isinstance(node, (list, tuple)):
        for index, item in enumerate(node):
            _scan_for_code(item, f"{path}[{index}]", validator)
        return
    if isinstance(node, str):
        for description, pattern in _CODE_PATTERNS:
            if pattern.search(node):
                validator.add(
                    CODE_EXECUTABLE_CODE_FORBIDDEN,
                    path,
                    (
                        f"value looks like executable content ({description}); a "
                        f"pack manifest carries configuration only"
                    ),
                )
                break
        leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]
        if leaf in _IDENTIFIER_FIELDS and _MODULE_PATH_RE.match(node.strip()):
            validator.add(
                CODE_EXECUTABLE_CODE_FORBIDDEN,
                path,
                (
                    f"value {node!r} is a dotted module path; detectors are composed "
                    f"from the primitive library, never imported"
                ),
            )


# -- block validators ---------------------------------------------------------


def _validate_pack_block(
    block: Mapping[str, Any], validator: _Validator
) -> Tuple[str, str, str, str, str, PackAuthor]:
    validator.unknown_fields("pack", block, PACK_FIELDS)
    validator.required_fields("pack", block, REQUIRED_PACK_FIELDS)

    pack_id = validator.text("pack.packId", block.get("packId"))
    if pack_id and not _PACK_ID_RE.match(pack_id):
        validator.add(
            CODE_INVALID_VALUE,
            "pack.packId",
            (
                "packId must be lower_snake_case, 3-64 characters, starting with a "
                "letter"
            ),
        )
    if pack_id and pack_id in PACK_REGISTRY:
        validator.add(
            CODE_RESERVED_PACK_ID,
            "pack.packId",
            (
                f"packId {pack_id!r} is a first-party CloudFulcrum pack; an authored "
                f"pack must choose an id that is not already registered"
            ),
        )

    pack_name = validator.text("pack.packName", block.get("packName"))
    pack_version = validator.text("pack.packVersion", block.get("packVersion"))
    if pack_version and parse_version(pack_version) is None:
        validator.add(
            CODE_INVALID_VALUE,
            "pack.packVersion",
            f"packVersion {pack_version!r} is not a parseable dotted version",
        )

    domain = validator.text("pack.domain", block.get("domain")) or pack_id
    if domain and not _SLUG_RE.match(domain):
        validator.add(
            CODE_INVALID_VALUE,
            "pack.domain",
            "domain must be a lower_snake_case identifier",
        )

    description = validator.text("pack.description", block.get("description"))

    author_block = validator.mapping("pack.author", block.get("author"))
    validator.unknown_fields("pack.author", author_block, AUTHOR_FIELDS)
    validator.required_fields("pack.author", author_block, REQUIRED_AUTHOR_FIELDS)
    author = PackAuthor(
        name=validator.text("pack.author.name", author_block.get("name")),
        contact=validator.text("pack.author.contact", author_block.get("contact")),
        url=validator.text("pack.author.url", author_block.get("url")),
    )
    return pack_id, pack_name, pack_version, domain, description, author


def _validate_concept_list(
    path: str, raw: Any, validator: _Validator
) -> Tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        validator.add(CODE_INVALID_TYPE, path, "expected a list of concept ids")
        return ()
    concepts: List[str] = []
    for index, entry in enumerate(raw):
        item_path = f"{path}[{index}]"
        concept = validator.text(item_path, entry)
        if not concept:
            continue
        if concept in concepts:
            validator.add(
                CODE_DUPLICATE, item_path, f"concept {concept!r} is listed twice"
            )
            continue
        if not is_concept_known(concept):
            validator.add(
                CODE_UNKNOWN_CONCEPT,
                item_path,
                (
                    f"{concept!r} is not a normalised concept this platform "
                    f"provides at any version; a manifest may only reference the "
                    f"declared concept vocabulary"
                ),
            )
            continue
        concepts.append(concept)
    return tuple(concepts)


def _validate_compatibility_block(
    block: Mapping[str, Any], validator: _Validator
) -> Tuple[str, Optional[str], Tuple[str, ...], Tuple[str, ...]]:
    validator.unknown_fields("compatibility", block, COMPATIBILITY_FIELDS)
    validator.required_fields(
        "compatibility", block, REQUIRED_COMPATIBILITY_FIELDS
    )

    minimum = validator.text(
        "compatibility.minPlatformVersion", block.get("minPlatformVersion")
    )
    if minimum and parse_version(minimum) is None:
        validator.add(
            CODE_INVALID_VALUE,
            "compatibility.minPlatformVersion",
            f"{minimum!r} is not a parseable platform version",
        )
    maximum_raw = validator.text(
        "compatibility.maxPlatformVersion", block.get("maxPlatformVersion")
    )
    maximum: Optional[str] = maximum_raw or None
    if maximum and parse_version(maximum) is None:
        validator.add(
            CODE_INVALID_VALUE,
            "compatibility.maxPlatformVersion",
            f"{maximum!r} is not a parseable platform version",
        )
    if minimum and maximum:
        ordering = compare_versions(minimum, maximum)
        if ordering is not None and ordering > 0:
            validator.add(
                CODE_INVALID_VALUE,
                "compatibility.maxPlatformVersion",
                (
                    f"declared range is empty: minimum {minimum} is above maximum "
                    f"{maximum}"
                ),
            )

    required = _validate_concept_list(
        "compatibility.requiredConcepts", block.get("requiredConcepts"), validator
    )
    optional = _validate_concept_list(
        "compatibility.optionalConcepts", block.get("optionalConcepts"), validator
    )
    for concept in optional:
        if concept in required:
            validator.add(
                CODE_DUPLICATE,
                "compatibility.optionalConcepts",
                (
                    f"concept {concept!r} is declared both required and optional; a "
                    f"concept is one or the other"
                ),
            )

    # A floor below a required concept's introduction is self-contradictory: the
    # pack claims to run on a platform that cannot give it what it needs. The
    # first-party registry is held to the same rule by a structural test.
    if minimum and parse_version(minimum) is not None:
        for concept in required:
            spec = get_concept(concept)
            if spec is None:
                continue
            ordering = compare_versions(minimum, spec.since)
            if ordering is not None and ordering < 0:
                validator.add(
                    CODE_CONCEPT_REQUIRES_NEWER_PLATFORM,
                    "compatibility.minPlatformVersion",
                    (
                        f"declared minimum platform version {minimum} is below "
                        f"{spec.since}, which introduced required concept "
                        f"{describe_concept(concept)}"
                    ),
                )
    return minimum, maximum, required, optional


def _validate_certification_block(
    block: Mapping[str, Any], validator: _Validator
) -> Tuple[str, str, str]:
    for reserved in CERTIFICATION_ISSUED_FIELDS:
        if reserved in block:
            validator.add(
                CODE_RESERVED_FIELD,
                f"certification.{reserved}",
                (
                    f"{reserved!r} is issued by CloudFulcrum after review, not "
                    f"declared by a pack author; a manifest may only state "
                    f"'requestedLevel'"
                ),
            )
    # Reserved fields are already reported by name above; excluding them here keeps
    # one error per mistake and stops the "permitted fields" hint from listing the
    # very fields an author may not write.
    validator.unknown_fields(
        "certification",
        {
            key: value
            for key, value in block.items()
            if key not in CERTIFICATION_ISSUED_FIELDS
        },
        CERTIFICATION_FIELDS,
    )

    requested = (
        validator.text(
            "certification.requestedLevel", block.get("requestedLevel")
        ).lower()
        or LEVEL_COMMUNITY
    )
    if requested not in REQUESTABLE_LEVELS:
        validator.add(
            CODE_INVALID_VALUE,
            "certification.requestedLevel",
            (
                f"{requested!r} is not a certification level; one of "
                f"{', '.join(REQUESTABLE_LEVELS)}"
            ),
        )
        requested = LEVEL_COMMUNITY
    return (
        requested,
        validator.text("certification.contact", block.get("contact")),
        validator.text("certification.notes", block.get("notes")),
    )


def _validate_parameter(
    path: str, spec: ParameterSpec, value: Any, validator: _Validator
) -> None:
    if spec.kind == KIND_BOOLEAN:
        if not isinstance(value, bool):
            validator.add(
                CODE_INVALID_TYPE, path, f"parameter {spec.name!r} must be a boolean"
            )
        return
    if spec.kind == KIND_ENUM:
        if not isinstance(value, str) or value not in spec.choices:
            validator.add(
                CODE_INVALID_VALUE,
                path,
                (
                    f"parameter {spec.name!r} must be one of "
                    f"{', '.join(spec.choices)}"
                ),
            )
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        validator.add(
            CODE_INVALID_TYPE, path, f"parameter {spec.name!r} must be a {spec.kind}"
        )
        return
    if spec.kind == KIND_INTEGER and not isinstance(value, int):
        validator.add(
            CODE_INVALID_TYPE, path, f"parameter {spec.name!r} must be an integer"
        )
        return
    if spec.minimum is not None and value < spec.minimum:
        validator.add(
            CODE_PARAMETER_OUT_OF_RANGE,
            path,
            f"parameter {spec.name!r} must be >= {spec.minimum} (got {value})",
        )
    if spec.maximum is not None and value > spec.maximum:
        validator.add(
            CODE_PARAMETER_OUT_OF_RANGE,
            path,
            f"parameter {spec.name!r} must be <= {spec.maximum} (got {value})",
        )


def _validate_detector(
    index: int,
    block: Mapping[str, Any],
    declared_concepts: Tuple[str, ...],
    validator: _Validator,
) -> Optional[DetectorDeclaration]:
    path = f"detectors[{index}]"
    validator.unknown_fields(path, block, DETECTOR_FIELDS)
    validator.required_fields(path, block, REQUIRED_DETECTOR_FIELDS)

    detector_id = validator.text(f"{path}.detectorId", block.get("detectorId"))
    if detector_id and not _DETECTOR_ID_RE.match(detector_id):
        validator.add(
            CODE_INVALID_VALUE,
            f"{path}.detectorId",
            "detectorId must be lower_snake_case, 3-80 characters",
        )
    title = validator.text(f"{path}.title", block.get("title"))

    primitive_id = validator.text(f"{path}.primitive", block.get("primitive"))
    spec: Optional[PrimitiveSpec] = get_primitive(primitive_id) if primitive_id else None
    if primitive_id and spec is None:
        validator.add(
            CODE_UNKNOWN_PRIMITIVE,
            f"{path}.primitive",
            (
                f"{primitive_id!r} is not in the detector primitive library; "
                f"available primitives are: {', '.join(primitive_ids())}"
            ),
        )

    concepts: List[str] = []
    raw_concepts = block.get("concepts")
    if raw_concepts is not None and not isinstance(raw_concepts, (list, tuple)):
        validator.add(
            CODE_INVALID_TYPE, f"{path}.concepts", "expected a list of concept ids"
        )
        raw_concepts = []
    for position, entry in enumerate(raw_concepts or []):
        concept_path = f"{path}.concepts[{position}]"
        concept = validator.text(concept_path, entry)
        if not concept:
            continue
        if not is_concept_known(concept):
            validator.add(
                CODE_UNKNOWN_CONCEPT,
                concept_path,
                (
                    f"{concept!r} is not a normalised concept this platform provides "
                    f"at any version"
                ),
            )
            continue
        if concept not in declared_concepts:
            # Otherwise the compatibility gate would be a lie: the pack would read
            # a concept it never declared, so an incompatible platform could not be
            # detected at activation.
            validator.add(
                CODE_UNDECLARED_CONCEPT,
                concept_path,
                (
                    f"detector reads concept {concept!r}, which is not declared in "
                    f"compatibility.requiredConcepts or optionalConcepts"
                ),
            )
            continue
        if concept in concepts:
            validator.add(
                CODE_DUPLICATE, concept_path, f"concept {concept!r} is listed twice"
            )
            continue
        concepts.append(concept)

    if spec is not None:
        minimum, maximum = spec.concept_arity
        if len(concepts) < minimum or (maximum is not None and len(concepts) > maximum):
            expected = (
                f"exactly {minimum}"
                if maximum == minimum
                else f"at least {minimum}"
                if maximum is None
                else f"{minimum}-{maximum}"
            )
            validator.add(
                CODE_INVALID_VALUE,
                f"{path}.concepts",
                (
                    f"primitive {describe_primitive(spec.primitive_id)} binds "
                    f"{expected} normalised concept(s), got {len(concepts)}"
                ),
            )

    parameters_block = validator.mapping(f"{path}.parameters", block.get("parameters"))
    parameters: Dict[str, Any] = {}
    if spec is not None:
        for name, value in parameters_block.items():
            parameter_path = f"{path}.parameters.{name}"
            parameter_spec = spec.parameter(name)
            if parameter_spec is None:
                validator.add(
                    CODE_UNKNOWN_PARAMETER,
                    parameter_path,
                    (
                        f"primitive {spec.primitive_id!r} has no parameter {name!r}; "
                        f"its parameters are: {', '.join(spec.parameter_names)}"
                    ),
                )
                continue
            _validate_parameter(parameter_path, parameter_spec, value, validator)
            parameters[name] = value
        for parameter_spec in spec.parameters:
            if parameter_spec.required and parameter_spec.name not in parameters_block:
                validator.add(
                    CODE_MISSING_PARAMETER,
                    f"{path}.parameters.{parameter_spec.name}",
                    (
                        f"primitive {spec.primitive_id!r} requires parameter "
                        f"{parameter_spec.name!r}: {parameter_spec.description}"
                    ),
                )

    labels_block = validator.mapping(f"{path}.labels", block.get("labels"))
    labels: Dict[str, str] = {}
    for key, value in labels_block.items():
        label_path = f"{path}.labels.{key}"
        if key not in DETECTOR_LABEL_KEYS:
            validator.add(
                CODE_UNKNOWN_FIELD,
                label_path,
                (
                    f"unknown label {key!r}; permitted labels are: "
                    f"{', '.join(DETECTOR_LABEL_KEYS)}"
                ),
            )
            continue
        text = validator.text(label_path, value)
        if text:
            labels[key] = text

    enabled = block.get("enabledByDefault", True)
    if not isinstance(enabled, bool):
        validator.add(
            CODE_INVALID_TYPE, f"{path}.enabledByDefault", "expected a boolean"
        )
        enabled = True

    if not detector_id or not title or spec is None:
        return None
    return DetectorDeclaration(
        detector_id=detector_id,
        title=title,
        primitive=primitive_id,
        concepts=tuple(concepts),
        parameters=parameters,
        labels=labels,
        enabled_by_default=enabled,
    )


def _validate_scorer_block(
    block: Mapping[str, Any], validator: _Validator
) -> Tuple[Dict[str, float], Dict[str, str], Dict[str, Dict[str, Any]]]:
    validator.unknown_fields("scorerCalibration", block, SCORER_FIELDS)

    weights_block = validator.mapping(
        "scorerCalibration.impactWeights", block.get("impactWeights")
    )
    weights: Dict[str, float] = {}
    for name, value in weights_block.items():
        weight_path = f"scorerCalibration.impactWeights.{name}"
        if name not in IMPACT_DIMENSIONS:
            validator.add(
                CODE_WEIGHTS_INVALID,
                weight_path,
                (
                    f"unknown impact dimension {name!r}; the scoring engine weights "
                    f"{', '.join(IMPACT_DIMENSIONS)}"
                ),
            )
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            validator.add(CODE_INVALID_TYPE, weight_path, "expected a number")
            continue
        if not 0.0 <= float(value) <= 1.0:
            validator.add(
                CODE_WEIGHTS_INVALID, weight_path, "weights are fractions in 0..1"
            )
            continue
        weights[name] = float(value)
    if weights:
        total = sum(weights.values())
        if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
            validator.add(
                CODE_WEIGHTS_INVALID,
                "scorerCalibration.impactWeights",
                (
                    f"impact weights must sum to 1.0 (got {total:.4f}); a partial set "
                    f"would silently rescale the ranking"
                ),
            )

    confidence_block = validator.mapping(
        "scorerCalibration.confidence", block.get("confidence")
    )
    validator.unknown_fields(
        "scorerCalibration.confidence", confidence_block, CONFIDENCE_FIELDS
    )
    confidence: Dict[str, str] = {}
    for name in CONFIDENCE_FIELDS:
        if name not in confidence_block:
            continue
        cap_path = f"scorerCalibration.confidence.{name}"
        level = validator.text(cap_path, confidence_block.get(name)).upper()
        if level not in CONFIDENCE_LEVELS:
            validator.add(
                CODE_INVALID_VALUE,
                cap_path,
                f"expected one of {', '.join(CONFIDENCE_LEVELS)}",
            )
            continue
        ceiling = CONFIDENCE_MAX_ALLOWED[name]
        if _CONFIDENCE_RANK[level] > _CONFIDENCE_RANK[ceiling]:
            validator.add(
                CODE_CONFIDENCE_CEILING,
                cap_path,
                (
                    f"{name} may not exceed {ceiling}: this is a standing platform "
                    f"ceiling a pack can lower but never raise"
                ),
            )
            continue
        confidence[name] = level

    dimensions_block = validator.mapping(
        "scorerCalibration.dimensions", block.get("dimensions")
    )
    dimensions: Dict[str, Dict[str, Any]] = {}
    for name, value in dimensions_block.items():
        dimension_path = f"scorerCalibration.dimensions.{name}"
        if name not in IMPACT_DIMENSIONS:
            validator.add(
                CODE_WEIGHTS_INVALID,
                dimension_path,
                (
                    f"unknown impact dimension {name!r}; the scoring engine calibrates "
                    f"{', '.join(IMPACT_DIMENSIONS)}"
                ),
            )
            continue
        knobs = validator.mapping(dimension_path, value)
        resolved: Dict[str, Any] = {}
        for knob, knob_value in knobs.items():
            knob_path = f"{dimension_path}.{knob}"
            if not isinstance(knob, str) or not _TERM_KEY_RE.match(knob):
                validator.add(
                    CODE_INVALID_VALUE,
                    knob_path,
                    "calibration keys must be lower_snake_case identifiers",
                )
                continue
            if isinstance(knob_value, bool) or isinstance(knob_value, (int, float)):
                resolved[knob] = knob_value
                continue
            validator.add(
                CODE_INVALID_TYPE,
                knob_path,
                "calibration values are numbers or booleans — calibration is data",
            )
        if resolved:
            dimensions[name] = resolved
    return weights, confidence, dimensions


def _validate_terminology_block(
    block: Mapping[str, Any], validator: _Validator
) -> Tuple[Dict[str, str], Dict[str, str], str]:
    validator.unknown_fields("terminology", block, TERMINOLOGY_FIELDS)
    glossary = validator.string_map("terminology.glossary", block.get("glossary"))
    language_map = validator.string_map(
        "terminology.languageMap", block.get("languageMap")
    )
    llm_context = validator.text("terminology.llmContext", block.get("llmContext"))
    return glossary, language_map, llm_context


def _validate_template_block(
    block: Mapping[str, Any], validator: _Validator
) -> Dict[str, Any]:
    validator.unknown_fields("templateDefaults", block, TEMPLATE_FIELDS)
    defaults: Dict[str, Any] = {}

    industry = validator.text("templateDefaults.industry", block.get("industry"))
    if industry:
        if not _SLUG_RE.match(industry):
            validator.add(
                CODE_INVALID_VALUE,
                "templateDefaults.industry",
                "industry must be a lower_snake_case registry id",
            )
        else:
            defaults["industry"] = industry

    for key in ("systems", "recommendedSystems"):
        raw = block.get(key)
        if raw is None:
            continue
        if not isinstance(raw, (list, tuple)):
            validator.add(
                CODE_INVALID_TYPE,
                f"templateDefaults.{key}",
                "expected a list of connector ids",
            )
            continue
        systems: List[str] = []
        for index, entry in enumerate(raw):
            entry_path = f"templateDefaults.{key}[{index}]"
            system = validator.text(entry_path, entry)
            if not system:
                continue
            if not _SLUG_RE.match(system):
                validator.add(
                    CODE_INVALID_VALUE,
                    entry_path,
                    "connector ids are lower_snake_case identifiers",
                )
                continue
            if system in systems:
                validator.add(
                    CODE_DUPLICATE, entry_path, f"system {system!r} is listed twice"
                )
                continue
            systems.append(system)
        if systems:
            defaults[key] = systems

    workflow_focus = validator.text(
        "templateDefaults.workflowFocus", block.get("workflowFocus")
    )
    if workflow_focus:
        defaults["workflowFocus"] = workflow_focus

    roles = validator.string_map("templateDefaults.roles", block.get("roles"))
    if roles:
        defaults["roles"] = roles
    return defaults


# ── Public API ────────────────────────────────────────────────────────────────


def validate_manifest(document: Any) -> ManifestValidation:
    """Validate a manifest document and report EVERY problem found.

    Never raises: the verdict is the return value, so an installer can render all
    failures at once (2.0-C3's "reports specific failures" requirement).
    """
    validator = _Validator()

    if not isinstance(document, dict):
        validator.add(
            CODE_INVALID_TYPE,
            "$",
            f"a pack manifest is a JSON object, got {type(document).__name__}",
        )
        return ManifestValidation(errors=validator.errors)

    # Code-shape sweep first: the governing constraint is not a per-field concern.
    _scan_for_code(document, "$", validator)

    validator.unknown_fields("$", document, TOP_LEVEL_FIELDS)
    validator.required_fields("$", document, REQUIRED_TOP_LEVEL_FIELDS)

    manifest_version = validator.text(
        "$.manifestVersion", document.get("manifestVersion")
    )
    if manifest_version and manifest_version != MANIFEST_VERSION:
        validator.add(
            CODE_INVALID_VALUE,
            "$.manifestVersion",
            (
                f"unsupported manifest schema {manifest_version!r}; this platform "
                f"reads {MANIFEST_VERSION!r}"
            ),
        )

    primitive_library_version = (
        validator.text(
            "$.primitiveLibraryVersion", document.get("primitiveLibraryVersion")
        )
        or PRIMITIVE_LIBRARY_VERSION
    )
    if parse_version(primitive_library_version) is None:
        validator.add(
            CODE_INVALID_VALUE,
            "$.primitiveLibraryVersion",
            f"{primitive_library_version!r} is not a parseable version",
        )
        primitive_library_version = PRIMITIVE_LIBRARY_VERSION
    elif (
        compare_versions(primitive_library_version, PRIMITIVE_LIBRARY_VERSION) or 0
    ) > 0:
        validator.add(
            CODE_INVALID_VALUE,
            "$.primitiveLibraryVersion",
            (
                f"manifest was authored against primitive library "
                f"{primitive_library_version}, but this platform provides "
                f"{PRIMITIVE_LIBRARY_VERSION}"
            ),
        )

    pack_block = validator.mapping("pack", document.get("pack"))
    (
        pack_id,
        pack_name,
        pack_version,
        domain,
        description,
        author,
    ) = _validate_pack_block(pack_block, validator)

    compatibility_block = validator.mapping(
        "compatibility", document.get("compatibility")
    )
    (
        minimum,
        maximum,
        required_concepts,
        optional_concepts,
    ) = _validate_compatibility_block(compatibility_block, validator)

    certification_block = validator.mapping(
        "certification", document.get("certification")
    )
    (
        requested_level,
        certification_contact,
        certification_notes,
    ) = _validate_certification_block(certification_block, validator)

    declared_concepts = tuple(required_concepts) + tuple(
        concept for concept in optional_concepts if concept not in required_concepts
    )

    raw_detectors = document.get("detectors")
    detectors: List[DetectorDeclaration] = []
    if raw_detectors is not None and not isinstance(raw_detectors, (list, tuple)):
        validator.add(
            CODE_INVALID_TYPE, "detectors", "expected a list of detector declarations"
        )
        raw_detectors = []
    seen_detector_ids: set = set()
    for index, entry in enumerate(raw_detectors or []):
        block = validator.mapping(f"detectors[{index}]", entry)
        if not block:
            continue
        declaration = _validate_detector(index, block, declared_concepts, validator)
        if declaration is None:
            continue
        if declaration.detector_id in seen_detector_ids:
            validator.add(
                CODE_DUPLICATE,
                f"detectors[{index}].detectorId",
                f"detectorId {declaration.detector_id!r} is declared twice",
            )
            continue
        seen_detector_ids.add(declaration.detector_id)
        detectors.append(declaration)

    weights, confidence, dimensions = _validate_scorer_block(
        validator.mapping("scorerCalibration", document.get("scorerCalibration")),
        validator,
    )
    glossary, language_map, llm_context = _validate_terminology_block(
        validator.mapping("terminology", document.get("terminology")), validator
    )
    template_defaults = _validate_template_block(
        validator.mapping("templateDefaults", document.get("templateDefaults")),
        validator,
    )

    if validator.errors:
        return ManifestValidation(errors=validator.errors)

    manifest = PackManifest(
        manifest_version=manifest_version,
        pack_id=pack_id,
        pack_name=pack_name,
        pack_version=pack_version,
        domain=domain,
        description=description,
        author=author,
        min_platform_version=minimum,
        max_platform_version=maximum,
        required_concepts=tuple(required_concepts),
        optional_concepts=tuple(optional_concepts),
        detectors=tuple(detectors),
        requested_certification_level=requested_level,
        certification_contact=certification_contact,
        certification_notes=certification_notes,
        impact_weights=weights,
        confidence_caps=confidence,
        scorer_dimensions=dimensions,
        glossary=glossary,
        language_map=language_map,
        llm_context=llm_context,
        template_defaults=template_defaults,
        primitive_library_version=primitive_library_version,
    )
    return ManifestValidation(errors=[], manifest=manifest)


def parse_manifest(document: Any) -> PackManifest:
    """Validate and return the manifest, raising :class:`ManifestValidationError`."""
    result = validate_manifest(document)
    if not result.ok or result.manifest is None:
        raise ManifestValidationError(result.errors)
    return result.manifest


def load_manifest_document(path: Any) -> Any:
    """Read a manifest JSON file, raising :class:`ManifestValidationError` if the
    file is unreadable or is not JSON.

    A malformed file is a manifest failure, not a stack trace: an installer must be
    able to report it in the same shape as every other validation error.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestValidationError(
            [
                ManifestError(
                    CODE_INVALID_VALUE,
                    "$",
                    f"manifest file could not be read: {exc.strerror or exc}",
                )
            ]
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(
            [
                ManifestError(
                    CODE_INVALID_VALUE,
                    "$",
                    f"manifest file is not valid JSON: {exc.msg} (line {exc.lineno})",
                )
            ]
        ) from exc


def load_manifest(path: Any) -> PackManifest:
    """Read, validate, and return a manifest from a JSON file."""
    return parse_manifest(load_manifest_document(path))


def manifest_fingerprint(manifest: PackManifest) -> str:
    """SHA-256 over the manifest's canonical JSON — its stable content identity.

    Deterministic (sorted keys, no insignificant whitespace, reusing the same
    canonicalisation the certification signature uses), so packaging can bind a
    bundle signature to exactly this manifest and installation can prove the
    document it validated is the document it registers.
    """
    payload = canonical_payload_bytes(manifest.to_dict())
    return hashlib.sha256(payload).hexdigest()


def manifest_to_pack_config(manifest: PackManifest) -> Dict[str, Any]:
    """Project a validated manifest into the ``PACK_REGISTRY`` config shape.

    The bridge between the authoring surface and the running platform: the pack
    lifecycle (compatibility gate, disable/rollback, certification, versioning)
    reads a pack config, so a manifest becomes one rather than acquiring a second,
    parallel code path.

    Two deliberate properties:

    * ``detectors`` is EMPTY and always will be. That field holds importable
      module paths, and an authored pack has none — its detectors live under
      ``manifestDetectors`` as declarations the platform's own primitives execute.
    * ``certification`` is the community default. The requested level is carried
      separately as a request, never as a claim; only a CloudFulcrum signature
      (2.0-C2) can raise it.
    """
    return {
        "packId": manifest.pack_id,
        "packVersion": manifest.pack_version,
        "packName": manifest.pack_name,
        "domain": manifest.domain,
        "pack_domain": manifest.domain,
        "description": manifest.description,
        "compatibility": {
            "minPlatformVersion": manifest.min_platform_version,
            "maxPlatformVersion": manifest.max_platform_version,
            "requiredConcepts": list(manifest.required_concepts),
            "optionalConcepts": list(manifest.optional_concepts),
        },
        "certification": {
            "level": LEVEL_COMMUNITY,
            "certifyingEntity": "",
            "reviewDate": "",
            "reviewedAgainstPlatformVersion": "",
            "scope": {"summary": "", "criteria": []},
            "signature": {"keyId": "", "algorithm": "", "value": ""},
        },
        "detectors": [],
        "manifestDetectors": [
            {
                **declaration.to_dict(),
                "resolvedParameters": declaration.resolved_parameters(),
            }
            for declaration in manifest.detectors
        ],
        "scorerCalibration": {
            "impactWeights": dict(manifest.impact_weights),
            "confidence": dict(manifest.confidence_caps),
            "dimensions": {
                name: dict(values)
                for name, values in manifest.scorer_dimensions.items()
            },
        },
        "terminology": {
            "glossary": dict(manifest.glossary),
            "language_map": dict(manifest.language_map),
        },
        "templateDefaults": dict(manifest.template_defaults),
        "ui_labels_path": None,
        "config_path": None,
        "llm_context": manifest.llm_context,
        "source": {
            "kind": "manifest",
            "manifestVersion": manifest.manifest_version,
            "primitiveLibraryVersion": manifest.primitive_library_version,
            "author": manifest.author.to_dict(),
            "requestedCertificationLevel": manifest.requested_certification_level,
            "fingerprint": manifest_fingerprint(manifest),
        },
    }


def manifest_schema_reference() -> Dict[str, Any]:
    """Machine-readable description of the schema — the authoring toolkit's source.

    Generated from the same constants validation uses, so the reference an author
    reads and the rules an installer enforces cannot drift.
    """
    from .primitives import primitive_catalog

    return {
        "manifestVersion": MANIFEST_VERSION,
        "primitiveLibraryVersion": PRIMITIVE_LIBRARY_VERSION,
        "topLevelFields": list(TOP_LEVEL_FIELDS),
        "requiredTopLevelFields": list(REQUIRED_TOP_LEVEL_FIELDS),
        "blocks": {
            "pack": {
                "fields": list(PACK_FIELDS),
                "required": list(REQUIRED_PACK_FIELDS),
                "author": {
                    "fields": list(AUTHOR_FIELDS),
                    "required": list(REQUIRED_AUTHOR_FIELDS),
                },
            },
            "compatibility": {
                "fields": list(COMPATIBILITY_FIELDS),
                "required": list(REQUIRED_COMPATIBILITY_FIELDS),
            },
            "certification": {
                "fields": list(CERTIFICATION_FIELDS),
                "issuedByCloudFulcrum": list(CERTIFICATION_ISSUED_FIELDS),
                "requestableLevels": list(REQUESTABLE_LEVELS),
            },
            "detectors": {
                "fields": list(DETECTOR_FIELDS),
                "required": list(REQUIRED_DETECTOR_FIELDS),
                "labelKeys": list(DETECTOR_LABEL_KEYS),
            },
            "scorerCalibration": {
                "fields": list(SCORER_FIELDS),
                "impactDimensions": list(IMPACT_DIMENSIONS),
                "confidenceFields": list(CONFIDENCE_FIELDS),
                "confidenceCeilings": dict(CONFIDENCE_MAX_ALLOWED),
            },
            "terminology": {"fields": list(TERMINOLOGY_FIELDS)},
            "templateDefaults": {"fields": list(TEMPLATE_FIELDS)},
        },
        "forbiddenKeys": sorted(FORBIDDEN_KEYS),
        "primitives": primitive_catalog()["primitives"],
    }


__all__ = [
    "CERTIFICATION_ISSUED_FIELDS",
    "CODE_CONCEPT_REQUIRES_NEWER_PLATFORM",
    "CODE_CONFIDENCE_CEILING",
    "CODE_DUPLICATE",
    "CODE_EXECUTABLE_CODE_FORBIDDEN",
    "CODE_INVALID_TYPE",
    "CODE_INVALID_VALUE",
    "CODE_MISSING_FIELD",
    "CODE_MISSING_PARAMETER",
    "CODE_PARAMETER_OUT_OF_RANGE",
    "CODE_RESERVED_FIELD",
    "CODE_RESERVED_PACK_ID",
    "CODE_UNDECLARED_CONCEPT",
    "CODE_UNKNOWN_CONCEPT",
    "CODE_UNKNOWN_FIELD",
    "CODE_UNKNOWN_PARAMETER",
    "CODE_UNKNOWN_PRIMITIVE",
    "CODE_WEIGHTS_INVALID",
    "DetectorDeclaration",
    "IMPACT_DIMENSIONS",
    "MANIFEST_VERSION",
    "ManifestError",
    "ManifestValidation",
    "ManifestValidationError",
    "PackAuthor",
    "PackManifest",
    "load_manifest",
    "load_manifest_document",
    "manifest_fingerprint",
    "manifest_schema_reference",
    "manifest_to_pack_config",
    "parse_manifest",
    "validate_manifest",
]
