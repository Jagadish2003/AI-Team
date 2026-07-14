"""R18-A6 / AT-611 (T6) — configured app-to-repo mapping per org.

Phase two parses repository content already moved by R18-A2, but A2 ingests repos
as flat, independent units — it does not know that ``covenant-web`` and
``covenant-core`` together ARE the *Covenant Service*. This module owns that
declaration: per org, which repos constitute a given application, and on which
platform. It is **configured, never auto-discovered** (consistent with phase
one's ``JAVA_APP_TARGETS`` / ``DOTNET_APP_TARGETS`` and A2's ``GIT_CONTENT_REPOS``
— AgentIQ never scans to *find* an application), and it is the scope that:

  * T1/T2 parse over — the structure parser runs across the union of an
    application's configured repos (:func:`extract_app_structure`);
  * T5 scopes retrieval against — "the code of the covenant service" resolves to
    exactly this application's repos, not path-coincidental matches;
  * T4 joins on — the ``app_id`` / ``service`` here is deliberately the SAME
    identity phase one uses for a *running* application, so a structural app and
    its operational counterpart share a join key.

Enables (no dedicated AC of its own): AC1/AC2 (defines the configured Java/.NET
app→repo scope extraction operates over), AC5 (the application boundary
component-scoped retrieval filters to), AC7 (the app whose runtime signal and
structure the end-to-end join reconciles).

Configuration shape (per org)
-----------------------------
Offline (default) reads the deterministic fixture
``fixtures/enterprise_app_repos.json``; live reads the ``ENTERPRISE_APP_REPOS``
env var. Either source is one of:

  * an **object keyed by org id** → the calling org's list of app entries (a
    ``"default"`` / ``"*"`` key is the fallback for any org) — the per-org shape;
  * a plain **array** of app entries → applied to every org (a single-tenant
    deployment default).

Each app entry is non-secret configuration::

    {"app_id": "covenant-service", "name": "Covenant Service",
     "platform": "java", "repos": ["covenant-web", "covenant-core"],
     "metadata": {"service": "covenant-service", "team": "lending"}}

Credentials never live here (repos are credentialed by A2 / the vault); to keep
the "no secret in config" discipline enforceable rather than documented, an entry
carrying an inline secret-looking field is rejected — reusing the SAME shared
guard phase one uses (:func:`discovery.ingest.operational_config.find_inline_secret_keys`).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from discovery.ingest import is_live
from discovery.ingest.operational_config import find_inline_secret_keys

from .structure import (
    PLATFORM_DOTNET,
    PLATFORM_JAVA,
    AppStructure,
    extract_structure,
)

logger = logging.getLogger(__name__)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "enterprise_app_repos.json"

#: Env var (live mode) holding the app→repo mapping — a JSON object keyed by org
#: id, or a plain array applied to every org.
_CONFIG_ENV = "ENTERPRISE_APP_REPOS"

#: Fallback keys used when the config is an org-keyed object and the calling org
#: has no explicit entry — a single shared declaration for every org.
_DEFAULT_ORG_KEYS: Tuple[str, ...] = ("default", "*")

#: Platforms an application may declare. Both ``java`` (T1/AT-606) and
#: ``dotnet`` (T2/AT-607) have a structure parser (the mapping itself is
#: platform-agnostic by design).
SUPPORTED_PLATFORMS: frozenset = frozenset({PLATFORM_JAVA, PLATFORM_DOTNET})

#: A source of A2-ingested repo content: given a repo id, yields that repo's files
#: (each a :class:`~discovery.enterprise_apps.structure.RepoFile` or a
#: ``{path, content}`` shape :func:`extract_structure` accepts). Injected by the
#: caller so this module never reaches into the retrieval substrate itself.
ContentProvider = Callable[[str], Iterable[Any]]


class EnterpriseAppConfigError(Exception):
    """Raised when the app→repo mapping configuration is invalid."""


@dataclass(frozen=True)
class AppRepoMapping:
    """One configured application and the repos that ARE it (R18-A6 T6).

    Pure, non-secret configuration. ``app_id`` is the stable identity shared with
    phase one (so the runtime→structure join has a key); ``platform`` selects the
    structure parser; ``repo_ids`` are the A2-ingested repos whose content makes up
    this application; ``metadata`` carries non-secret service info (service name,
    team, environment).
    """

    app_id: str
    name: str
    platform: str
    repo_ids: Tuple[str, ...]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.app_id or not isinstance(self.app_id, str):
            raise EnterpriseAppConfigError(
                "AppRepoMapping.app_id must be a non-empty string"
            )
        if self.platform not in SUPPORTED_PLATFORMS:
            raise EnterpriseAppConfigError(
                f"app '{self.app_id}' declares unsupported platform "
                f"{self.platform!r}; expected one of {sorted(SUPPORTED_PLATFORMS)}"
            )
        if not self.repo_ids:
            raise EnterpriseAppConfigError(
                f"app '{self.app_id}' declares no repos — nothing to map"
            )

    @property
    def service(self) -> str:
        """Service name used to join this app to its phase-one operational twin.

        Falls back to ``app_id`` when ``metadata.service`` is absent, so the
        runtime→structure join ("the same service") always has a key."""
        svc = self.metadata.get("service") if isinstance(self.metadata, dict) else None
        return str(svc) if svc else self.app_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "app_id": self.app_id,
            "name": self.name,
            "platform": self.platform,
            "repo_ids": list(self.repo_ids),
            "metadata": self.metadata,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Loading (configured, never discovered)
# ─────────────────────────────────────────────────────────────────────────────
def _as_str_tuple(value: Any) -> Tuple[str, ...]:
    """Coerce a config value to a de-duplicated tuple of non-empty strings.

    Tolerant of a list of ids, a single id string, or nothing; order is preserved
    and blanks/dupes are dropped so a hand-edited config stays deterministic."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    out: List[str] = []
    seen: set = set()
    for v in value:
        if v is None:
            continue
        s = str(v).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


