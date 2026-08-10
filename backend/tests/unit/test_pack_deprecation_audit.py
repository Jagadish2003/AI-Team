"""2.0-C4 T5 (AT-846) — the deprecation lifecycle audit, DB-free.

Sub-task scope: *deprecation, migration, and post-grace disable are audit events.*

Parent-story criterion this discharges:

  * AC4 — all three transitions are audit events.

Two of the three already reached the audit log when this task started (migration via
AT-844, post-grace disable via AT-845). **Deprecation itself did not** — a declaration
is a registry fact, and nothing recorded that a particular organisation had ever come
under it. That gap is what most of this suite is about.

The load-bearing rules pinned here:

  1. **All three transitions are covered**, and the claim is checkable rather than
     asserted in prose — a structural test walks the emitted event types.
  2. **Announced once**, so a pack re-evaluated on every activation does not bury the
     trail under one row per run.
  3. **Announced AGAIN when the terms change.** A moved grace date or a new
     replacement is materially different notice, and keying on the pack id alone
     would swallow exactly the changes that matter.
  4. **The audit never fails the activation.** A record is not worth refusing a run
     over.
  5. **Order follows the facts**: told, then retired.

The API half is pinned in ``tests/contract/test_pack_deprecation_audit_api.py``.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import pack_deprecation_audit  # noqa: E402
from app.middleware import audit as audit_module  # noqa: E402
from app.pack_activation import resolve_activatable_packs  # noqa: E402
from app.pack_deprecation_audit import (  # noqa: E402
    DEPRECATION_AUDIT_EVENTS,
    DEPRECATION_AUDIT_EVENT_TYPES,
    InMemoryAnnouncementLedger,
    PACK_DEPRECATION_ANNOUNCED,
    TRANSITION_DEPRECATED,
    TRANSITION_FOR_EVENT,
    TRANSITION_MIGRATED,
    TRANSITION_RETIRED,
    announce_deprecations,
    declaration_fingerprint,
    set_announcement_ledger,
)
from app.pack_state import (  # noqa: E402
    InMemoryPackStateStore,
    set_pack_state_store,
)
from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_config import DEPRECATION_KEY  # noqa: E402
from discovery.packs.pack_deprecation import (  # noqa: E402
    STATUS_DEPRECATED,
    get_pack_deprecation,
)

PACK = "cloud_ops"
OTHER_PACK = "service_cloud"
REPLACEMENT = "enterprise_ops"
ORG = "org_at846"
OTHER_ORG = "org_at846_other"

DEPRECATED_ON = "2026-07-01"
OPEN_UNTIL = "2099-09-29"
EXPIRED_ON = "2026-07-31"


@pytest.fixture(autouse=True)
def ledger():
    store = InMemoryAnnouncementLedger()
    set_announcement_ledger(store)
    set_pack_state_store(InMemoryPackStateStore())
    yield store
    set_announcement_ledger(None)
    set_pack_state_store(None)


@pytest.fixture(autouse=True)
def in_memory_certification_policy():
    """Activation consults the 2.0-C2 T4 policy, whose read FAILS CLOSED."""
    from app.pack_certification_policy import (
        InMemoryPackCertificationPolicyStore,
        set_policy_store,
    )

    set_policy_store(InMemoryPackCertificationPolicyStore())
    yield
    set_policy_store(None)


@pytest.fixture(autouse=True)
def captured_audit(monkeypatch):
    entries: list = []
    monkeypatch.setattr(
        audit_module,
        "log_event",
        lambda event_type, **kwargs: entries.append((event_type, kwargs)),
    )
    return entries


@pytest.fixture(autouse=True)
def captured_telemetry(monkeypatch):
    import app.telemetry as telemetry

    events: list = []
    monkeypatch.setattr(
        telemetry,
        "record_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )
    return events


@pytest.fixture
def deprecate(monkeypatch):
    def _deprecate(*, grace_ends_on=OPEN_UNTIL, replacement=REPLACEMENT, **extra):
        declaration = {
            "status": STATUS_DEPRECATED,
            "reason": "Superseded by the Enterprise Operations pack.",
            "deprecatedOn": DEPRECATED_ON,
            "graceEndsOn": grace_ends_on,
            "replacement": {"packId": replacement} if replacement else {},
        }
        declaration.update(extra)
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[PACK], DEPRECATION_KEY, declaration
        )
        return PACK

    return _deprecate


def _announcements(captured):
    return [
        payload
        for event_type, payload in captured
        if event_type == PACK_DEPRECATION_ANNOUNCED
    ]


# ── The gap this task closes ──────────────────────────────────────────────────


def test_coming_under_a_deprecation_is_an_audit_event(deprecate, captured_audit):
    """The transition that had no record at all before AT-846."""
    deprecate()

    announce_deprecations(org_id=ORG, pack_ids=[PACK, OTHER_PACK], run_id="run_1")

    entries = _announcements(captured_audit)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["pack_id"] == PACK
    assert entry["org_id"] == ORG
    assert entry["run_id"] == "run_1"
    assert entry["grace_ends_on"] == OPEN_UNTIL
    assert entry["replacement_pack_id"] == REPLACEMENT
    assert entry["reason"]
    assert entry["deprecated_on"] == DEPRECATED_ON


def test_it_is_emitted_by_the_shared_activation_path(deprecate, captured_audit):
    """Emitted from the ONE resolution both API edges and the runner call, so a CLI
    caller cannot produce a run whose deprecation exposure went unrecorded."""
    deprecate()

    resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])

    assert len(_announcements(captured_audit)) == 1


def test_a_healthy_pack_announces_nothing(captured_audit):
    resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])
    assert _announcements(captured_audit) == []


def test_it_also_emits_telemetry(deprecate, captured_telemetry):
    deprecate()

    announce_deprecations(org_id=ORG, pack_ids=[PACK], run_id="run_1")

    emitted = [
        payload
        for event_type, payload in captured_telemetry
        if event_type == "pack.deprecation_announced"
    ]
    assert len(emitted) == 1
    assert emitted[0]["pack_id"] == PACK


# ── Announced once, and again only when the terms change ──────────────────────


def test_a_repeat_activation_does_not_re_announce(deprecate, captured_audit):
    """A deprecated pack is re-evaluated on EVERY activation. One row, not one per
    run forever — otherwise the trail buries the entry that matters."""
    deprecate()

    first = announce_deprecations(org_id=ORG, pack_ids=[PACK])
    second = announce_deprecations(org_id=ORG, pack_ids=[PACK])

    assert first[0].announced is True
    assert second[0].announced is False
    assert len(_announcements(captured_audit)) == 1


def test_moving_the_grace_date_announces_again(deprecate, captured_audit):
    """Different terms are different notice. Keying on the pack id alone would
    swallow exactly the change a customer most needs telling about."""
    deprecate(grace_ends_on="2099-01-01")
    announce_deprecations(org_id=ORG, pack_ids=[PACK])

    deprecate(grace_ends_on="2099-12-31")
    again = announce_deprecations(org_id=ORG, pack_ids=[PACK])

    assert again[0].announced is True
    entries = _announcements(captured_audit)
    assert len(entries) == 2
    assert [e["grace_ends_on"] for e in entries] == ["2099-01-01", "2099-12-31"]


def test_changing_the_replacement_announces_again(deprecate, captured_audit):
    deprecate(replacement=REPLACEMENT)
    announce_deprecations(org_id=ORG, pack_ids=[PACK])

    deprecate(replacement="security_ops")
    again = announce_deprecations(org_id=ORG, pack_ids=[PACK])

    assert again[0].announced is True
    assert len(_announcements(captured_audit)) == 2


def test_the_phase_moving_to_expired_is_not_new_terms(deprecate, captured_audit):
    """Grace running out is the announced terms COMING TRUE, not new terms — and
    AT-845's retirement event already records that moment."""
    deprecate(grace_ends_on=EXPIRED_ON)
    in_grace = get_pack_deprecation(PACK, as_of=date(2026, 7, 1))
    expired = get_pack_deprecation(PACK, as_of=date(2026, 9, 1))

    assert in_grace.phase != expired.phase
    assert declaration_fingerprint(in_grace) == declaration_fingerprint(expired)


