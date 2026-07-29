"""financial_services_cloud_config.py — 2.0-D1 T2: externalized FSC pack config.

Loads ``financial_services_cloud_pack_config.json``: detector firing thresholds,
the AC5 aggregation floor, FSC terminology, and scorer calibration. Mirrors
``cloud_ops_config.py`` exactly — same cache-by-(path, mtime) behaviour, same
"config edit alters behaviour with no code deploy" contract, same
``get_detector_thresholds`` degrade-to-defaults posture — so this is a second
instance of an existing mechanism, not new pack machinery.

Why the thresholds live here rather than in detector bodies (2.0-D1 T2):
"Calibrated for FSC" means firing boundaries, and those numbers cannot be derived
from first principles — there is no measured FSC dataset. They are therefore
shipped as NAMED, EXTERNALLY VISIBLE, EXPLICITLY PROVISIONAL values so replacing
them later is a config edit. ``calibration_status()`` and ``is_provisional()``
make that provisional posture readable at runtime rather than only in a comment,
so nothing downstream can mistake these numbers for measured ones.

No detector logic lives here.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = str(
    Path(__file__).parent / "financial_services_cloud_pack_config.json"
)

# The FSC terminology set the config must cover. These keys are ALSO the
# `_terminology` keys in financial_services_cloud_ui_labels.json (T1) — a contract
# test pins the two together so the machine-readable glossary and the user-visible
# label file cannot drift.
REQUIRED_FSC_TERMS = (
    "household",
    "relationship_group",
    "financial_account",
    "service_process",
    "referral",
    "service_queue",
    "review_cycle",
)

# Detector threshold sections the config must declare — one per FSC detector.
REQUIRED_THRESHOLD_SECTIONS = (
    "servicing_request_recurrence",
    "referral_handoff_friction",
    "approval_review_cycle",
    "service_queue_ageing",
    "cross_object_rework",
)


class FscConfigError(ValueError):
    """The FSC pack config is missing, unparseable, or incomplete."""


@dataclass(frozen=True)
class FscTerminology:
    glossary: Dict[str, str] = field(default_factory=dict)
    language_map: Dict[str, str] = field(default_factory=dict)

    def term(self, name: str) -> str:
        """Case-insensitive lookup; '' when the term is not defined."""
        return self.glossary.get(str(name).strip().lower(), "")


@dataclass(frozen=True)
class FscAggregation:
    """The AC5 aggregation floor, as config.

    ``permitted_units`` are the only things a finding may be ABOUT.
    ``forbidden_units`` are person-level concepts that must never be a unit.
    ``emit_household_names`` is False: a household name identifies a family and,
    for a single-member household, an individual — so detectors emit household
    COUNTS plus opaque record-id pointers, never names.
    """
    permitted_units: List[str] = field(default_factory=list)
    forbidden_units: List[str] = field(default_factory=list)
    emit_household_names: bool = False
    min_household_size: int = 2
    household_reference_form: str = "record_id_only"


@dataclass(frozen=True)
class FscScope:
    """The org-configurable record types and picklist values the ingest branches on.

    Salesforce record-type DeveloperNames and Case status picklists are defined per
    org, so these cannot be derived — confirming a customer's real values needs
    records from their FSC org. Declaring them here means a differing org is a
    config edit, not a code change.
    """
    service_process_record_types: List[str] = field(default_factory=list)
    household_record_types: List[str] = field(default_factory=list)
    closed_statuses: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class FscCalibration:
    """Scorer calibration (2.0-D1 T3).

    ``impact_weights`` are the relative ranking weights across the three dimensions
    D1 names. ``effort_concentration`` / ``breadth`` / ``automation_shape`` carry the
    per-dimension detail the scorer derives each dimension score from, so a value
    edit alters ranked order with no code deploy.

    ``confidence`` documents the honest-confidence posture the T2 detectors already
    apply — the scorer NEVER recomputes confidence from it.

    Per-detector BASE scores are deliberately NOT here: they live in ``_FSC_SCORES``
    in the scorer module, following the ``_LENDING_SCORES`` convention every pack
    scorer on this branch uses, each value carrying inline provenance.
    """
    confidence: Dict[str, Any] = field(default_factory=dict)
    impact_weights: Dict[str, float] = field(default_factory=dict)
    effort_concentration: Dict[str, Any] = field(default_factory=dict)
    breadth: Dict[str, Any] = field(default_factory=dict)
    automation_shape: Dict[str, Any] = field(default_factory=dict)
    calibration_status: str = ""

    def automation_shape_for(self, detector_id: str) -> Optional[float]:
        """Configured automation shape for a detector, or None when unset."""
        by_detector = self.automation_shape.get("by_detector") or {}
        value = by_detector.get(str(detector_id))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        return None

    def is_provisional(self) -> bool:
        return "PROVISIONAL" in str(self.calibration_status).upper()


@dataclass(frozen=True)
class FscPackConfig:
    """Fully-loaded, validated Financial Services Cloud pack configuration."""
    pack_version: str
    terminology: FscTerminology
    aggregation: FscAggregation
    scope: FscScope
    thresholds: Dict[str, Any]
    calibration: FscCalibration
    calibration_status: str
    source_path: str


# Cache by (path, mtime): repeated reads are cheap, but editing the file
# transparently invalidates the cache — a config change needs no restart.
_CACHE: Dict[str, Any] = {}


def _strip_meta(obj: Any) -> Any:
    """Drop documentation-only keys (``_meta``/``_note``/``_basis``) from the tree."""
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def load_fsc_config(path: Optional[str] = None) -> FscPackConfig:
    """Load, validate, and return the FSC pack config.

    Raises :class:`FscConfigError` when the file is missing, unparseable, does not
    cover :data:`REQUIRED_FSC_TERMS`, or omits a required threshold section — a
    config that silently dropped a detector's thresholds would leave that detector
    running on hardcoded defaults, which is exactly what externalising them is
    meant to prevent.
    """
    cfg_path = path or DEFAULT_CONFIG_PATH

    try:
        mtime = os.path.getmtime(cfg_path)
    except OSError as exc:
        raise FscConfigError(
            f"FSC pack config not found at {cfg_path!r}: {exc}"
        ) from exc

    cached = _CACHE.get(cfg_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise FscConfigError(
            f"FSC pack config at {cfg_path!r} could not be parsed: {exc}"
        ) from exc

    # Read the provisional marker BEFORE stripping documentation keys — the
    # honesty flag lives in _meta and must survive into the loaded config.
    calibration_status = str(
        (raw.get("_meta", {}) or {}).get("calibration_status", "") or ""
    )

    data = _strip_meta(raw)

    terminology_raw = data.get("terminology", {}) or {}
    glossary = {
        str(k).lower(): str(v)
        for k, v in (terminology_raw.get("glossary", {}) or {}).items()
    }
    missing_terms = [t for t in REQUIRED_FSC_TERMS if t not in glossary]
    if missing_terms:
        raise FscConfigError(
            f"FSC terminology config at {cfg_path!r} is missing required term(s): "
            f"{missing_terms}. Required set: {list(REQUIRED_FSC_TERMS)}."
        )

    terminology = FscTerminology(
        glossary=glossary,
        language_map=dict(terminology_raw.get("language_map", {}) or {}),
    )

    thresholds = dict(data.get("thresholds", {}) or {})
    missing_sections = [s for s in REQUIRED_THRESHOLD_SECTIONS if s not in thresholds]
    if missing_sections:
        raise FscConfigError(
            f"FSC pack config at {cfg_path!r} is missing threshold section(s): "
            f"{missing_sections}. Required: {list(REQUIRED_THRESHOLD_SECTIONS)}."
        )

    aggregation_raw = data.get("aggregation", {}) or {}
    aggregation = FscAggregation(
        permitted_units=[str(u) for u in (aggregation_raw.get("permitted_units") or [])],
        forbidden_units=[str(u) for u in (aggregation_raw.get("forbidden_units") or [])],
        emit_household_names=bool(aggregation_raw.get("emit_household_names", False)),
        min_household_size=int(aggregation_raw.get("min_household_size", 2) or 2),
        household_reference_form=str(
            aggregation_raw.get("household_reference_form", "record_id_only")
        ),
    )

    scope_raw = data.get("scope", {}) or {}
    scope = FscScope(
        service_process_record_types=[
            str(x) for x in (scope_raw.get("service_process_record_types") or [])
        ],
        household_record_types=[
            str(x) for x in (scope_raw.get("household_record_types") or [])
        ],
        closed_statuses=[
            str(x).lower() for x in (scope_raw.get("closed_statuses") or [])
        ],
    )

    # The calibration block's own provisional marker, read from the RAW tree so it
    # survives _strip_meta (which drops keys beginning with "_"). Falls back to the
    # file-level marker when the block does not carry its own.
    calibration_status_raw = str(
        (raw.get("calibration", {}) or {}).get("calibration_status", "")
        or calibration_status
    )

    calibration_raw = data.get("calibration", {}) or {}
    calibration = FscCalibration(
        confidence=dict(calibration_raw.get("confidence", {}) or {}),
        impact_weights={
            str(k): float(v)
            for k, v in (calibration_raw.get("impact_weights", {}) or {}).items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        },
        effort_concentration=dict(calibration_raw.get("effort_concentration", {}) or {}),
        breadth=dict(calibration_raw.get("breadth", {}) or {}),
        automation_shape=dict(calibration_raw.get("automation_shape", {}) or {}),
        calibration_status=calibration_status_raw,
    )

    config = FscPackConfig(
        pack_version=str(data.get("packVersion", "")),
        terminology=terminology,
        aggregation=aggregation,
        scope=scope,
        thresholds=thresholds,
        calibration=calibration,
        calibration_status=calibration_status,
        source_path=cfg_path,
    )

    _CACHE[cfg_path] = (mtime, config)
    return config


# ── Convenience accessors ───────────────────────────────────────────────────────


def get_terminology(path: Optional[str] = None) -> FscTerminology:
    """Return the externalized FSC terminology set."""
    return load_fsc_config(path).terminology


def get_fsc_term(term: str, path: Optional[str] = None) -> str:
    """Return the definition of a single FSC term, or '' if not defined."""
    return get_terminology(path).term(term)


def get_aggregation(path: Optional[str] = None) -> FscAggregation:
    """Return the AC5 aggregation floor (read by fsc_finding.py)."""
    return load_fsc_config(path).aggregation


def get_scope(path: Optional[str] = None) -> FscScope:
    """Return the org-configurable record types / picklist values (read by the ingest)."""
    return load_fsc_config(path).scope


def get_thresholds(path: Optional[str] = None) -> Dict[str, Any]:
    """Return every externalized detector threshold block."""
    return dict(load_fsc_config(path).thresholds)


def get_detector_thresholds(
    section: str,
    fallback: Optional[Dict[str, Any]] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one detector's threshold block, config-driven with a safe fallback.

    If the config is missing/unreadable the detector degrades to its documented
    module defaults rather than failing the run; when the file is present the
    config wins. Documentation keys (``_basis``) are never returned as thresholds.
    """
    base = dict(fallback or {})
    try:
        section_cfg = load_fsc_config(path).thresholds.get(section, {}) or {}
    except FscConfigError as exc:  # pragma: no cover - defensive
        logger.warning(
            "financial_services_cloud thresholds for %r unavailable (%s); using "
            "defaults %s", section, exc, base,
        )
        return base
    base.update(
        {k: v for k, v in section_cfg.items() if not str(k).startswith("_")}
    )
    return base


def get_calibration(path: Optional[str] = None) -> FscCalibration:
    """Return the externalized scorer calibration (2.0-D1 T3 populates the weights)."""
    return load_fsc_config(path).calibration


def calibration_status(path: Optional[str] = None) -> str:
    """Return the config's self-declared calibration posture.

    Non-empty and containing 'PROVISIONAL' while the thresholds remain
    engineering placeholders. Readable at runtime so no downstream consumer can
    mistake these numbers for measured ones.
    """
    return load_fsc_config(path).calibration_status


def is_provisional(path: Optional[str] = None) -> bool:
    """True while the FSC firing thresholds are unmeasured placeholders."""
    return "PROVISIONAL" in calibration_status(path).upper()