def _select_org_entries(parsed: Any, org_id: str) -> List[Dict[str, Any]]:
    """Pick the raw app entries for ``org_id`` from a parsed config value.

    An array applies to every org; an object is org-keyed (with a
    ``default``/``*`` fallback). A top-level scalar key like ``_comment`` is
    naturally ignored — only keys whose value is a list are considered."""
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict)]
    if isinstance(parsed, dict):
        for key in (org_id, *_DEFAULT_ORG_KEYS):
            candidate = parsed.get(key)
            if isinstance(candidate, list):
                return [e for e in candidate if isinstance(e, dict)]
    return []


def _raw_entries(org_id: str) -> List[Dict[str, Any]]:
    """Raw app→repo entries for an org — configuration only, never discovery.

    Offline: the deterministic fixture. Live: the ``ENTERPRISE_APP_REPOS`` env
    JSON. A missing/blank source yields no entries (no apps configured)."""
    if not is_live():
        if not FIXTURE_PATH.exists():
            return []
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return _select_org_entries(data, org_id)

    raw = os.getenv(_CONFIG_ENV, "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise EnterpriseAppConfigError(
            f"{_CONFIG_ENV} is not valid JSON: {type(exc).__name__}"
        ) from exc
    return _select_org_entries(parsed, org_id)


def _coerce_mapping(entry: Dict[str, Any]) -> AppRepoMapping:
    """Build an :class:`AppRepoMapping` from a raw config dict, rejecting secrets.

    A mapping carries no credentials, so an inline secret-looking field is a
    misconfiguration; it is rejected (naming the offending KEYS only, never a
    value) using the same shared guard phase one uses."""
    if not isinstance(entry, dict):
        raise EnterpriseAppConfigError("each app→repo entry must be a JSON object")

    inline_secrets = find_inline_secret_keys(entry)
    if inline_secrets:
        raise EnterpriseAppConfigError(
            f"app '{entry.get('app_id', '?')}' contains inline credential field(s) "
            f"{inline_secrets}; repos are credentialed by A2/the vault — a mapping "
            "must carry no secrets."
        )

    metadata = entry.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    return AppRepoMapping(
        app_id=str(entry.get("app_id", "")).strip(),
        name=str(entry.get("name", entry.get("app_id", ""))).strip(),
        platform=str(entry.get("platform", "")).strip().lower(),
        repo_ids=_as_str_tuple(entry.get("repos", entry.get("repo_ids"))),
        metadata=metadata,
    )


