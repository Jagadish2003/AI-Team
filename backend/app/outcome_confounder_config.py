"""2.0-A2 T4 — external configuration for confounder detection.

Thresholds are configuration, not constants. The volume-shift threshold in
particular is a tuning parameter that will be wrong on first guess, so it lives in
``config/outcome_confounders.json`` where a value change alters behaviour with no
code deploy — the pattern the ``cloud_ops`` pack already established.

The config is explicit about **which values are measured and which are
provisional**, mirroring how ``discovery/signals/ops_calibration.py`` distinguishes
quantitatively-derived defaults from operationally-justified ones. Every threshold
carries a ``basis`` of ``measured`` / ``operationally_justified`` / ``provisional``
and the reasoning behind it, so a reader can tell a calibrated number from a first
guess without going to look for the commit that introduced it.

The seasonality check takes its **window definition** from config too, rather than
hardcoding a calendar assumption that is wrong for a large share of the customer
base — retail, education, public-sector fiscal years and southern-hemisphere
operations all disagree about what "the same part of the year" means.

Loaded with an mtime cache so an edit is picked up without a restart, and
documentation-only keys (``_``-prefixed) are stripped before use.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = str(
    Path(__file__).parent / "config" / "outcome_confounders.json"
)

SEVERITY_MATERIAL = "material"
SEVERITY_ADVISORY = "advisory"

#: Seasonality comparison modes the config may select.
SEASONALITY_CALENDAR_MONTH = "calendar_month_overlap"
SEASONALITY_FISCAL_QUARTER = "fiscal_quarter"
SEASONALITY_DECLARED_PERIODS = "declared_periods"
SEASONALITY_DISABLED = "disabled"

SEASONALITY_MODES = (
    SEASONALITY_CALENDAR_MONTH,
    SEASONALITY_FISCAL_QUARTER,
    SEASONALITY_DECLARED_PERIODS,
    SEASONALITY_DISABLED,
)

#: Recognised bases, in the order a reader should trust them.
BASIS_MEASURED = "measured"
BASIS_OPERATIONALLY_JUSTIFIED = "operationally_justified"
BASIS_PROVISIONAL = "provisional"
RECOGNISED_BASES = (BASIS_MEASURED, BASIS_OPERATIONALLY_JUSTIFIED, BASIS_PROVISIONAL)


class ConfounderConfigError(ValueError):
    """The confounder config is missing, unparseable, or internally inconsistent."""


def _strip_meta(obj: Any) -> Any:
    """Drop documentation-only keys (``_``-prefixed) from a loaded dict tree."""
    if isinstance(obj, dict):
        return {k: _strip_meta(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_meta(v) for v in obj]
    return obj


def _bases(raw: Dict[str, Any]) -> Dict[str, str]:
    """Each section's declared ``_basis``, kept for the audit surface.

    Read from the RAW tree before ``_strip_meta`` removes it, because a threshold
    whose provenance is invisible is a threshold people will assume is calibrated.
    """
    out: Dict[str, str] = {}
    for section, body in raw.items():
        if section.startswith("_") or not isinstance(body, dict):
            continue
        basis = body.get("_basis")
        if basis:
            out[section] = str(basis)
    return out


@dataclass(frozen=True)
class VolumeShiftConfig:
    material_shift_fraction: float = 0.25
    advisory_shift_fraction: float = 0.10


@dataclass(frozen=True)
class CiPopulationConfig:
    material_change_fraction: float = 0.20
    advisory_change_fraction: float = 0.05
    min_population_for_fraction: int = 5
    material_change_absolute: int = 2


@dataclass(frozen=True)
class PackVersionConfig:
    any_change_is_material: bool = True
    treat_missing_version_as_change: bool = False


@dataclass(frozen=True)
class SeasonalityConfig:
    mode: str = SEASONALITY_CALENDAR_MONTH
    min_month_overlap: float = 0.5
    fiscal_year_start_month: int = 1
    declared_periods: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.mode != SEASONALITY_DISABLED


@dataclass(frozen=True)
class ConfounderConfig:
    """The whole loaded config, plus where each threshold's authority comes from."""

    config_version: str
    volume_shift: VolumeShiftConfig
    ci_population: CiPopulationConfig
    pack_version: PackVersionConfig
    seasonality: SeasonalityConfig
    #: section -> declared basis. Served on the audit surface so a provisional
    #: number is never mistaken for a calibrated one.
    bases: Dict[str, str] = field(default_factory=dict)
    source_path: str = ""

    def basis_for(self, section: str) -> str:
        """The declared basis for a section, or ``provisional`` when undeclared.

        Defaults to the LEAST trustworthy value on purpose: an undeclared
        threshold has not earned the benefit of the doubt.
        """
        return self.bases.get(section, BASIS_PROVISIONAL)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configVersion": self.config_version,
            "sourcePath": self.source_path,
            "bases": dict(self.bases),
            "volumeShift": {
                "materialShiftFraction": self.volume_shift.material_shift_fraction,
                "advisoryShiftFraction": self.volume_shift.advisory_shift_fraction,
                "basis": self.basis_for("volume_shift"),
            },
            "ciPopulation": {
                "materialChangeFraction": self.ci_population.material_change_fraction,
                "advisoryChangeFraction": self.ci_population.advisory_change_fraction,
                "minPopulationForFraction": self.ci_population.min_population_for_fraction,
                "materialChangeAbsolute": self.ci_population.material_change_absolute,
                "basis": self.basis_for("ci_population"),
            },
            "packVersion": {
                "anyChangeIsMaterial": self.pack_version.any_change_is_material,
                "treatMissingVersionAsChange": (
                    self.pack_version.treat_missing_version_as_change
                ),
                "basis": self.basis_for("pack_version"),
            },
            "seasonality": {
                "mode": self.seasonality.mode,
                "minMonthOverlap": self.seasonality.min_month_overlap,
                "fiscalYearStartMonth": self.seasonality.fiscal_year_start_month,
                "declaredPeriods": list(self.seasonality.declared_periods),
                "basis": self.basis_for("seasonality"),
            },
        }


