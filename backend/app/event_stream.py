"""Org-scoped change stream (server-sent events).

An AgentIQ org is used by several people at once, so a change made by ONE user
must reach the others' browsers without a manual reload. This module is the push
half of that:

  * ``publish(org_id)`` announces "something in this org changed".
  * ``GET /api/events/stream`` is a per-client SSE subscription for the caller's
    own org; the frontend refreshes whatever it currently shows when an event
    lands (see ``frontend/src/lib/orgEvents.ts``).
  * ``register_change_publisher(app)`` publishes automatically after ANY
    successful mutating request (POST/PUT/PATCH/DELETE → 2xx), so a NEW mutation
    endpoint is covered without anyone remembering to touch this file.

Deliberately coarse: the event carries no payload beyond "changed". The client
already knows which resources it is displaying, so it simply revalidates those.
That keeps the wire contract tiny and means there is no per-resource key map to
keep in sync between backend and frontend — the classic way this kind of feature
rots.

Scope and limits (documented rather than hidden):
  * The bus is IN-PROCESS. With multiple uvicorn workers a client only receives
    events published by the worker holding its stream. This degrades to the
    frontend's focus/interval revalidation (the change still appears within
    ~30s), so it is a LATENCY fallback, not a correctness hole. A cross-process
    bus (Redis pub/sub or Postgres LISTEN/NOTIFY) is the upgrade path.
  * Subscriber queues are bounded and drop-oldest. "changed" pings coalesce, so
    dropping one is harmless (the next ping — or the fallback poll — refreshes
    the client anyway), and a slow/stalled client can never grow memory here.
  * Org isolation: a stream only ever receives its OWN org's events. The org is
    taken from the authenticated request context, never from client input.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Dict, Set

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse

from app.middleware.tenancy import get_current_org_id
from app.rbac import require_role
from app.security import require_auth

logger = logging.getLogger(__name__)

# Pings coalesce, so a tiny queue is plenty; drop-oldest bounds memory.
_QUEUE_MAXSIZE = 8
# Comment frame cadence — keeps idle proxies/load balancers from closing the
# connection, and lets the client notice a dead link.
_HEARTBEAT_SECONDS = 25

# org_id -> set of subscriber queues (one per open browser stream).
_subscribers: Dict[str, Set["asyncio.Queue[str]"]] = {}

_CHANGED = "changed"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _subscribe(org_id: str) -> "asyncio.Queue[str]":
    queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
    _subscribers.setdefault(org_id, set()).add(queue)
    return queue


def _unsubscribe(org_id: str, queue: "asyncio.Queue[str]") -> None:
    subs = _subscribers.get(org_id)
    if not subs:
        return
    subs.discard(queue)
    if not subs:
        _subscribers.pop(org_id, None)


def subscriber_count(org_id: str) -> int:
    """Open streams for an org. Exposed for tests/observability."""
    return len(_subscribers.get(org_id, ()))


def publish(org_id: str) -> None:
    """Announce a change to every open stream of ``org_id``.

    Never raises: a notification failure must not affect the request that
    triggered it. Only this org's subscribers are touched (tenant isolation).
    """
    if not org_id:
        return
    for queue in list(_subscribers.get(org_id, ())):
        try:
            queue.put_nowait(_CHANGED)
        except asyncio.QueueFull:
            # Drop-oldest: a coalesced "changed" ping is disposable — the client
            # revalidates on the next one (or on its fallback poll) regardless.
            try:
                queue.get_nowait()
                queue.put_nowait(_CHANGED)
            except Exception:
                pass
        except Exception:  # noqa: BLE001 — never break the caller
            pass


async def _event_generator(org_id: str) -> AsyncIterator[str]:
    queue = _subscribe(org_id)
    try:
        # An initial comment frame flushes headers through buffering proxies and
        # tells the client the stream is live.
        yield ": connected\n\n"
        while True:
            try:
                await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                yield "event: org.changed\ndata: {}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        _unsubscribe(org_id, queue)


def register_event_stream_routes(app: FastAPI) -> None:
    """GET /api/events/stream — this org's change feed (viewer+)."""

    @app.get(
        "/api/events/stream",
        dependencies=[Depends(require_auth), Depends(require_role("viewer"))],
    )
    async def events_stream() -> StreamingResponse:
        # Org comes from the authenticated request context only — a client can
        # never subscribe to another tenant's feed.
        org_id = get_current_org_id()
        return StreamingResponse(
            _event_generator(org_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # Ask nginx not to buffer the stream (it would defeat SSE).
                "X-Accel-Buffering": "no",
            },
        )


def register_change_publisher(app: FastAPI) -> None:
    """Publish a change ping after every successful mutating request.

    MUST be registered BEFORE ``register_tenancy`` so it runs INSIDE the tenancy
    middleware (Starlette runs middleware in reverse registration order) — the
    per-request org context must still be set when this reads it.
    """

    @app.middleware("http")
    async def _publish_on_mutation(request: Request, call_next):
        response = await call_next(request)
        try:
            if (
                request.method in _MUTATING_METHODS
                and 200 <= response.status_code < 300
            ):
                org_id = get_current_org_id()
                if org_id:
                    publish(org_id)
        except Exception:  # noqa: BLE001 — notification must never break a request
            logger.debug("change publish skipped", exc_info=True)
        return response
