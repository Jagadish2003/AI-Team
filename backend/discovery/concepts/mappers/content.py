"""2.0-B4 T2 — content sources → normalised concepts.

Confluence, SharePoint, Slack, Teams and GitHub. These five map a narrow slice of the
concept set (``artifact``, ``entity_reference``, and for the two chat platforms
``actor_group``) and that narrowness is the honest answer, not an unfinished one: a
wiki has no work items and a channel has no approvals.

Grouped in one module because they share one mapping — a content artifact — with the
per-source difference amounting to which ``artifact_type`` it is and where its
``location`` comes from. Five near-identical modules would be five places for that
mapping to drift.

Two rules this module exists to hold
------------------------------------
1. **``content_type`` is the retrieval substrate's vocabulary, not a second one.** The
   artifact contract requires the value the substrate would chunk this artifact under,
   so a Confluence page is ``prose``, a code file is ``code`` and a chat thread is
   ``conversation`` — matching ``retrieval/chunking.py``'s policies exactly. Two
   vocabularies for one idea drift; classify once.
2. **No content travels on the concept.** An ``Artifact`` is a REFERENCE to content.
   The bytes reach the platform through ``retrieval.ingest_content``, which is where
   secret redaction runs — so putting text on ``attributes`` would route content around
   the one path that redacts it.

A channel is a ``team``, never a ``queue``
-----------------------------------------
Slack and Teams containers map to ``group_type='team'``. The vocabulary's ``queue``
means work is routed to and drawn from the group; a channel is a container people talk
in. The distinction is load-bearing because a queue-ageing detector reading a channel
as a queue would report backlog that does not exist — which is why T1 wrote the
warning into the conformance reason rather than leaving it to a reviewer.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    from discovery.concepts import model as m
    from discovery.concepts.mappers import maps
    from discovery.concepts.mappers._common import (
        MappingInputError, concept_provenance, iso_or_none, require_id, text,
    )
except ModuleNotFoundError:  # pragma: no cover - import-style shim
    from backend.discovery.concepts import model as m
    from backend.discovery.concepts.mappers import maps
    from backend.discovery.concepts.mappers._common import (
        MappingInputError, concept_provenance, iso_or_none, require_id, text,
    )


def _artifact(
    org_id: str,
    source_system: str,
    signal_id: str,
    *,
    artifact_type: str,
    content_type: str,
    native_type: str,
    title: Optional[str] = None,
    location: Optional[str] = None,
    revision: Optional[str] = None,
    updated_at: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
) -> m.Artifact:
    """The shared artifact construction — one place, five sources."""
    return m.Artifact(
        org_id=org_id,
        source_system=source_system,
        signal_id=signal_id,
        observed_at=updated_at or "",
        provenance=concept_provenance(source_system, signal_id, updated_at, updated_at or ""),
        native_type=native_type,
        artifact_type=artifact_type,
        content_type=content_type,
        title=title,
        location=location,
        revision=revision,
        updated_at=updated_at,
        attributes=dict(attributes or {}),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Confluence — pages and blogposts (R18-A5 reads these); attachments route to the
# document path and are mapped there by the connector that owns them.
# ─────────────────────────────────────────────────────────────────────────────

@maps("confluence", m.CONCEPT_ARTIFACT)
def map_confluence_page(org_id: str, record: Dict[str, Any]) -> m.Artifact:
    """A Confluence page or blogpost → :class:`Artifact`."""
    page_id = require_id(record.get("id"), "Confluence content id")
    version = record.get("version") or {}
    updated = iso_or_none(version.get("when") if isinstance(version, dict) else None)
    links = record.get("_links") or {}
    return _artifact(
        org_id, "confluence", page_id,
        artifact_type="page",
        content_type="prose",
        native_type=text(record.get("type")) or "page",
        title=text(record.get("title")),
        location=text(links.get("webui") if isinstance(links, dict) else None),
        revision=text(version.get("number") if isinstance(version, dict) else None),
        updated_at=updated,
        attributes=_space_attributes(record),
    )


def _space_attributes(record: Dict[str, Any]) -> Dict[str, Any]:
    space = record.get("space")
    key = text(space.get("key")) if isinstance(space, dict) else text(space)
    return {"space_key": key} if key else {}


@maps("confluence", m.CONCEPT_ENTITY_REFERENCE)
def map_confluence_reference(record: Dict[str, Any]) -> m.EntityReference:
    """A Confluence page → :class:`EntityReference` (an ``object``, not a process)."""
    page_id = require_id(record.get("id"), "Confluence content id")
    return m.EntityReference(
        entity_type="object",
        source_system="confluence",
        source_record_id=page_id,
        display_name=text(record.get("title")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# SharePoint — site pages/lists (R18-A5) and library files (R18-A1 document path)
# ─────────────────────────────────────────────────────────────────────────────

@maps("sharepoint", m.CONCEPT_ARTIFACT)
def map_sharepoint_item(org_id: str, record: Dict[str, Any]) -> m.Artifact:
    """A SharePoint page or library file → :class:`Artifact`.

    The page-vs-file decision is NOT re-derived here: it follows the same rule as
    ``discovery/ingest/content_router.py``, which is the single classification point
    for this source. A ``file`` facet is a ``document``; anything reached through the
    pages surface is a ``page``. A folder is not an artifact and is refused rather
        than mapped to ``other``.
    """
    item_id = require_id(record.get("id"), "SharePoint item id")
    if record.get("folder") is not None:
        raise MappingInputError(
            "a SharePoint folder is not an artifact — content_router classifies it as "
            "SKIP, and mapping it to 'other' would put a container in the same concept "
            "as a document"
        )
    is_file = record.get("file") is not None
    updated = iso_or_none(record.get("lastModifiedDateTime"))
    return _artifact(
        org_id, "sharepoint", item_id,
        artifact_type="document" if is_file else "page",
        content_type="prose",
        native_type="driveItem" if is_file else "sitePage",
        title=text(record.get("name") or record.get("title")),
        location=text(record.get("webUrl")),
        revision=text(record.get("eTag")),
        updated_at=updated,
    )


@maps("sharepoint", m.CONCEPT_ENTITY_REFERENCE)
def map_sharepoint_reference(record: Dict[str, Any]) -> m.EntityReference:
    """A SharePoint item → :class:`EntityReference`."""
    item_id = require_id(record.get("id"), "SharePoint item id")
    return m.EntityReference(
        entity_type="object",
        source_system="sharepoint",
        source_record_id=item_id,
        display_name=text(record.get("name") or record.get("title")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Slack / Teams — threads as artifacts, containers as groups
# ─────────────────────────────────────────────────────────────────────────────

def _thread_artifact(org_id: str, source_system: str, record: Dict[str, Any]) -> m.Artifact:
    """A conversation thread → :class:`Artifact`.

    Keyed on the thread artifact id the R18-A4 conversation model already uses
    (``"{container}:{thread_key}"``), so a concept and a retrieval chunk point at the
    same artifact rather than at two different ids for one thread.
    """
    thread_id = require_id(
        record.get("thread_id") or record.get("id"), "conversation thread id"
    )
    updated = iso_or_none(record.get("last_ts") or record.get("updated_at"))
    attributes: Dict[str, Any] = {}
    channel = text(record.get("channel_name"))
    if channel:
        attributes["channel_name"] = channel
    # Participant COUNT only. The R18-A4 model carries participant names for
    # provenance, and copying them here would put individuals on a concept.
    participants = record.get("participants")
    if isinstance(participants, (list, tuple)):
        attributes["participant_count"] = len(participants)
    return _artifact(
        org_id, source_system, thread_id,
        artifact_type="conversation",
        content_type="conversation",
        native_type="thread",
        title=text(record.get("title")),
        location=text(record.get("permalink") or record.get("web_url")),
        updated_at=updated,
        attributes=attributes,
    )


@maps("slack", m.CONCEPT_ARTIFACT)
def map_slack_thread(org_id: str, record: Dict[str, Any]) -> m.Artifact:
    """A Slack thread → :class:`Artifact` (``conversation`` / ``conversation``)."""
    return _thread_artifact(org_id, "slack", record)


@maps("teams", m.CONCEPT_ARTIFACT)
def map_teams_thread(org_id: str, record: Dict[str, Any]) -> m.Artifact:
    """A Teams thread → :class:`Artifact` (``conversation`` / ``conversation``)."""
    return _thread_artifact(org_id, "teams", record)


def _channel_group(org_id: str, source_system: str, record: Dict[str, Any]) -> m.ActorGroup:
    """A channel → :class:`ActorGroup` of type ``team`` — never ``queue``."""
    channel_id = require_id(record.get("id"), f"{source_system} channel id")
    name = text(record.get("name") or record.get("displayName"))
    if not name:
        raise MappingInputError(f"{source_system} channel has no name — cannot map ActorGroup")
    updated = iso_or_none(record.get("updated_at") or record.get("lastModifiedDateTime"))
    members = record.get("member_count")
    return m.ActorGroup(
        org_id=org_id,
        source_system=source_system,
        signal_id=channel_id,
        observed_at=updated or "",
        provenance=concept_provenance(source_system, channel_id, updated, updated or ""),
        native_type="channel",
        group_type="team",
        name=name,
        member_count=int(members) if isinstance(members, (int, float)) else None,
    )


@maps("slack", m.CONCEPT_ACTOR_GROUP)
def map_slack_channel(org_id: str, record: Dict[str, Any]) -> m.ActorGroup:
    """A Slack channel → :class:`ActorGroup` (``team``)."""
    return _channel_group(org_id, "slack", record)


@maps("teams", m.CONCEPT_ACTOR_GROUP)
def map_teams_channel(org_id: str, record: Dict[str, Any]) -> m.ActorGroup:
    """A Teams channel → :class:`ActorGroup` (``team``)."""
    return _channel_group(org_id, "teams", record)


@maps("slack", m.CONCEPT_ENTITY_REFERENCE)
def map_slack_reference(record: Dict[str, Any]) -> m.EntityReference:
    """A Slack channel → :class:`EntityReference` (a ``team``)."""
    channel_id = require_id(record.get("id"), "Slack channel id")
    return m.EntityReference(
        entity_type="team",
        source_system="slack",
        source_record_id=channel_id,
        display_name=text(record.get("name")),
    )


@maps("teams", m.CONCEPT_ENTITY_REFERENCE)
def map_teams_reference(record: Dict[str, Any]) -> m.EntityReference:
    """A Teams channel → :class:`EntityReference` (a ``team``)."""
    channel_id = require_id(record.get("id"), "Teams channel id")
    return m.EntityReference(
        entity_type="team",
        source_system="teams",
        source_record_id=channel_id,
        display_name=text(record.get("displayName") or record.get("name")),
    )


# ─────────────────────────────────────────────────────────────────────────────
# GitHub — commits and code files (what R18-A2's git content path reads)
# ─────────────────────────────────────────────────────────────────────────────

@maps("github", m.CONCEPT_ARTIFACT)
def map_git_artifact(org_id: str, record: Dict[str, Any]) -> m.Artifact:
    """A commit or a repository file → :class:`Artifact`.

    ``kind`` selects which: ``'commit'`` (``content_type='conversation'``, matching how
    the substrate ingests a commit MESSAGE corpus) or ``'file'``
    (``content_type='code'``). Defaulting is refused — a caller that has not said which
    it holds has not classified the artifact, and guessing from the presence of a
    ``path`` would silently mis-type a commit that touched one file.
    """
    kind = (text(record.get("kind")) or "").strip().lower()
    if kind not in ("commit", "file"):
        raise MappingInputError(
            "github artifact requires kind='commit' or kind='file'; the two carry "
            "different content_types and must not be inferred from the record's shape"
        )
    repo = text(record.get("repo_id")) or ""
    sha = text(record.get("sha")) or ""
    if kind == "commit":
        signal_id = require_id(f"{repo}@{sha}" if repo and sha else sha, "commit id")
        return _artifact(
            org_id, "github", signal_id,
            artifact_type="commit",
            content_type="conversation",
            native_type="commit",
            title=text(record.get("title")),
            location=signal_id,
            revision=sha or None,
            updated_at=iso_or_none(record.get("committed_at")),
            attributes={"repo_id": repo} if repo else {},
        )
    path = text(record.get("path"))
    if not path:
        raise MappingInputError("a github file artifact requires its repository path")
    signal_id = require_id(
        f"{repo}@{sha}:{path}" if repo and sha else path, "code file id"
    )
    return _artifact(
        org_id, "github", signal_id,
        artifact_type="code_file",
        content_type="code",
        native_type="blob",
        title=path,
        location=signal_id,
        revision=sha or None,
        updated_at=iso_or_none(record.get("committed_at")),
        attributes={"repo_id": repo} if repo else {},
    )


@maps("github", m.CONCEPT_ENTITY_REFERENCE)
def map_repo_reference(record: Dict[str, Any]) -> m.EntityReference:
    """A repository → :class:`EntityReference` (a ``project``)."""
    repo_id = require_id(record.get("repo_id") or record.get("id"), "repository id")
    return m.EntityReference(
        entity_type="project",
        source_system="github",
        source_record_id=repo_id,
        display_name=text(record.get("name") or record.get("repo_id")),
    )


__all__ = [
    "map_confluence_page",
    "map_confluence_reference",
    "map_sharepoint_item",
    "map_sharepoint_reference",
    "map_slack_thread",
    "map_teams_thread",
    "map_slack_channel",
    "map_teams_channel",
    "map_slack_reference",
    "map_teams_reference",
    "map_git_artifact",
    "map_repo_reference",
]
