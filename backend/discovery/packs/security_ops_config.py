"""
security_ops_config.py — MSP-B12 T1: Security Operations Discovery Pack config schema.

The Security Operations pack is the second sibling of the Cloud-Operations pack
(MSP-B6) on the same configuration-driven template model. Like cloud-ops, it keeps
its calibration values, detector thresholds, and SecOps terminology in an EXTERNAL
config file (``security_ops_pack_config.json``) rather than in code, so a config
change alters pack behaviour with no code deploy. This module is the typed SCHEMA +
loader that turns that JSON into validated config objects the later tasks read:

  * T2 detectors read ``thresholds``.
  * T6 SecOps ops-impact scorer reads ``calibration`` (severity-band weighting).
  * Findings / roadmap / report rendering read ``terminology``.

Design deliberately mirrors ``cloud_ops_config.py`` (frozen dataclasses + module-
level accessors that read the same way) so the two operational packs share one
shape — the whole point of B12 being "B6's pattern applied to a second domain".
The one difference from a Python-literal registry is that the VALUES come from
JSON on disk.

This module contains NO detector or scorer logic. It only loads, shapes, and
validates configuration.

Public API:
  load_security_ops_config(path=None) -> SecurityOpsPackConfig
  get_terminology(path=None) -> SecurityOpsTerminology
  get_thresholds(path=None) -> Dict[str, Any]
  get_detector_thresholds(section, fallback=None, path=None) -> Dict[str, Any]
  get_calibration(path=None) -> SecurityOpsCalibration
  get_secops_term(term, path=None) -> str
  DEFAULT_CONFIG_PATH, REQUIRED_SECOPS_TERMS
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default location of the externalized pack config, alongside the other pack
# config/label files in this directory.
DEFAULT_CONFIG_PATH = str(Path(__file__).parent / "security_ops_pack_config.json")

# The SecOps terminology set this pack must externalize (MSP-B12 §2). Every one of
# these terms must be present in the config glossary or the config is rejected at
# load time — a scaffold that silently dropped a term would let a later task render
# generic service-management language and pass unnoticed. Keys are the normalized
# (lowercase, underscore) forms of: remediation, scan cycle, deferral, SLA, triage,
# severity band, security queue, CI class.
REQUIRED_SECOPS_TERMS = (
    "remediation",
    "scan_cycle",
    "deferral",
    "sla",
    "triage",
    "severity_band",
    "security_queue",
    "ci_class",
)


class SecurityOpsConfigError(ValueError):
    """Raised when the externalized Security-Operations pack config is missing or malformed."""


# ── Schema ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SecurityOpsTerminology:
    """The externalized SecOps vocabulary (MSP-B12 §2).

    glossary     — canonical SecOps term -> its definition (covers REQUIRED_SECOPS_TERMS).
    language_map — generic AgentIQ / service-management term -> SecOps term, the same
                   re-labelling mechanism the lending and cloud-ops packs use.
    """
    glossary: Dict[str, str] = field(default_factory=dict)
    language_map: Dict[str, str] = field(default_factory=dict)

    def term(self, name: str) -> str:
        """Return the definition of a SecOps term (case-insensitive), or '' if absent."""
        return self.glossary.get(name.lower(), "")


@dataclass(frozen=True)
class SecurityOpsCalibration:
    """SecOps ops-impact scorer calibration (read by MSP-B12 T6; no scoring logic here).

    impact_weights — relative ranking weights across the scorer's dimensions
                     (effort_concentration, breadth, recurrence_stability,
                     severity_band).
    severity_band  — per-band weighting so critical-band toil ranks above
                     informational-band toil at equal effort (T6 AC6).
    confidence     — the honest-confidence caps enforced at the pack boundary.
    """
    impact_weights: Dict[str, float] = field(default_factory=dict)
    severity_band: Dict[str, float] = field(default_factory=dict)
    severity_default: str = ""
    recurrence_stability: Dict[str, float] = field(default_factory=dict)
    normalization: Dict[str, float] = field(default_factory=dict)
    score_tiers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    confidence_defaults: Dict[str, str] = field(default_factory=dict)
    roadmap_stages: Dict[str, str] = field(default_factory=dict)
    presentation: Dict[str, Any] = field(default_factory=dict)
    confidence: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SecurityOpsPackConfig:
    """Fully-loaded, validated Security-Operations pack configuration."""
    pack_version: str
    terminology: SecurityOpsTerminology
    thresholds: Dict[str, Any]
    calibration: SecurityOpsCalibration
    source_path: str


# ── Loader (cached by path + mtime so config edits apply with no restart) ───────

# A config change must alter behaviour with no code deploy. We cache by
# (path, mtime) so repeated reads are cheap, but an edit to the file (new mtime)
# transparently invalidates the cache and is picked up on the next call.
_CACHE: Dict[str, Any] = {}


def _numeric_map(raw: Any) -> Dict[str, float]:
    """Coerce a config sub-block into a {str: float} map, dropping non-numeric values.

    Used for the scorer's numeric calibration blocks (severity_band). Documentation-
    only keys (already stripped by ``_strip_meta``) and any stray non-numeric value
    are ignored so a malformed entry never crashes the loader.
    """
    out: Dict[str, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[str(k)] = float(v)
    return out


def _strip_meta(obj: Any) -> Any:
    """Drop documentation-only keys (``_meta``/``_note``) from a loaded dict tree."""
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def load_security_ops_config(path: Optional[str] = None) -> SecurityOpsPackConfig:
    """Load, validate, and return the Security-Operations pack config.

    Raises SecurityOpsConfigError if the file is missing, unparseable, or does not
    cover the required SecOps terminology set (MSP-B12 §2).
    """
    cfg_path = path or DEFAULT_CONFIG_PATH

    try:
        mtime = os.path.getmtime(cfg_path)
    except OSError as exc:
        raise SecurityOpsConfigError(
            f"Security-Operations pack config not found at {cfg_path!r}: {exc}"
        ) from exc

    cached = _CACHE.get(cfg_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SecurityOpsConfigError(
            f"Security-Operations pack config at {cfg_path!r} could not be parsed: {exc}"
        ) from exc

    data = _strip_meta(raw)

    terminology_raw = data.get("terminology", {}) or {}
    glossary = {str(k).lower(): str(v) for k, v in (terminology_raw.get("glossary", {}) or {}).items()}

    missing = [t for t in REQUIRED_SECOPS_TERMS if t not in glossary]
    if missing:
        raise SecurityOpsConfigError(
            f"Security-Operations terminology config at {cfg_path!r} is missing required "
            f"SecOps term(s): {missing}. Required set: {list(REQUIRED_SECOPS_TERMS)}."
        )

    terminology = SecurityOpsTerminology(
        glossary=glossary,
        language_map=dict(terminology_raw.get("language_map", {}) or {}),
    )

    calibration_raw = data.get("calibration", {}) or {}
    calibration = SecurityOpsCalibration(
        impact_weights={
            str(k): float(v) for k, v in (calibration_raw.get("impact_weights", {}) or {}).items()
        },
        severity_band=_numeric_map(calibration_raw.get("severity_band", {})),
        severity_default=str(
            (calibration_raw.get("severity_band", {}) or {}).get("default_band", "")
        ).strip().lower(),
        recurrence_stability=_numeric_map(calibration_raw.get("recurrence_stability", {})),
        normalization=_numeric_map(calibration_raw.get("normalization", {})),
        score_tiers={
            str(k): dict(v) for k, v in (calibration_raw.get("score_tiers", {}) or {}).items()
            if isinstance(v, dict)
        },
        confidence_defaults={
            str(k): str(v) for k, v in (calibration_raw.get("confidence_defaults", {}) or {}).items()
        },
        roadmap_stages={
            str(k): str(v) for k, v in (calibration_raw.get("roadmap_stages", {}) or {}).items()
        },
        presentation=dict(calibration_raw.get("presentation", {}) or {}),
        confidence=dict(calibration_raw.get("confidence", {}) or {}),
    )

    config = SecurityOpsPackConfig(
        pack_version=str(data.get("packVersion", "")),
        terminology=terminology,
        thresholds=dict(data.get("thresholds", {}) or {}),
        calibration=calibration,
        source_path=cfg_path,
    )

    _CACHE[cfg_path] = (mtime, config)
    return config


# ── Convenience accessors ───────────────────────────────────────────────────────


def get_terminology(path: Optional[str] = None) -> SecurityOpsTerminology:
    """Return the externalized SecOps terminology set."""
    return load_security_ops_config(path).terminology


def get_thresholds(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the externalized detector thresholds (read by T2 detectors)."""
    return dict(load_security_ops_config(path).thresholds)


def get_detector_thresholds(
    section: str,
    fallback: Optional[Dict[str, Any]] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one detector's threshold block, config-driven with a safe fallback.

    Reads ``thresholds[section]`` from the external config. If the config is
    missing/unreadable, returns ``fallback`` so a detector degrades to its
    documented defaults rather than failing a run — the config edit still wins when
    the file is present.
    """
    base = dict(fallback or {})
    try:
        section_cfg = load_security_ops_config(path).thresholds.get(section, {}) or {}
    except SecurityOpsConfigError as exc:  # pragma: no cover - defensive
        logger.warning(
            "security_ops thresholds for %r unavailable (%s); using defaults %s",
            section, exc, base,
        )
        return base
    base.update({k: v for k, v in section_cfg.items() if not str(k).startswith("_")})
    return base


def get_calibration(path: Optional[str] = None) -> SecurityOpsCalibration:
    """Return the externalized scorer calibration (read by the T6 ops-impact scorer)."""
    return load_security_ops_config(path).calibration


def get_secops_term(term: str, path: Optional[str] = None) -> str:
    """Return the definition of a single SecOps term, or '' if not defined."""
    return get_terminology(path).term(term)
