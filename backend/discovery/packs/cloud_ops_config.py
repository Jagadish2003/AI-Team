"""
cloud_ops_config.py — MSP-B6 T1 (AT-736): Cloud-Operations Discovery Pack config schema.

The Cloud-Operations pack keeps its calibration values, detector thresholds, and
NOC terminology in an EXTERNAL config file (``cloud_ops_pack_config.json``) rather
than in code, so a config change alters pack behaviour with no code deploy
(MSP-B6 T1 AC2). This module is the typed SCHEMA + loader that turns that JSON into
validated config objects the later tasks read:

  * T2/T3 detectors read ``thresholds``.
  * T4 ops-impact scorer reads ``calibration``.
  * T5 template + findings/roadmap/report rendering read ``terminology``.

Design mirrors the existing registry modules (``industry_registry.py`` /
``template_registry.py``): frozen dataclasses + module-level accessors that read
the same way. The one difference is that the VALUES come from JSON on disk, not a
Python literal — that is the whole point of AC2.

This module contains NO detector or scorer logic (MSP-B6 T1 AC4). It only loads,
shapes, and validates configuration.

Public API:
  load_cloud_ops_config(path=None) -> CloudOpsPackConfig
  get_terminology(path=None) -> CloudOpsTerminology
  get_thresholds(path=None) -> Dict[str, Any]
  get_calibration(path=None) -> CloudOpsCalibration
  get_noc_term(term, path=None) -> str
  DEFAULT_CONFIG_PATH, REQUIRED_NOC_TERMS
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .pack_version_context import resolve_config_path

logger = logging.getLogger(__name__)

#: Registry id this loader serves — the key a per-run version pin is published under.
PACK_ID = "cloud_ops"

# Default location of the externalized pack config, alongside the other pack
# config/label files in this directory.
DEFAULT_CONFIG_PATH = str(Path(__file__).parent / "cloud_ops_pack_config.json")

# The NOC terminology set this pack must externalize (MSP-B6 scope §2 / T1 AC3).
# Every one of these terms must be present in the config glossary or the config
# is rejected at load time — a scaffold that silently dropped a term would let a
# later task render generic language and pass unnoticed.
REQUIRED_NOC_TERMS = (
    "alerts",
    "incidents",
    "runbooks",
    "mttr",
    "toil",
    "escalation",
)


class CloudOpsConfigError(ValueError):
    """Raised when the externalized Cloud-Operations pack config is missing or malformed."""


# ── Schema ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CloudOpsTerminology:
    """The externalized NOC vocabulary (MSP-B6 T1 AC3).

    glossary    — canonical NOC term -> its definition (covers REQUIRED_NOC_TERMS).
    language_map — generic AgentIQ term -> NOC term, the same re-labelling mechanism
                   the lending pack uses for borrower/covenant language.
    """
    glossary: Dict[str, str] = field(default_factory=dict)
    language_map: Dict[str, str] = field(default_factory=dict)

    def term(self, name: str) -> str:
        """Return the definition of a NOC term (case-insensitive), or '' if absent."""
        return self.glossary.get(name.lower(), "")


@dataclass(frozen=True)
class CloudOpsCalibration:
    """Ops-impact scorer calibration (read by MSP-B6 T4; no scoring logic here).

    impact_weights       — relative ranking weights across the four Section-2
                           dimensions (effort_concentration, breadth,
                           recurrence_stability, automation_shape).
    automation_shape     — per-dimension thresholds the T4 scorer uses to derive
                           the automation-shape score (trivial vs judgment-heavy).
    recurrence_stability — per-dimension values the T4 scorer uses to derive the
                           recurrence-stability score (steady vs burst).
    confidence           — the honest-confidence caps enforced at the pack
                           boundary (T6).
    """
    impact_weights: Dict[str, float] = field(default_factory=dict)
    confidence: Dict[str, str] = field(default_factory=dict)
    automation_shape: Dict[str, float] = field(default_factory=dict)
    recurrence_stability: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CloudOpsPackConfig:
    """Fully-loaded, validated Cloud-Operations pack configuration."""
    pack_version: str
    terminology: CloudOpsTerminology
    thresholds: Dict[str, Any]
    calibration: CloudOpsCalibration
    source_path: str


# ── Loader (cached by path + mtime so config edits apply with no restart) ───────

# AC2: a config change must alter behaviour with no code deploy. We cache by
# (path, mtime) so repeated reads are cheap, but an edit to the file (new mtime)
# transparently invalidates the cache and is picked up on the next call.
_CACHE: Dict[str, Any] = {}


def _numeric_map(raw: Any) -> Dict[str, float]:
    """Coerce a config sub-block into a {str: float} map, dropping non-numeric values.

    Used for the scorer's per-dimension calibration blocks (automation_shape /
    recurrence_stability), which carry only numeric thresholds. Documentation-only
    keys (already stripped by ``_strip_meta``) and any stray non-numeric value are
    ignored so a malformed entry never crashes the loader.
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


