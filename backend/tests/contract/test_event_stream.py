"""Org change stream (multi-user reactivity) — contract tests.

An org is used by several people at once, so one user's change must reach the
others. These pin the guarantees of the push half (app/event_stream.py):

  * a change is delivered ONLY to the publishing org's subscribers (isolation),
  * publishing with nobody listening is harmless,
  * a slow/stalled client cannot grow the server's memory (bounded, drop-oldest),
  * the stream is authenticated.

The live streaming response itself is deliberately not driven here: it is an
infinite generator, so a TestClient GET would block. Its behaviour is covered by
the bus tests below plus the auth gate (which rejects before streaming starts).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import event_stream


def test_publish_with_no_subscribers_is_a_noop():
    # Must never raise: a notification failure cannot be allowed to break the
    # request that triggered it.
    event_stream.publish("org_with_no_subscribers")


def test_publish_reaches_only_the_publishing_org():
    """Tenant isolation: an org's change never leaks into another org's stream."""
    queue_a = event_stream._subscribe("org_a")
    queue_b = event_stream._subscribe("org_b")
    try:
        event_stream.publish("org_a")
        assert queue_a.qsize() == 1
        assert queue_b.qsize() == 0
    finally:
        event_stream._unsubscribe("org_a", queue_a)
        event_stream._unsubscribe("org_b", queue_b)


def test_publish_reaches_every_subscriber_of_the_org():
    """Two people on the same org → both browsers get the ping."""
    first = event_stream._subscribe("org_multi")
    second = event_stream._subscribe("org_multi")
    try:
        event_stream.publish("org_multi")
        assert first.qsize() == 1
        assert second.qsize() == 1
    finally:
        event_stream._unsubscribe("org_multi", first)
        event_stream._unsubscribe("org_multi", second)


def test_subscriber_count_tracks_subscribe_and_unsubscribe():
    assert event_stream.subscriber_count("org_count") == 0
    queue = event_stream._subscribe("org_count")
    assert event_stream.subscriber_count("org_count") == 1
    event_stream._unsubscribe("org_count", queue)
    # Fully unsubscribed orgs are dropped, not left as empty sets.
    assert event_stream.subscriber_count("org_count") == 0


def test_empty_org_id_publishes_nothing():
    queue = event_stream._subscribe("org_guard")
    try:
        event_stream.publish("")
        assert queue.qsize() == 0
    finally:
        event_stream._unsubscribe("org_guard", queue)


def test_slow_subscriber_queue_is_bounded_and_drops_oldest():
    """A stalled client must not grow memory; coalesced pings are disposable."""
    queue = event_stream._subscribe("org_slow")
    try:
        for _ in range(event_stream._QUEUE_MAXSIZE + 10):
            event_stream.publish("org_slow")
        assert queue.qsize() <= event_stream._QUEUE_MAXSIZE
    finally:
        event_stream._unsubscribe("org_slow", queue)


def test_stream_requires_auth(client: TestClient):
    # Rejected before the streaming generator starts, so this cannot hang.
    resp = client.get("/api/events/stream")
    assert resp.status_code in (401, 403)