def load_app_repo_mappings(org_id: str) -> List[AppRepoMapping]:
    """Return the configured app→repo mappings for ``org_id``.

    Returns exactly the configured set (no auto-discovery). Degrades rather than
    crashes: an invalid/insecure entry is skipped (logged by app_id / offending
    key, never by value); a duplicate ``app_id`` keeps the first; a repo already
    claimed by an earlier app is dropped from a later one so :func:`app_for_repo`
    is unambiguous (a repo is exactly one application). Config order is preserved,
    so the result is deterministic for a deterministic source."""
    mappings: List[AppRepoMapping] = []
    seen_apps: set = set()
    repo_owner: Dict[str, str] = {}
    for entry in _raw_entries(org_id):
        try:
            mapping = _coerce_mapping(entry)
        except EnterpriseAppConfigError as exc:
            logger.warning(
                "enterprise_apps: skipping invalid app→repo mapping (org=%s): %s",
                org_id,
                exc,
            )
            continue
        if mapping.app_id in seen_apps:
            logger.warning(
                "enterprise_apps: duplicate app_id '%s' (org=%s) — keeping the first",
                mapping.app_id,
                org_id,
            )
            continue
        kept: List[str] = []
        for repo_id in mapping.repo_ids:
            owner = repo_owner.get(repo_id)
            if owner is not None:
                logger.warning(
                    "enterprise_apps: repo '%s' is already mapped to app '%s' "
                    "(org=%s); ignoring its claim by '%s'",
                    repo_id,
                    owner,
                    org_id,
                    mapping.app_id,
                )
                continue
            repo_owner[repo_id] = mapping.app_id
            kept.append(repo_id)
        if not kept:
            logger.warning(
                "enterprise_apps: app '%s' (org=%s) has no unclaimed repos; skipping",
                mapping.app_id,
                org_id,
            )
            continue
        if tuple(kept) != mapping.repo_ids:
            mapping = dataclasses.replace(mapping, repo_ids=tuple(kept))
        seen_apps.add(mapping.app_id)
        mappings.append(mapping)
    return mappings


# ─────────────────────────────────────────────────────────────────────────────
# Lookups — the scope T1/T2/T5 read off
# ─────────────────────────────────────────────────────────────────────────────
def get_app_mapping(org_id: str, app_id: str) -> Optional[AppRepoMapping]:
    """The configured mapping for one application, or ``None`` if not configured."""
    target = (app_id or "").strip()
    if not target:
        return None
    for mapping in load_app_repo_mappings(org_id):
        if mapping.app_id == target:
            return mapping
    return None


def repo_ids_for_app(org_id: str, app_id: str) -> Tuple[str, ...]:
    """The repo ids that constitute an application (empty if not configured)."""
    mapping = get_app_mapping(org_id, app_id)
    return mapping.repo_ids if mapping else ()


def app_for_repo(org_id: str, repo_id: str) -> Optional[AppRepoMapping]:
    """The application a repo belongs to (reverse lookup), or ``None``.

    Unambiguous by construction — :func:`load_app_repo_mappings` gives each repo to
    exactly one application."""
    target = (repo_id or "").strip()
    if not target:
        return None
    for mapping in load_app_repo_mappings(org_id):
        if target in mapping.repo_ids:
            return mapping
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Bridge to T1/T2 — the mapping DRIVES which repo content is parsed
# ─────────────────────────────────────────────────────────────────────────────
def repo_content_for_app(
    org_id: str, app_id: str, content_provider: ContentProvider
) -> List[Any]:
    """Gather the A2-ingested content across an application's configured repos.

    ``content_provider(repo_id)`` yields that repo's files (whatever
    :func:`extract_structure` accepts). This is the scoping step: only the
    application's declared repos are read, in configured order. Raises when the app
    is not configured — a caller must not silently extract nothing."""
    mapping = get_app_mapping(org_id, app_id)
    if mapping is None:
        raise EnterpriseAppConfigError(
            f"no configured app→repo mapping for app_id '{app_id}' (org={org_id})"
        )
    files: List[Any] = []
    for repo_id in mapping.repo_ids:
        for f in content_provider(repo_id) or []:
            files.append(f)
    return files


def extract_app_structure(
    org_id: str, app_id: str, content_provider: ContentProvider
) -> AppStructure:
    """Extract an application's :class:`AppStructure` over its configured repos.

    The T6→T1/T2 bridge: resolve the app's platform + repo scope from the mapping,
    gather that scope's A2-ingested content via the injected ``content_provider``,
    and run the platform's deterministic parser over the union. No model is ever
    consulted (the extraction path is :func:`extract_structure`)."""
    mapping = get_app_mapping(org_id, app_id)
    if mapping is None:
        raise EnterpriseAppConfigError(
            f"no configured app→repo mapping for app_id '{app_id}' (org={org_id})"
        )
    files = repo_content_for_app(org_id, app_id, content_provider)
    return extract_structure(files, mapping.platform)