def load_cloud_ops_config(path: Optional[str] = None) -> CloudOpsPackConfig:
    """Load, validate, and return the Cloud-Operations pack config.

    Raises CloudOpsConfigError if the file is missing, unparseable, or does not
    cover the required NOC terminology set (AC3).

    2.0-C1 T3 (AT-828): when the caller passes no explicit ``path``, an active
    per-run VERSION PIN is honoured before ``DEFAULT_CONFIG_PATH`` — so a run rolled
    back to 1.1.0 loads 1.1.0's archived thresholds/calibration rather than the
    current ones. Detectors and scorers call the accessors below with no path, which
    is exactly why the precedence lives here. With no pin active this is unchanged.
    """
    cfg_path = resolve_config_path(PACK_ID, path, DEFAULT_CONFIG_PATH)

    try:
        mtime = os.path.getmtime(cfg_path)
    except OSError as exc:
        raise CloudOpsConfigError(
            f"Cloud-Operations pack config not found at {cfg_path!r}: {exc}"
        ) from exc

    cached = _CACHE.get(cfg_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise CloudOpsConfigError(
            f"Cloud-Operations pack config at {cfg_path!r} could not be parsed: {exc}"
        ) from exc

    data = _strip_meta(raw)

    terminology_raw = data.get("terminology", {}) or {}
    glossary = {str(k).lower(): str(v) for k, v in (terminology_raw.get("glossary", {}) or {}).items()}

    missing = [t for t in REQUIRED_NOC_TERMS if t not in glossary]
    if missing:
        raise CloudOpsConfigError(
            f"Cloud-Operations terminology config at {cfg_path!r} is missing required "
            f"NOC term(s): {missing}. Required set: {list(REQUIRED_NOC_TERMS)}."
        )

    terminology = CloudOpsTerminology(
        glossary=glossary,
        language_map=dict(terminology_raw.get("language_map", {}) or {}),
    )

    calibration_raw = data.get("calibration", {}) or {}
    calibration = CloudOpsCalibration(
        impact_weights={
            str(k): float(v) for k, v in (calibration_raw.get("impact_weights", {}) or {}).items()
        },
        confidence=dict(calibration_raw.get("confidence", {}) or {}),
        automation_shape=_numeric_map(calibration_raw.get("automation_shape", {})),
        recurrence_stability=_numeric_map(calibration_raw.get("recurrence_stability", {})),
    )

    config = CloudOpsPackConfig(
        pack_version=str(data.get("packVersion", "")),
        terminology=terminology,
        thresholds=dict(data.get("thresholds", {}) or {}),
        calibration=calibration,
        source_path=cfg_path,
    )

    _CACHE[cfg_path] = (mtime, config)
    return config


# ── Convenience accessors ───────────────────────────────────────────────────────


def get_terminology(path: Optional[str] = None) -> CloudOpsTerminology:
    """Return the externalized NOC terminology set."""
    return load_cloud_ops_config(path).terminology


def get_thresholds(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the externalized detector thresholds (read by T2/T3 detectors)."""
    return dict(load_cloud_ops_config(path).thresholds)


def get_detector_thresholds(
    section: str,
    fallback: Optional[Dict[str, Any]] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Return one detector's threshold block, config-driven with a safe fallback.

    Reads ``thresholds[section]`` from the external config (MSP-B6 T1 AC2). If the
    config is missing/unreadable, returns ``fallback`` so a detector degrades to
    its documented defaults rather than failing a run — the config edit still wins
    when the file is present.
    """
    base = dict(fallback or {})
    try:
        section_cfg = load_cloud_ops_config(path).thresholds.get(section, {}) or {}
    except CloudOpsConfigError as exc:  # pragma: no cover - defensive
        logger.warning(
            "cloud_ops thresholds for %r unavailable (%s); using defaults %s",
            section, exc, base,
        )
        return base
    base.update({k: v for k, v in section_cfg.items() if not str(k).startswith("_")})
    return base


def get_calibration(path: Optional[str] = None) -> CloudOpsCalibration:
    """Return the externalized scorer calibration (read by the T4 ops-impact scorer)."""
    return load_cloud_ops_config(path).calibration


def get_noc_term(term: str, path: Optional[str] = None) -> str:
    """Return the definition of a single NOC term, or '' if not defined."""
    return get_terminology(path).term(term)
