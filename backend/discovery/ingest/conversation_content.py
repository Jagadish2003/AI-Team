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


class ConversationDeepContentError(RuntimeError):
    """Raised by the deep-content hand-off when the substrate reports failures.

    Propagated so a driver can leave a checkpoint un-advanced past conversation
    content that never reached retrieval — the batch is re-read and re-handed next
    run (idempotent via ``ingest_content``'s per-artifact replace).
    """


@dataclass
class ConversationDeepContentResult:
    """Accounting for one :func:`ingest_conversation_content` call."""

    org_id: str
    source_system: str
    threads: int = 0
    windows: int = 0
    artifacts_handed_off: int = 0
    artifacts_indexed: int = 0
    artifacts_empty: int = 0
    artifacts_failed: int = 0
    chunks_indexed: int = 0


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
        return f"{self.container_id}:{self.key}"


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


def thread_to_artifact(thread: ConversationThread) -> Any:
    """Map one assembled thread to a substrate :class:`ContentArtifact`.

    Carries the rendered author-attributed text plus thread-level provenance
    (channel, thread/window position, participants, and the observed evidence
    pointer) so a retrieval hit shows the exact source thread (AC1/AC5). The
    platform's per-message ``extra`` fields (e.g. Teams ``team_id``) are merged in.
    Imported lazily so this discovery module carries no import-time dependency on
    the app.retrieval package.
    """
    from app.retrieval.ingest import ContentArtifact

    provenance: Dict[str, Any] = {
        "source_system": thread.source_system,
        "channel_name": thread.container_name,
        "unit": thread.unit,
        "thread_id": None if thread.is_window else thread.key,
        "message_count": len(thread.messages),
        "participants": thread.participants,
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

    The one shared depth path both chat platforms drive (Slack — AT-594; Teams —
    AT-595). Messages are first scope-filtered against ``scope_container_ids`` — the
    explicitly selected/granted channels (AC2) — then assembled into threads/windows
    and handed to ``retrieval.ingest_content(org_id, artifacts)`` as
    ``conversation``-typed artifacts with thread-level observed provenance (AC1).
    Never writes vectors — the substrate owns chunking/embedding/indexing.

    ``ingest_fn`` is injectable for tests; it defaults to the real
    ``ingest_content``. Raises :class:`ConversationDeepContentError` when the
    substrate reports any failed artifact, so a caller that owns a checkpoint can
    leave it un-advanced and re-hand the batch next run (at-least-once; idempotent
    replace by ``(source_system, source_artifact)``).
    """
    result = ConversationDeepContentResult(org_id=org_id, source_system=source_system)
    if not messages:
        return result

    in_scope = [m for m in messages if m.container_id in scope_container_ids]
    if not in_scope:
        return result

    threads = assemble_threads(source_system, in_scope, window_seconds=window_seconds)
    result.threads = len(threads)
    result.windows = sum(1 for t in threads if t.is_window)
    if not threads:
        return result

    artifacts = [thread_to_artifact(t) for t in threads]
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
    return result