def test_announcements_are_org_scoped(deprecate, captured_audit):
    deprecate()

    announce_deprecations(org_id=ORG, pack_ids=[PACK])
    other = announce_deprecations(org_id=OTHER_ORG, pack_ids=[PACK])

    # The second org has NOT been told, so it gets its own entry.
    assert other[0].announced is True
    entries = _announcements(captured_audit)
    assert [e["org_id"] for e in entries] == [ORG, OTHER_ORG]


# ── Order follows the facts ───────────────────────────────────────────────────


def test_the_org_is_told_before_the_pack_is_retired(deprecate, captured_audit):
    deprecate(grace_ends_on=EXPIRED_ON)

    resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])

    emitted = [event_type for event_type, _ in captured_audit]
    assert PACK_DEPRECATION_ANNOUNCED in emitted
    assert "pack_deprecation_disabled" in emitted
    assert emitted.index(PACK_DEPRECATION_ANNOUNCED) < emitted.index(
        "pack_deprecation_disabled"
    )


# ── The audit never fails the activation ──────────────────────────────────────


def test_an_unwritable_ledger_still_announces(deprecate, captured_audit):
    """Err toward re-announcing rather than toward silence: a duplicate entry is
    noise, a missing one is a hole in the trail."""
    deprecate()

    class _Broken(pack_deprecation_audit.AnnouncementLedger):
        def read(self, org_id):
            raise RuntimeError("kv unavailable")

        def write(self, org_id, announced):
            raise RuntimeError("kv unavailable")

    set_announcement_ledger(_Broken())

    announce_deprecations(org_id=ORG, pack_ids=[PACK])
    announce_deprecations(org_id=ORG, pack_ids=[PACK])

    assert len(_announcements(captured_audit)) == 2


