"""Skills SDK — partner pack authoring surface (2.0-C3).

The public entry point for everything a pack author (and everything that
validates, packages, or installs an authored pack) touches:

* :mod:`.manifest`          — the pack manifest schema, its validator, the
                              canonical fingerprint, and the projection into the
                              platform's pack config shape (T1 / AT-836).
* :mod:`.primitives`        — the detector primitive vocabulary, parameter
                              contracts, and concept arity (T1 / AT-836).
* :mod:`.primitive_library` — the RUNNABLE half of those primitives (T2 /
                              AT-837): one implementation per declared id.
* :mod:`.signals`           — the normalised-concept records a primitive reads,
                              individual-free at admission (T2 / AT-837).
* :mod:`.contract`          — the four-part finding contract, INHERITED from the
                              operational pack scaffold, plus the confidence and
                              corroboration derivation an author cannot override.
* :mod:`.execution`         — running a manifest's detectors over signal, with
                              the contract enforced at the pack boundary.
* :mod:`.scaffold`          — generating a working pack project locally
                              (T3 / AT-838).
* :mod:`.harness`           — the fixture-based test harness: seeded signal in,
                              asserted findings out (T3 / AT-838).
* :mod:`.lint`              — the platform's non-negotiables, checked at
                              authoring time (T3 / AT-838).
* :mod:`.toolkit`           — validate + test + lint as ONE check, the same code
                              path installation runs before activation.

The CLI over all of it is ``backend/scripts/pack_sdk.py``.

The governing constraint of this whole package: **a pack is declarative
configuration, never code**. Nothing here loads, imports, or executes
partner-supplied content — validation reads a JSON document, and execution runs
the PLATFORM's primitives against parameters that document supplied.

Dependency-free of ``app``, so authoring tooling runs offline. The single
exception is ``execution.to_detector_results``, the adapter into the run
pipeline, which imports ``discovery.models`` lazily inside the function.
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
from .contract import (  # noqa: F401
    PackContractViolation,
    build_pack_contract,
    derive_confidence,
    enforce_pack_contract,
)
from .execution import (  # noqa: F401
    DetectorOutcome,
    PackExecutionResult,
    run_detector,
    run_manifest,
    to_detector_results,
)
from .primitive_library import (  # noqa: F401
    PRIMITIVE_IMPLEMENTATIONS,
    PrimitiveContext,
    PrimitiveExecutionError,
    PrimitiveFinding,
    implemented_primitive_ids,
    run_primitive,
)
from .signals import (  # noqa: F401
    ConceptRecord,
    SignalError,
    SignalSet,
    concept_record,
    records_from_dicts,
    signal_set,
    signal_set_from_dicts,
)
from .harness import (  # noqa: F401
    FIXTURES_DIRNAME,
    PACK_MANIFEST_FILENAME,
    CaseResult,
    HarnessError,
    HarnessResult,
    load_cases,
    run_case,
    run_cases,
    run_pack_directory,
    validate_case,
)
from .lint import (  # noqa: F401
    LintFinding,
    LintReport,
    lint_pack,
    lint_rule_reference,
)
from .scaffold import (  # noqa: F401
    ScaffoldError,
    ScaffoldResult,
    scaffold_pack,
)
from .toolkit import (  # noqa: F401
    PackCheckReport,
    check_manifest_document,
    check_pack_directory,
)

__all__ = [
    "FIXTURES_DIRNAME",
    "MANIFEST_VERSION",
    "PACK_MANIFEST_FILENAME",
    "PRIMITIVE_IMPLEMENTATIONS",
    "PRIMITIVE_LIBRARY",
    "PRIMITIVE_LIBRARY_VERSION",
    "CaseResult",
    "ConceptRecord",
    "DetectorDeclaration",
    "DetectorOutcome",
    "HarnessError",
    "HarnessResult",
    "LintFinding",
    "LintReport",
    "PackCheckReport",
    "ScaffoldError",
    "ScaffoldResult",
    "check_manifest_document",
    "check_pack_directory",
    "lint_pack",
    "lint_rule_reference",
    "load_cases",
    "run_case",
    "run_cases",
    "run_pack_directory",
    "scaffold_pack",
    "validate_case",
    "ManifestError",
    "ManifestValidation",
    "ManifestValidationError",
    "PackAuthor",
    "PackContractViolation",
    "PackExecutionResult",
    "PackManifest",
    "ParameterSpec",
    "PrimitiveContext",
    "PrimitiveExecutionError",
    "PrimitiveFinding",
    "PrimitiveSpec",
    "SignalError",
    "SignalSet",
    "build_pack_contract",
    "concept_record",
    "derive_confidence",
    "enforce_pack_contract",
    "get_primitive",
    "implemented_primitive_ids",
    "is_known_primitive",
    "load_manifest",
    "load_manifest_document",
    "manifest_fingerprint",
    "manifest_schema_reference",
    "manifest_to_pack_config",
    "parse_manifest",
    "primitive_catalog",
    "primitive_ids",
    "records_from_dicts",
    "run_detector",
    "run_manifest",
    "run_primitive",
    "signal_set",
    "signal_set_from_dicts",
    "to_detector_results",
    "validate_manifest",
]
