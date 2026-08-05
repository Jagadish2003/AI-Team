"""Skills SDK — partner pack authoring surface (2.0-C3).

The public entry point for everything a pack author (and everything that
validates, packages, or installs an authored pack) touches:

* :mod:`.manifest`   — the pack manifest schema, its validator, the canonical
                       fingerprint, and the projection into the platform's pack
                       config shape (2.0-C3 T1 / AT-836).
* :mod:`.primitives` — the detector primitive vocabulary and parameter contracts
                       a manifest composes detectors from.

The governing constraint of this whole package: **a pack is declarative
configuration, never code**. Nothing here loads, imports, or executes
partner-supplied content — validation reads a JSON document and reports on it.

Dependency-free of ``app``, so authoring tooling runs offline.
"""

from __future__ import annotations

from .manifest import (  # noqa: F401
    MANIFEST_VERSION,
    DetectorDeclaration,
    ManifestError,
    ManifestValidation,
    ManifestValidationError,
    PackAuthor,
    PackManifest,
    load_manifest,
    load_manifest_document,
    manifest_fingerprint,
    manifest_schema_reference,
    manifest_to_pack_config,
    parse_manifest,
    validate_manifest,
)
from .primitives import (  # noqa: F401
    PRIMITIVE_LIBRARY,
    PRIMITIVE_LIBRARY_VERSION,
    ParameterSpec,
    PrimitiveSpec,
    get_primitive,
    is_known_primitive,
    primitive_catalog,
    primitive_ids,
)

__all__ = [
    "MANIFEST_VERSION",
    "PRIMITIVE_LIBRARY",
    "PRIMITIVE_LIBRARY_VERSION",
    "DetectorDeclaration",
    "ManifestError",
    "ManifestValidation",
    "ManifestValidationError",
    "PackAuthor",
    "PackManifest",
    "ParameterSpec",
    "PrimitiveSpec",
    "get_primitive",
    "is_known_primitive",
    "load_manifest",
    "load_manifest_document",
    "manifest_fingerprint",
    "manifest_schema_reference",
    "manifest_to_pack_config",
    "parse_manifest",
    "primitive_catalog",
    "primitive_ids",
    "validate_manifest",
]