def test_an_unreadable_deprecation_does_not_fail_the_activation(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(
        "discovery.packs.pack_deprecation.get_pack_deprecation", _boom
    )

    assert announce_deprecations(org_id=ORG, pack_ids=[PACK]) == []
    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])
    assert decision.activated_pack_ids == [PACK]


def test_a_telemetry_failure_does_not_lose_the_audit_entry(
    deprecate, captured_audit, monkeypatch
):
    """Telemetry is observability beside the audit entry, never a gate on it."""
    import app.telemetry as telemetry

    deprecate()
    monkeypatch.setattr(
        telemetry,
        "record_event",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telemetry down")),
    )

    result = announce_deprecations(org_id=ORG, pack_ids=[PACK])

    assert result[0].announced is True
    assert len(_announcements(captured_audit)) == 1


def test_an_empty_org_id_announces_nothing(deprecate, captured_audit):
    deprecate()
    assert announce_deprecations(org_id="", pack_ids=[PACK]) == []
    assert _announcements(captured_audit) == []


# ── All three transitions, checkably ──────────────────────────────────────────


def test_the_three_transitions_are_named_and_each_has_an_event():
    assert set(DEPRECATION_AUDIT_EVENTS) == {
        TRANSITION_DEPRECATED,
        TRANSITION_MIGRATED,
        TRANSITION_RETIRED,
    }
    assert all(events for events in DEPRECATION_AUDIT_EVENTS.values())


def test_every_declared_event_type_is_a_registered_audit_event():
    """A transition whose event type is not in the registry would never be
    documented or validated — the mapping must not drift from reality."""
    for event_type in DEPRECATION_AUDIT_EVENT_TYPES:
        assert event_type in audit_module.AUDIT_EVENT_REGISTRY


def test_every_declared_event_type_is_actually_emitted_somewhere():
    """The claim is "these three transitions ARE audit events". This walks the source
    to check each declared type has a real emission site, so the mapping cannot
    become a list of aspirations."""
    import inspect

    from app import pack_grace
    from app import routes_pack_migration

    sources = "\n".join(
        inspect.getsource(module)
        for module in (pack_grace, routes_pack_migration, pack_deprecation_audit)
    )
    for event_type in DEPRECATION_AUDIT_EVENT_TYPES:
        # Emitted via the imported CONSTANT, so look for either form.
        constant = event_type.upper()
        assert event_type in sources or constant in sources, event_type


def test_every_deprecation_audit_constant_is_mapped_to_a_transition():
    """The reverse direction: an audit event named for this lifecycle that nobody
    mapped would render in the trail with a blank transition."""
    lifecycle_events = {
        value
        for name, value in vars(audit_module).items()
        if name.startswith(("PACK_DEPRECATION_", "PACK_MIGRATION_"))
        and isinstance(value, str)
    }
    assert lifecycle_events == set(DEPRECATION_AUDIT_EVENT_TYPES)
    assert all(TRANSITION_FOR_EVENT[event] for event in lifecycle_events)


def test_the_module_has_no_delete_path():
    """The trail is append-only end to end (2.0-C1 T4 / AT-829)."""
    import inspect

    source = inspect.getsource(pack_deprecation_audit).upper()
    for forbidden in ("DELETE FROM", "DROP TABLE", "TRUNCATE"):
        assert forbidden not in source
