"""Per-run effective pack version — 2.0-C1 T3 (AT-828) rollback.

A rolled-back pack must actually *behave* as its pinned version, not merely be
stamped with it. Two things carry that behaviour:

* **the detector list** — resolved eagerly by the runner from
  ``pack_config.resolve_pack_at_version``, so it needs no context; and
* **the external config artifact** (thresholds / calibration / terminology) — read
  LAZILY, deep inside detectors and scorers that call
  ``cloud_ops_config.get_detector_thresholds(section, fallback)`` with no path
  argument. Threading a config path through every detector signature would touch
  dozens of call sites and be easy to forget in the next one.

This module is that seam: the runner publishes ``{pack_id: config_path}`` for the
run, and each pack's config loader consults it when the caller passed no explicit
path. Precedence in the loaders is therefore:

    explicit path argument  →  this run context  →  the pack's default config path

Why a ``contextvars.ContextVar`` and not a module global: several Discovery Runs
execute concurrently in background threads, and Starlette runs each background task
via ``copy_context().run(...)``. A process-global would let one tenant's rolled-back
config leak into another tenant's concurrently-running run — exactly the reasoning
behind ``discovery/ingest/__init__.py``'s per-run live-connector context, which this
mirrors deliberately.

Default is an empty mapping: with nothing set (CLI, tests, un-pinned runs) every
loader falls through to its normal default path, so behaviour is unchanged.
"""

from __future__ import annotations

import contextvars
from typing import Dict, Mapping, Optional

# Per-run effective pack config paths: {pack_id: absolute config artifact path}.
# Empty/None means "no rollback active in this context" → default config paths.
_pack_config_paths: contextvars.ContextVar[Optional[Dict[str, str]]] = (
    contextvars.ContextVar("aiq_pack_config_paths", default=None)
)


def set_pack_config_paths(paths: Optional[Mapping[str, str]]) -> None:
    """Publish this run's effective per-pack config artifact paths.

    Pass ``None`` or an empty mapping to clear, so the current context falls back to
    each pack's default config path. Isolated per run via contextvars — safe under
    concurrent multi-tenant runs.
    """
    _pack_config_paths.set(
        {str(k): str(v) for k, v in (paths or {}).items() if k and v}
    )


def get_pinned_config_path(pack_id: str) -> Optional[str]:
    """This run's PINNED config artifact path for a pack, or ``None`` if not pinned.

    Deliberately NOT named ``get_pack_config_path`` — that name already belongs to
    ``pack_config.get_pack_config_path``, which returns the pack's DEFAULT registry
    config path. Two same-named accessors returning different paths would be a
    genuine footgun.
    """
    paths = _pack_config_paths.get()
    if not paths:
        return None
    return paths.get(str(pack_id))


def get_pack_config_paths() -> Dict[str, str]:
    """A copy of the whole per-run pinned mapping (empty when nothing is pinned)."""
    return dict(_pack_config_paths.get() or {})


class pack_config_paths:  # noqa: N801 - context-manager naming matches its usage
    """Scope a set of per-pack config paths to a ``with`` block.

    Restores the previous mapping on exit, so a nested/temporary override (a test,
    or a single materialisation step) cannot leak into the surrounding context.
    """

    def __init__(self, paths: Optional[Mapping[str, str]]) -> None:
        self._paths = paths
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> Dict[str, str]:
        self._token = _pack_config_paths.set(
            {str(k): str(v) for k, v in (self._paths or {}).items() if k and v}
        )
        return get_pack_config_paths()

    def __exit__(self, *_exc: object) -> None:
        if self._token is not None:
            _pack_config_paths.reset(self._token)
            self._token = None


def resolve_config_path(pack_id: str, explicit: Optional[str], default: str) -> str:
    """Apply the documented precedence for a pack config loader.

    ``explicit`` (a caller-supplied path) wins, then this run's pinned path, then the
    pack's ``default``. The single helper both pack config loaders call, so the
    precedence cannot drift between them.
    """
    if explicit:
        return explicit
    return get_pinned_config_path(pack_id) or default
