"""
R18-A4 / AT-594 + AT-595 — shared conversation deep-content model.

The depth phase reads CONVERSATION TEXT from chat sources (Slack — T1/AT-594;
Teams — T2/AT-595) into the R18-B1 retrieval substrate. Both platforms do exactly
the same thing with the text: assemble messages into threads (or time-bounded
windows where no thread structure exists), render author-attributed text, stamp a
thread-level ``origin='observed'`` evidence pointer, and hand each thread to
``retrieval.ingest_content`` as a ``conversation``-typed ``ContentArtifact``.

Only the COLLECTION EDGE differs — how a Slack message vs a Microsoft Graph
message exposes its channel, author, timestamp and thread parent. So that edge is
the ONLY thing the platform ingestors implement: each adapts its raw delta records
into the neutral :class:`ConversationMessage` shape here, and this module owns the
rest (assembly, windowing, rendering, provenance, scope filtering, hand-off). The
two platforms therefore cannot drift — thread semantics, the artifact shape, and
the substrate contract live in one place (R18-A4 §4 "Threads are the unit of
meaning"; AT-595 "diverge only at the Graph collection edge").

Trust posture (R18-A4 §1): conversation content is context/corroboration and is
reported under its own ``source_system`` ('slack'/'teams') so the standing
conversation MEDIUM ceiling stays applicable to a retrieval hit — never relabelled.
This module never writes vectors: the substrate owns chunking (its *conversation*
policy), embedding, and indexing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.provenance import EvidencePointer, utc_now_iso

logger = logging.getLogger(__name__)

#: Conversation text chunks under the substrate's *conversation* policy (R18-A4 §1).
CONTENT_TYPE = "conversation"

#: Default time-bound window (seconds) for messages that carry no thread structure,
#: so the conversational unit is never a lone, context-free message. Consecutive
#: standalone messages within this many seconds of the window's first message form
#: one window "thread".
DEFAULT_WINDOW_SECONDS = 3600

#: Change kinds a delta record can carry (R18-A4 / AT-596 — T3). Mirrored as plain
#: strings (like :mod:`app.retrieval.freshness` does) so this shared model stays
#: decoupled from the connector base module and from the app.retrieval package.
#: A record with no recognised kind is treated as ``created`` (it is content that
#: newly appeared in a delta, so the default is "index it", not "invalidate it").
CHANGE_CREATED = "created"
CHANGE_UPDATED = "updated"
CHANGE_DELETED = "deleted"
_KNOWN_CHANGE_KINDS = frozenset({CHANGE_CREATED, CHANGE_UPDATED, CHANGE_DELETED})


def normalise_change_kind(value: Any) -> str:
    """Coerce a record's ``change_kind`` to a known kind, defaulting to created."""
    kind = str(value or "").strip().lower()
    return kind if kind in _KNOWN_CHANGE_KINDS else CHANGE_CREATED


def thread_artifact_id(container_id: str, key: str) -> str:
    """The substrate identity for a thread/window: ``"{container_id}:{key}"``.

    Single source of truth for the thread-level ``source_artifact`` shape so the
    ingest path, the freshness/refresh path (T3), and the content resolver all agree
    on the key every chunk is stored under (a mismatch would silently orphan chunks).
    """
    return f"{container_id}:{key}"


def split_thread_artifact_id(source_artifact: str) -> tuple:
    """Split a thread-level ``source_artifact`` back into ``(container_id, key)``.

    Splits on the LAST ``:`` so a container id that itself contains a colon (a Teams
    ``"{team_id}/{channel_id}"`` where the channel id is e.g. ``"19:ops"``) is
    preserved intact and only the trailing thread key is separated. Returns
    ``(None, None)`` for an id with no separator so callers can bail safely.
    """
    container_id, sep, key = str(source_artifact or "").rpartition(":")
    if not sep or not container_id or not key:
        return None, None
    return container_id, key


class ConversationDeepContentError(RuntimeError):
    """Raised by the deep-content hand-off when the substrate reports failures.

    Propagated so a driver can leave a checkpoint un-advanced past conversation
    content that never reached retrieval — the batch is re-read and re-handed next
    run (idempotent via ``ingest_content``'s per-artifact replace).
    """