_CACHE: Dict[str, Tuple[float, ConfounderConfig]] = {}


def _float(body: Dict[str, Any], key: str, default: float) -> float:
    try:
        return float(body.get(key, default))
    except (TypeError, ValueError):
        logger.warning(
            "Confounder config: %r is not numeric; falling back to %s", key, default
        )
        return default


def _int(body: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(body.get(key, default))
    except (TypeError, ValueError):
        logger.warning(
            "Confounder config: %r is not an integer; falling back to %s", key, default
        )
        return default


def load_confounder_config(path: Optional[str] = None) -> ConfounderConfig:
    """Load and validate the confounder config, mtime-cached.

    A missing or unparseable file raises rather than silently falling back to
    defaults: a deployment that thinks it configured a threshold and did not is
    worse off than one told the config is broken.
    """
    cfg_path = path or DEFAULT_CONFIG_PATH

    try:
        mtime = os.path.getmtime(cfg_path)
    except OSError as exc:
        raise ConfounderConfigError(
            f"Outcome confounder config not found at {cfg_path!r}: {exc}"
        ) from exc

    cached = _CACHE.get(cfg_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfounderConfigError(
            f"Outcome confounder config at {cfg_path!r} could not be parsed: {exc}"
        ) from exc

    bases = _bases(raw)
    data = _strip_meta(raw)

    volume = data.get("volume_shift", {}) or {}
    ci = data.get("ci_population", {}) or {}
    pack = data.get("pack_version", {}) or {}
    season = data.get("seasonality", {}) or {}

    mode = str(season.get("mode", SEASONALITY_CALENDAR_MONTH))
    if mode not in SEASONALITY_MODES:
        raise ConfounderConfigError(
            f"Outcome confounder config at {cfg_path!r} declares unknown seasonality "
            f"mode {mode!r}. Valid modes: {list(SEASONALITY_MODES)}"
        )

    fiscal_start = _int(season, "fiscal_year_start_month", 1)
    if not 1 <= fiscal_start <= 12:
        raise ConfounderConfigError(
            f"Outcome confounder config at {cfg_path!r}: fiscal_year_start_month "
            f"must be 1-12, got {fiscal_start}"
        )

    unknown_bases = sorted(set(bases.values()) - set(RECOGNISED_BASES))
    if unknown_bases:
        raise ConfounderConfigError(
            f"Outcome confounder config at {cfg_path!r} declares unrecognised "
            f"basis value(s) {unknown_bases}. Valid: {list(RECOGNISED_BASES)}"
        )

    config = ConfounderConfig(
        config_version=str((raw.get("_meta") or {}).get("configVersion", "")),
        volume_shift=VolumeShiftConfig(
            material_shift_fraction=_float(volume, "material_shift_fraction", 0.25),
            advisory_shift_fraction=_float(volume, "advisory_shift_fraction", 0.10),
        ),
        ci_population=CiPopulationConfig(
            material_change_fraction=_float(ci, "material_change_fraction", 0.20),
            advisory_change_fraction=_float(ci, "advisory_change_fraction", 0.05),
            min_population_for_fraction=_int(ci, "min_population_for_fraction", 5),
            material_change_absolute=_int(ci, "material_change_absolute", 2),
        ),
        pack_version=PackVersionConfig(
            any_change_is_material=bool(pack.get("any_change_is_material", True)),
            treat_missing_version_as_change=bool(
                pack.get("treat_missing_version_as_change", False)
            ),
        ),
        seasonality=SeasonalityConfig(
            mode=mode,
            min_month_overlap=_float(season, "min_month_overlap", 0.5),
            fiscal_year_start_month=fiscal_start,
            declared_periods=list(season.get("declared_periods") or []),
        ),
        bases=bases,
        source_path=cfg_path,
    )

    _CACHE[cfg_path] = (mtime, config)
    return config


def confounder_config_summary(path: Optional[str] = None) -> Dict[str, Any]:
    """JSON audit surface — every threshold with its declared basis."""
    return load_confounder_config(path).to_dict()


__all__ = [
    "BASIS_MEASURED",
    "BASIS_OPERATIONALLY_JUSTIFIED",
    "BASIS_PROVISIONAL",
    "DEFAULT_CONFIG_PATH",
    "RECOGNISED_BASES",
    "SEASONALITY_CALENDAR_MONTH",
    "SEASONALITY_DECLARED_PERIODS",
    "SEASONALITY_DISABLED",
    "SEASONALITY_FISCAL_QUARTER",
    "SEASONALITY_MODES",
    "SEVERITY_ADVISORY",
    "SEVERITY_MATERIAL",
    "CiPopulationConfig",
    "ConfounderConfig",
    "ConfounderConfigError",
    "PackVersionConfig",
    "SeasonalityConfig",
    "VolumeShiftConfig",
    "confounder_config_summary",
    "load_confounder_config",
]