@dataclass
class ConversationDeepContentResult:
    """Accounting for one deep-content hand-off (create path + T3 freshness)."""

    org_id: str
    source_system: str
    threads: int = 0
    windows: int = 0
    artifacts_handed_off: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0
    # R18-A4 / AT-596 (T3) — edit/delete propagation into R18-B2 freshness.
    threads_refreshed: int = 0  # threads marked stale + queued for async re-chunk
    threads_removed: int = 0    # standalone/window threads purged immediately (delete)
    freshness_events: int = 0   # total thread-level artifact_changed events emitted


@dataclass
class ConversationChange:
    """A neutral message paired with the kind of change it represents (T3).

    The deep-content path builds one of these per delta record so the shared
    orchestrator can route creates to a direct substrate hand-off and edits/deletes
    into R18-B2 freshness at the thread level.
    """

    message: "ConversationMessage"
    change_kind: str = CHANGE_CREATED


@dataclass
class ConversationMessage:
    """A neutral, platform-agnostic view of one chat message (the collection edge).

    A platform ingestor maps each raw delta record into this shape and hands a list
    to :func:`assemble_threads` / :func:`ingest_conversation_content`; everything
    downstream is platform-independent.

    ``container_id``    the conversation container that scopes + identifies the
                        thread — a Slack channel id (``"C001"``) or a Teams
                        ``"{team_id}/{channel_id}"`` pair. Also the scope-check key.
    ``container_name``  human-facing channel/display name.
    ``msg_id``          the message's own identity within its container (Slack
                        ``ts``; Teams ``message_id``). Anchors a windowed unit.
    ``thread_key``      the thread anchor this message belongs to, or ``None`` when
                        it has no thread structure (→ time-windowed). For a reply
                        this is the parent's identity; for a thread parent, its own.
    ``sort_key``        ordering + window-gap key in SECONDS (Slack ``float(ts)``;
                        Teams marker epoch). Unparseable → ``-inf`` (sorts first).
    ``iso_ts``          ISO-8601 timestamp for the evidence-pointer spine / recency.
    ``author``          author reference (Slack/Teams user id) for attribution.
    ``text``            the message body text.
    ``extra``           platform provenance fields merged verbatim into the thread
                        provenance (e.g. Teams ``team_id``/``channel_id``).
    """

    container_id: str
    container_name: str
    msg_id: str
    thread_key: Optional[str]
    sort_key: float
    iso_ts: Optional[str]
    author: str
    text: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationThread:
    """One assembled conversational unit ready for the retrieval substrate.

    Either an explicit thread (a parent plus its replies, grouped by thread anchor)
    or a time-bounded window of standalone messages. ``messages`` are oldest-first;
    ``key`` is the thread anchor (parent identity, or the window's first message id).
    """

    source_system: str
    container_id: str
    container_name: str
    key: str
    messages: List[ConversationMessage]
    is_window: bool = False

    @property
    def unit(self) -> str:
        return "window" if self.is_window else "thread"

    @property
    def root_iso(self) -> Optional[str]:
        return self.messages[0].iso_ts if self.messages else None

    @property
    def latest_iso(self) -> Optional[str]:
        return self.messages[-1].iso_ts if self.messages else None

    @property
    def participants(self) -> List[str]:
        seen: List[str] = []
        for m in self.messages:
            if m.author and m.author not in seen:
                seen.append(m.author)
        return seen

    def source_artifact(self) -> str:
        """The substrate identity: ``"{container_id}:{key}"`` (thread-level, AC1).

        Every chunk the substrate derives for this thread shares this id, so a
        retrieval hit points at the exact thread.
        """
        return thread_artifact_id(self.container_id, self.key)


# ---------------------------------------------------------------------------
# Assembly — threads by anchor, else time-bounded windows
# ---------------------------------------------------------------------------
def assemble_threads(
    source_system: str,
    messages: List[ConversationMessage],
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> List[ConversationThread]:
    """Assemble neutral messages into conversational units — threads or windows.

    Messages are grouped per container by their thread anchor (``thread_key``);
    messages with no anchor are bucketed into time-bounded windows so a lone
    message is never handed over as a context-free artifact (R18-A4 §4).
    Deterministically ordered (container, then anchor).
    """
    by_container: Dict[str, List[ConversationMessage]] = {}
    for m in messages:
        by_container.setdefault(m.container_id, []).append(m)

    threads: List[ConversationThread] = []
    for container_id in sorted(by_container):
        threads.extend(
            _assemble_container(
                source_system, container_id, by_container[container_id], window_seconds
            )
        )
    return threads


def _assemble_container(
    source_system: str,
    container_id: str,
    msgs: List[ConversationMessage],
    window_seconds: int,
) -> List[ConversationThread]:
    container_name = ""
    for m in msgs:
        if m.container_name:
            container_name = m.container_name
            break

    explicit: Dict[str, List[ConversationMessage]] = {}
    loose: List[ConversationMessage] = []
    for m in msgs:
        if m.thread_key:
            explicit.setdefault(str(m.thread_key), []).append(m)
        else:
            loose.append(m)

    threads: List[ConversationThread] = []
    for key in sorted(explicit, key=lambda k: _min_sort_key(explicit[k])):
        group = sorted(explicit[key], key=lambda x: x.sort_key)
        threads.append(
            ConversationThread(source_system, container_id, container_name, key, group, False)
        )
    threads.extend(
        _window_loose(source_system, container_id, container_name, loose, window_seconds)
    )
    return threads


def _min_sort_key(group: List[ConversationMessage]) -> float:
    return min((m.sort_key for m in group), default=float("-inf"))


def _window_loose(
    source_system: str,
    container_id: str,
    container_name: str,
    loose: List[ConversationMessage],
    window_seconds: int,
) -> List[ConversationThread]:
    """Bucket standalone messages into time-bounded windows (R18-A4 §1)."""
    if not loose:
        return []
    ordered = sorted(loose, key=lambda m: m.sort_key)
    windows: List[List[ConversationMessage]] = []
    current: List[ConversationMessage] = []
    window_start = 0.0
    for m in ordered:
        if not current:
            current = [m]
            window_start = m.sort_key
        elif m.sort_key - window_start <= window_seconds:
            current.append(m)
        else:
            windows.append(current)
            current = [m]
            window_start = m.sort_key
    if current:
        windows.append(current)
    return [
        ConversationThread(
            source_system, container_id, container_name, w[0].msg_id, w, is_window=True
        )
        for w in windows
    ]


# ---------------------------------------------------------------------------
# Rendering + provenance + artifact
# ---------------------------------------------------------------------------
def render_thread_text(thread: ConversationThread) -> str:
    """Render a thread as author-attributed text, oldest message first (R18-A4 §1).

    Each message becomes an ``author: text`` line so a human reviewing a finding
    can read who said what; a blank-bodied message keeps its attribution line so
    participation stays visible.
    """
    lines: List[str] = []
    for m in thread.messages:
        author = m.author or "unknown"
        text = (m.text or "").strip()
        lines.append(f"{author}: {text}" if text else f"{author}:")
    return "\n".join(lines)


def thread_evidence_pointer(thread: ConversationThread) -> Dict[str, Any]:
    """Build the R16-B1 OBSERVED, THREAD-LEVEL evidence pointer (AC1, AC5).

    A finding citing this conversation can point at the exact thread: the pointer's
    ``source_artifact`` is the thread identity and ``origin='observed'`` (read
    directly from the source). ``source_timestamp`` anchors to the thread's first
    message, falling back to now only when unparseable so the spine is always full.
    """
    return EvidencePointer.observed(
        source_system=thread.source_system,
        source_artifact=thread.source_artifact(),
        source_timestamp=thread.root_iso or utc_now_iso(),
        source_artifact_type="record_id",
    ).to_dict()


#: An author resolver maps ``(display_name, source_record_id)`` for one participant
#: to the entity-layer link dict for a CONFIDENT match, or ``None`` (no confident
#: match → the author stays a plain reference). Injected so the discovery layer
#: carries no import-time dependency on the entity layer and tests stay DB-free.
AuthorResolver = Callable[[str, Optional[str]], Optional[dict]]

#: Sentinel distinguishing "caller did not specify a resolver" (build the default,
#: DB-backed one) from an explicit ``None`` (author resolution disabled).
_DEFAULT_AUTHOR_RESOLVER: Any = object()


def _participant_identity(msg: ConversationMessage) -> tuple:
    """The ``(display_name, source_record_id)`` a participant is resolved by (T4).

    Uniform across platforms: the display name is the message's author reference,
    and the stable source id is the platform's user id when it carries one (Teams
    ``extra['user_id']``) else the author reference itself (Slack, whose author IS
    the user id). A resolver can therefore match either by same-source id or by
    canonical display name without knowing which platform produced the message.
    """
    ref = msg.author or ""
    source_record_id = (msg.extra or {}).get("user_id") or ref or None
    return ref, source_record_id


def _resolve_participants(
    thread: ConversationThread, author_resolver: Optional[AuthorResolver]
) -> list:
    """Resolve a thread's distinct participants to entity links (T4, conservative).

    Returns one link dict per participant the resolver CONFIDENTLY matched (keyed by
    the raw ``ref`` so a consumer can tie it back to the rendered author line); an
    unresolved participant is simply omitted. Order follows first appearance in the
    thread. Never raises — a resolver failure for one author drops only that link.
    """
    if author_resolver is None:
        return []
    links: list = []
    seen: set = set()
    for m in thread.messages:
        ref = m.author
        if not ref or ref in seen:
            continue
        seen.add(ref)
        display_name, source_record_id = _participant_identity(m)
        try:
            link = author_resolver(display_name, source_record_id)
        except Exception:  # noqa: BLE001 — one author's link never breaks the thread
            link = None
        if link:
            links.append({"ref": ref, **link})
    return links


def build_author_resolver(
    org_id: str, source_system: str
) -> Optional[AuthorResolver]:
    """Build the default read-only participant→entity resolver (T4 / AT-597).

    Looks each participant up against the entity layer via the conservative,
    side-effect-free :func:`app.entity_resolution.lookup_resolved_entity` — a
    confident ``person`` match returns its graph entity id + canonical name; anything
    else returns ``None`` (the author stays a plain reference). Fully guarded and a
    no-op when no database is configured, so an offline run never touches a DB and
    resolution can only ever ADD links, never break or block ingestion.
    """
    import os

    if not os.getenv("DATABASE_URL"):
        return None
    try:
        from app.entity_resolution import lookup_resolved_entity
    except Exception:  # pragma: no cover — entity layer unavailable → no links
        return None

    def _resolve(display_name: str, source_record_id: Optional[str]) -> Optional[dict]:
        try:
            entity = lookup_resolved_entity(
                org_id=org_id,
                entity_type="person",
                display_name=display_name,
                source_system=source_system,
                source_record_id=source_record_id,
            )
        except Exception:  # noqa: BLE001 — a lookup failure just means "no link"
            return None
        if entity is None:
            return None
        return {
            "entity_id": str(entity.id),
            "canonical_name": entity.canonical_name,
            "display_name": entity.display_name,
            "resolution_confidence": entity.resolution_confidence,
            "resolution_status": entity.resolution_status,
        }

    return _resolve


def thread_to_artifact(
    thread: ConversationThread,
    *,
    author_resolver: Optional[Callable[[str, Optional[str]], Optional[dict]]] = None,
) -> Any:
    """Map one assembled thread to a substrate :class:`ContentArtifact`.

    Carries the rendered author-attributed text plus thread-level provenance
    (channel, thread/window position, participants, and the observed evidence
    pointer) so a retrieval hit shows the exact source thread (AC1/AC5). The
    platform's per-message ``extra`` fields (e.g. Teams ``team_id``) are merged in.
    Imported lazily so this discovery module carries no import-time dependency on
    the app.retrieval package.

    R18-A4 / AT-597 (T4): when an ``author_resolver`` is supplied, each distinct
    participant is looked up against the entity layer and CONFIDENT matches are
    recorded under ``provenance['participant_entities']`` (the graph entity id +
    canonical name), so a finding citing this thread can trace participants into the
    knowledge graph (AC5). Resolution is conservative — unresolved authors are simply
    absent from that list and remain plain references in ``participants``; nothing is
    ever created or merged here.
    """
    from app.retrieval.ingest import ContentArtifact

    provenance: Dict[str, Any] = {
        "source_system": thread.source_system,
        "channel_name": thread.container_name,
        "unit": thread.unit,
        "thread_id": None if thread.is_window else thread.key,
        "message_count": len(thread.messages),
        "participants": thread.participants,
        "participant_entities": _resolve_participants(thread, author_resolver),
        "first_ts": thread.root_iso,
        "last_ts": thread.latest_iso,
        "origin": "observed",
        "evidence_pointer": thread_evidence_pointer(thread),
    }
    if thread.messages:
        # Platform provenance fields (channel_id, team_id, …) from the root message.
        provenance.update(thread.messages[0].extra or {})

    return ContentArtifact(
        source_system=thread.source_system,
        source_artifact=thread.source_artifact(),
        content=render_thread_text(thread),
        content_type=CONTENT_TYPE,
        # Recency of the last message drives freshness; the evidence pointer anchors
        # to the thread's origin.
        source_timestamp=thread.latest_iso,
        provenance=provenance,
    )


# ---------------------------------------------------------------------------
# The shared hand-off
# ---------------------------------------------------------------------------
def ingest_conversation_content(
    org_id: str,
    source_system: str,
    messages: List[ConversationMessage],
    *,
    scope_container_ids: set,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ingest_fn: Optional[Callable[[str, List[Any]], Any]] = None,
) -> ConversationDeepContentResult:
    """Assemble in-scope messages into threads and hand them to the substrate.

    The create-path convenience wrapper: every message is treated as newly-created
    content (no edit/delete propagation). Kept for callers that only index fresh
    content; :func:`ingest_conversation_changes` is the full T3 entry point that also
    wires edits/deletions into freshness.

    Messages are scope-filtered against ``scope_container_ids`` — the explicitly
    selected/granted channels (AC2) — then assembled into threads/windows and handed
    to ``retrieval.ingest_content(org_id, artifacts)`` as ``conversation``-typed
    artifacts with thread-level observed provenance (AC1). Never writes vectors.

    ``ingest_fn`` is injectable for tests; it defaults to the real
    ``ingest_content``. Raises :class:`ConversationDeepContentError` when the
    substrate reports any failed artifact, so a caller that owns a checkpoint can
    leave it un-advanced and re-hand the batch next run (at-least-once; idempotent
    replace by ``(source_system, source_artifact)``).
    """
    changes = [ConversationChange(m, CHANGE_CREATED) for m in messages]
    return ingest_conversation_changes(
        org_id,
        source_system,
        changes,
        scope_container_ids=scope_container_ids,
        window_seconds=window_seconds,
        ingest_fn=ingest_fn,
    )


def ingest_conversation_changes(
    org_id: str,
    source_system: str,
    changes: List[ConversationChange],
    *,
    scope_container_ids: set,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    ingest_fn: Optional[Callable[[str, List[Any]], Any]] = None,
    freshness_fn: Optional[Callable[[dict], Any]] = None,
    author_resolver: Any = _DEFAULT_AUTHOR_RESOLVER,
) -> ConversationDeepContentResult:
    """Route a batch's conversation changes to the substrate + R18-B2 freshness (T3).

    The one shared depth path both chat platforms drive (Slack — AT-594; Teams —
    AT-595; edit/delete propagation — AT-596). Changes are first scope-filtered
    against ``scope_container_ids`` — the explicitly selected/granted channels (AC2)
    — then routed by change kind:

      * **created** content whose thread is fully present in this batch is handed
        DIRECTLY to ``retrieval.ingest_content`` as a ``conversation`` artifact
        (AC1) — the create path, unchanged from T1/T2.
      * **updated** (an edited message) → a thread-level ``updated`` freshness event
        so R18-B2 marks the thread's chunks stale and queues an async re-chunk of the
        WHOLE thread (AC3 edit). A ``created`` reply to a thread whose root is NOT in
        this batch is treated the same way — the whole thread is re-read rather than
        the partial batch clobbering it.
      * **deleted** → for a standalone/window message (the artifact IS that one
        message) a thread-level ``deleted`` event purges it from retrieval
        IMMEDIATELY (R18-B2's delete rule, AC3 delete); for a message inside a larger
        thread an ``updated`` event marks the thread stale at once (excluded from
        retrieval immediately) and queues a re-read that drops the deleted message.

    Edits/deletes ride the reach connectors' existing checkpoints — only new/changed
    messages since the checkpoint reach this function (AC4). The substrate owns
    chunking/embedding/indexing and the async refresh; this path never writes
    vectors. Both callbacks are injectable for tests: ``ingest_fn`` defaults to
    ``retrieval.ingest_content`` and ``freshness_fn`` to
    ``retrieval.freshness.on_artifact_changed`` (fire-and-forget; never raises).
    Raises :class:`ConversationDeepContentError` only when the create hand-off
    reports a failed artifact (at-least-once for freshly-created content).
    """
    result = ConversationDeepContentResult(org_id=org_id, source_system=source_system)
    if not changes:
        return result

    in_scope = [c for c in changes if c.message.container_id in scope_container_ids]
    if not in_scope:
        return result

    if author_resolver is _DEFAULT_AUTHOR_RESOLVER:
        author_resolver = build_author_resolver(org_id, source_system)

    handoff_msgs, refresh_ids, delete_ids = _classify_changes(in_scope)

    if handoff_msgs:
        _handoff_created_threads(
            result, org_id, source_system, handoff_msgs, window_seconds, ingest_fn,
            author_resolver,
        )

    if refresh_ids or delete_ids:
        _emit_freshness(
            result, org_id, source_system, refresh_ids, delete_ids, freshness_fn
        )

    return result


def _classify_changes(in_scope: List[ConversationChange]) -> tuple:
    """Partition in-scope changes into (create hand-off msgs, refresh ids, delete ids).

    A thread routed to freshness (refresh or delete) is never ALSO partially handed
    off: any created message whose thread is being refreshed/deleted is dropped from
    the direct hand-off so the whole-thread re-read is the single source of truth.
    """
    present = {(c.message.container_id, c.message.msg_id) for c in in_scope}
    refresh_ids: set = set()
    delete_ids: set = set()
    candidates: List[ConversationMessage] = []

    def art_of(m: ConversationMessage) -> str:
        return thread_artifact_id(m.container_id, m.thread_key or m.msg_id)

    for c in in_scope:
        m = c.message
        if c.change_kind == CHANGE_DELETED:
            if m.thread_key is None:
                # Standalone/window message: the artifact IS this one message →
                # purge it outright (immediate, B2's delete rule).
                delete_ids.add(art_of(m))
            else:
                # One message inside a larger thread → re-read drops it; mark the
                # thread stale immediately so the deleted text stops being served.
                refresh_ids.add(art_of(m))
        elif c.change_kind == CHANGE_UPDATED:
            refresh_ids.add(art_of(m))
        else:  # created
            root_in_batch = (
                m.thread_key is None
                or (m.container_id, m.thread_key) in present
            )
            if root_in_batch:
                candidates.append(m)
            else:
                # A reply to a pre-existing thread — re-read the whole thread rather
                # than let the partial batch clobber the stored thread's chunks.
                refresh_ids.add(art_of(m))

    handoff_msgs = [
        m for m in candidates
        if art_of(m) not in refresh_ids and art_of(m) not in delete_ids
    ]
    return handoff_msgs, refresh_ids, delete_ids


def _handoff_created_threads(
    result: ConversationDeepContentResult,
    org_id: str,
    source_system: str,
    messages: List[ConversationMessage],
    window_seconds: int,
    ingest_fn: Optional[Callable[[str, List[Any]], Any]],
    author_resolver: Optional[AuthorResolver] = None,
) -> None:
    """Assemble fully-present created threads and hand them to the substrate (AC1).

    Participants are resolved to graph entities (T4) via ``author_resolver`` as each
    artifact's provenance is built.
    """
    threads = assemble_threads(source_system, messages, window_seconds=window_seconds)
    result.threads = len(threads)
    result.windows = sum(1 for t in threads if t.is_window)
    if not threads:
        return

    artifacts = [thread_to_artifact(t, author_resolver=author_resolver) for t in threads]
    result.artifacts_handed_off = len(artifacts)

    fn = ingest_fn
    if fn is None:
        from app.retrieval.ingest import ingest_content as fn  # type: ignore

    ingest_result = fn(org_id, artifacts)
    result.artifacts_indexed = getattr(ingest_result, "artifacts_indexed", 0)
    result.artifacts_empty = getattr(ingest_result, "artifacts_empty", 0)
    result.artifacts_failed = getattr(ingest_result, "artifacts_failed", 0)
    result.chunks_indexed = getattr(ingest_result, "chunks_indexed", 0)

    logger.info(
        "%s deep content: org=%s threads=%d (windows=%d) handed_off=%d indexed=%d "
        "empty=%d failed=%d chunks_indexed=%d (embedding is async)",
        source_system,
        org_id,
        result.threads,
        result.windows,
        result.artifacts_handed_off,
        result.artifacts_indexed,
        result.artifacts_empty,
        result.artifacts_failed,
        result.chunks_indexed,
    )

    if result.artifacts_failed:
        raise ConversationDeepContentError(
            f"{result.artifacts_failed} {source_system} thread(s) failed retrieval "
            f"hand-off for org {org_id}; checkpoint not advanced (will retry)"
        )


def _emit_freshness(
    result: ConversationDeepContentResult,
    org_id: str,
    source_system: str,
    refresh_ids: set,
    delete_ids: set,
    freshness_fn: Optional[Callable[[dict], Any]],
) -> None:
    """Emit thread-level ``artifact_changed`` events into R18-B2 freshness (T3).

    Reuses the SAME subscriber the ingestion foundation feeds since 1.6
    (``on_artifact_changed``): an ``updated`` event marks the thread stale + queues
    a re-chunk; a ``deleted`` event purges it immediately. Fire-and-forget — the
    subscriber never raises, and a resolution failure only ever delays freshness.
    """
    fn = freshness_fn
    if fn is None:
        from app.retrieval.freshness import on_artifact_changed as fn  # type: ignore

    for art_id in sorted(refresh_ids):
        fn(_freshness_event(org_id, source_system, art_id, CHANGE_UPDATED))
        result.threads_refreshed += 1
    for art_id in sorted(delete_ids):
        fn(_freshness_event(org_id, source_system, art_id, CHANGE_DELETED))
        result.threads_removed += 1
    result.freshness_events = result.threads_refreshed + result.threads_removed

    logger.info(
        "%s deep content freshness: org=%s threads_refreshed=%d threads_removed=%d",
        source_system, org_id, result.threads_refreshed, result.threads_removed,
    )


def _freshness_event(
    org_id: str, source_system: str, source_artifact: str, change_kind: str
) -> dict:
    """Build an ``ingestion.artifact_changed`` payload keyed at the THREAD level.

    Speaks the ingestion vocabulary the freshness subscriber expects
    (``connector_id`` / ``artifact_id``) so the thread's chunks — not a single
    message's — are the unit invalidated/refreshed.
    """
    return {
        "org_id": org_id,
        "connector_id": source_system,
        "artifact_id": source_artifact,
        "change_kind": change_kind,
    }


def resolve_conversation_thread(
    org_id: str,
    source_artifact: str,
    source_system: str,
    read_container_messages: Callable[[str, str], List[ConversationMessage]],
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    author_resolver: Any = _DEFAULT_AUTHOR_RESOLVER,
) -> Optional[Any]:
    """Re-extract one thread's CURRENT content for the freshness refresh worker (T3).

    The content-resolver seam (R18-B2 T3) for conversation sources: given a
    thread-level ``source_artifact`` (``"{container}:{key}"``), re-read the whole
    container from the source, re-assemble threads/windows via the SHARED model, and
    return the matching thread as a ``conversation`` :class:`ContentArtifact` — so
    the refresh re-chunks the WHOLE thread through the identical path used at ingest
    (identical chunk boundaries → hash-compare can reuse unchanged vectors).

    Returns:
      * the thread's artifact when the thread still exists;
      * an EMPTY-content artifact when the thread/window no longer exists (every
        message deleted, or the window re-formed under a different key) — the swap
        then removes its chunks, so the resolver is self-cleaning;
      * ``None`` when the container cannot be read right now (transient source
        outage) so the refresh worker leaves the artifact queued for retry.

    ``read_container_messages(org_id, container_id)`` is the ONLY platform-specific
    input — it returns the container's current messages as neutral
    :class:`ConversationMessage`s, exactly what the collection edge produces.
    """
    from app.retrieval.ingest import ContentArtifact

    container_id, key = split_thread_artifact_id(source_artifact)
    if container_id is None:
        return None
    try:
        messages = read_container_messages(org_id, container_id)
    except Exception as exc:  # noqa: BLE001 — a transient read failure → stay queued
        # Log the reason on ONE line (no stack trace): the common case is simply
        # "this source isn't connected for this org" (e.g. a leftover queued
        # artifact for an org that never authenticated the connector) — an
        # expected, handled condition that leaves the artifact queued for retry,
        # not an unexpected crash. Matches the plain-message logging the refresh
        # worker uses for the Confluence/SharePoint content resolvers.
        logger.warning(
            "%s thread resolver: could not read container %r for org %s "
            "(will retry): %s",
            source_system, container_id, org_id, exc,
        )
        return None

    if author_resolver is _DEFAULT_AUTHOR_RESOLVER:
        author_resolver = build_author_resolver(org_id, source_system)

    for thread in assemble_threads(source_system, messages, window_seconds=window_seconds):
        if thread.source_artifact() == source_artifact:
            return thread_to_artifact(thread, author_resolver=author_resolver)

    # The thread/window is gone — return empty content so the swap removes its chunks.
    return ContentArtifact(
        source_system=source_system,
        source_artifact=source_artifact,
        content="",
        content_type=CONTENT_TYPE,
    )
