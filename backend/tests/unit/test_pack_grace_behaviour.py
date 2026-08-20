"""2.0-C4 T4 (AT-845) — deprecation grace behaviour, DB-free.

Sub-task scope: *during grace the pack runs normally; after grace it moves to
disabled via C1's safe-disable path — history intact, never deleted.*

Parent-story criterion this discharges (backend half):

  * AC3 — the pack runs normally during grace and moves to safe-disabled after it,
    with history intact.

The load-bearing rules pinned here:

  1. **Grace is a promise that nothing changes yet.** A pack inside its grace is
     activated, its detectors resolve, and no state row is written. The whole first
     half of this feature is that negative.
  2. **Expiry is DERIVED, not scheduled.** No job runs; the grace ends because the
     date passed. A pack whose state write fails is *still* excluded.
  3. **It moves through C1's own path.** The stored state is C1's ``disabled``, the
     transition lands on C1's append-only history, and nothing is deleted.
  4. **A customer cannot un-expire a pack.** Re-enabling it does not bring it back.
  5. **Fail-soft in one direction only.** If deprecation cannot be read, nothing is
     treated as expired — a read error must never take a working pack offline.

The end-to-end and audit halves are pinned in
``tests/contract/test_pack_grace_lifecycle.py``.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import pack_grace  # noqa: E402
from app.pack_activation import (  # noqa: E402
    AllPacksDisabledError,
    resolve_activatable_packs,
)
from app.pack_grace import (  # noqa: E402
    EXCLUSION_REASON_GRACE_EXPIRED,
    SYSTEM_ACTOR,
    enforce_grace_expiry,
    expired_grace_packs,
    state_reason,
)
from app.pack_state import (  # noqa: E402
    InMemoryPackStateStore,
    STATE_ACTIVE,
    STATE_DISABLED,
    enable_pack,
    get_pack_state,
    pack_state_history,
    set_pack_state_store,
)
from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_config import DEPRECATION_KEY  # noqa: E402
from discovery.packs.pack_deprecation import (  # noqa: E402
    PHASE_GRACE,
    PHASE_GRACE_EXPIRED,
    STATUS_DEPRECATED,
    get_pack_deprecation,
)

PACK = "cloud_ops"
OTHER_PACK = "service_cloud"
REPLACEMENT = "enterprise_ops"
ORG = "org_at845"

#: Comfortably past, so the grace is over however long this test lives.
EXPIRED_ON = "2026-07-31"
#: Comfortably future, for the same reason in the other direction.
OPEN_UNTIL = "2099-09-29"
DEPRECATED_ON = "2026-07-01"


@pytest.fixture(autouse=True)
def pack_state_store():
    store = InMemoryPackStateStore()
    set_pack_state_store(store)
    yield store
    set_pack_state_store(None)


@pytest.fixture(autouse=True)
def in_memory_certification_policy():
    """Activation also consults the 2.0-C2 T4 policy, whose read FAILS CLOSED — so a
    DB-free suite must inject it or every activation refuses. No org sets a floor
    here, so the policy is the permissive default and the grace half is unaffected."""
    from app.pack_certification_policy import (
        InMemoryPackCertificationPolicyStore,
        set_policy_store,
    )

    set_policy_store(InMemoryPackCertificationPolicyStore())
    yield
    set_policy_store(None)


@pytest.fixture(autouse=True)
def captured_telemetry(monkeypatch):
    """Capture telemetry instead of writing it (this suite must not touch a DB)."""
    import app.telemetry as telemetry

    events: list = []
    monkeypatch.setattr(
        telemetry,
        "record_event",
        lambda event_type, payload: events.append((event_type, payload)),
    )
    return events


@pytest.fixture(autouse=True)
def captured_audit(monkeypatch):
    """Capture audit entries instead of writing them."""
    import app.middleware.audit as audit

    entries: list = []
    monkeypatch.setattr(
        audit, "log_event", lambda event_type, **kwargs: entries.append(
            (event_type, kwargs)
        )
    )
    return entries


@pytest.fixture
def deprecate(monkeypatch):
    """Deprecate a real registered pack for the duration of one test."""

    def _deprecate(pack_id=PACK, *, grace_ends_on, replacement=REPLACEMENT, **extra):
        declaration = {
            "status": STATUS_DEPRECATED,
            "reason": "Superseded by the Enterprise Operations pack.",
            "deprecatedOn": DEPRECATED_ON,
            "graceEndsOn": grace_ends_on,
            "replacement": {"packId": replacement} if replacement else {},
        }
        declaration.update(extra)
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[pack_id], DEPRECATION_KEY, declaration
        )
        return pack_id

    return _deprecate


# ── During grace, the pack runs normally (the promise) ────────────────────────


def test_a_pack_in_grace_is_still_activated(deprecate):
    deprecate(grace_ends_on=OPEN_UNTIL)

    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])

    assert decision.activated_pack_ids == [PACK]
    assert decision.excluded == []


def test_a_pack_in_grace_has_its_state_untouched(deprecate):
    """Not merely "still runs" — nothing is WRITTEN either. A grace period that
    quietly started recording state changes would not be a grace period."""
    deprecate(grace_ends_on=OPEN_UNTIL)

    resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])

    assert get_pack_state(ORG, PACK) == STATE_ACTIVE
    assert pack_state_history(ORG, PACK) == []


def test_open_ended_grace_never_expires(deprecate):
    """No announced removal date must never become an auto-disable on some date
    nobody declared (the AT-842 rule, enforced here)."""
    deprecate(grace_ends_on="", gracePeriodDays=None)

    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])

    assert decision.activated_pack_ids == [PACK]
    assert get_pack_state(ORG, PACK) == STATE_ACTIVE


def test_a_pack_that_is_not_deprecated_is_untouched():
    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])

    assert decision.activated_pack_ids == [PACK]
    assert get_pack_state(ORG, PACK) == STATE_ACTIVE


def test_the_boundary_day_still_runs(deprecate):
    """`graceEndsOn` is the LAST day the pack runs normally — expiry is strictly
    after it, matching the certification review-age rule."""
    deprecate(grace_ends_on="2026-08-31")

    on_the_day = get_pack_deprecation(PACK, as_of=date(2026, 8, 31))
    the_day_after = get_pack_deprecation(PACK, as_of=date(2026, 9, 1))

    assert on_the_day.phase == PHASE_GRACE
    assert the_day_after.phase == PHASE_GRACE_EXPIRED
    assert expired_grace_packs([PACK], as_of=date(2026, 8, 31)) == []
    assert [d.pack_id for d in expired_grace_packs([PACK], as_of=date(2026, 9, 1))] == [
        PACK
    ]


# ── After grace, it moves to safe-disabled ────────────────────────────────────


def test_an_expired_pack_is_moved_to_disabled(deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)

    expiries = enforce_grace_expiry(org_id=ORG, pack_ids=[PACK])

    assert [item.pack_id for item in expiries] == [PACK]
    assert expiries[0].disabled is True
    assert get_pack_state(ORG, PACK) == STATE_DISABLED


def test_the_transition_lands_on_c1s_append_only_history(deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)

    enforce_grace_expiry(org_id=ORG, pack_ids=[PACK])

    history = pack_state_history(ORG, PACK)
    assert len(history) == 1
    assert history[0]["transition"] == "disable"
    assert history[0]["previous_state"] == STATE_ACTIVE
    assert history[0]["resulting_state"] == STATE_DISABLED
    # A named non-human actor, so the trail distinguishes "the vendor retired this"
    # from "an owner turned it off".
    assert history[0]["actor_id"] == SYSTEM_ACTOR
    assert EXPIRED_ON in history[0]["reason"]
    assert REPLACEMENT in history[0]["reason"]


def test_an_expired_pack_is_excluded_from_activation(deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)

    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])

    assert decision.activated_pack_ids == [OTHER_PACK]
    assert [item.pack_id for item in decision.excluded] == [PACK]


def test_the_exclusion_names_the_grace_period_not_a_customer_disable(deprecate):
    """`pack_disabled` would send the operator to the re-enable button, which cannot
    help — the pack is retired, and the remedy is the replacement."""
    deprecate(grace_ends_on=EXPIRED_ON)

    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])

    assert decision.excluded[0].reason == EXCLUSION_REASON_GRACE_EXPIRED
    assert decision.excluded[0].state == STATE_DISABLED


def test_enforcement_is_idempotent_across_runs(deprecate):
    """An expired pack is re-evaluated on EVERY activation. One history row, not one
    per run forever."""
    deprecate(grace_ends_on=EXPIRED_ON)

    first = enforce_grace_expiry(org_id=ORG, pack_ids=[PACK])
    second = enforce_grace_expiry(org_id=ORG, pack_ids=[PACK])

    assert first[0].disabled is True
    assert second[0].disabled is False
    assert second[0].already_disabled is True
    assert len(pack_state_history(ORG, PACK)) == 1


def test_re_enabling_does_not_bring_a_retired_pack_back(deprecate):
    """Deprecation is the registry shipper's dimension; pack state is the customer's.
    A customer cannot un-deprecate a superseded pack (the AT-842 boundary)."""
    deprecate(grace_ends_on=EXPIRED_ON)
    resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])

    enable_pack(ORG, PACK, actor_id="user_owner", reason="we still need it")
    assert get_pack_state(ORG, PACK) == STATE_ACTIVE

    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])

    assert decision.activated_pack_ids == [OTHER_PACK]
    assert get_pack_state(ORG, PACK) == STATE_DISABLED


def test_every_selected_pack_expired_is_a_refusal_that_names_the_remedy(deprecate):
    """A run with zero packs would report success having produced nothing, so this
    still raises — but the message must not tell the operator to re-enable."""
    deprecate(grace_ends_on=EXPIRED_ON)

    with pytest.raises(AllPacksDisabledError) as exc:
        resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])

    assert PACK in str(exc.value)
    assert "cannot be re-enabled" in str(exc.value)
    assert "migrate" in str(exc.value).lower()


def test_expiry_wins_the_reason_when_the_pack_is_also_customer_disabled(deprecate):
    from app.pack_state import disable_pack

    deprecate(grace_ends_on=EXPIRED_ON)
    disable_pack(ORG, PACK, actor_id="user_owner", reason="paused")

    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])

    assert decision.excluded[0].reason == EXCLUSION_REASON_GRACE_EXPIRED


# ── The transition is an audit event (parent-story AC4, this transition's share) ─


def test_the_retirement_is_audited_once(deprecate, captured_audit):
    deprecate(grace_ends_on=EXPIRED_ON)

    enforce_grace_expiry(org_id=ORG, pack_ids=[PACK], run_id="run_1")
    enforce_grace_expiry(org_id=ORG, pack_ids=[PACK], run_id="run_2")

    retirements = [
        payload
        for event_type, payload in captured_audit
        if event_type == "pack_deprecation_disabled"
    ]
    # Re-evaluated every run; audited only on the real transition.
    assert len(retirements) == 1
    assert retirements[0]["pack_id"] == PACK
    assert retirements[0]["grace_ends_on"] == EXPIRED_ON
    assert retirements[0]["replacement_pack_id"] == REPLACEMENT
    assert retirements[0]["org_id"] == ORG


def test_the_retirement_emits_its_own_telemetry(deprecate, captured_telemetry):
    """A distinct event from `pack.state_changed`: the actor is the platform, not an
    owner, and support has to be able to separate the two."""
    deprecate(grace_ends_on=EXPIRED_ON)

    enforce_grace_expiry(org_id=ORG, pack_ids=[PACK], run_id="run_1")

    emitted = [
        payload
        for event_type, payload in captured_telemetry
        if event_type == "pack.deprecation_disabled"
    ]
    assert len(emitted) == 1
    assert emitted[0]["pack_id"] == PACK
    assert emitted[0]["actor_id"] == SYSTEM_ACTOR
    assert emitted[0]["run_id"] == "run_1"


def test_a_pack_in_grace_is_never_audited_as_retired(deprecate, captured_audit):
    """No RETIREMENT entry while the pack still runs.

    Scoped to this event type on purpose. 2.0-C4 T5 (AT-846) legitimately writes a
    `pack_deprecation_announced` entry on the same activation — the org has come
    under the terms — and that is a different fact from the pack being retired. A
    blanket "nothing was audited" assertion would forbid it.
    """
    deprecate(grace_ends_on=OPEN_UNTIL)

    resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])

    assert not [
        payload
        for event_type, payload in captured_audit
        if event_type == "pack_deprecation_disabled"
    ]


# ── History intact, never deleted ─────────────────────────────────────────────


def test_the_grace_module_has_no_delete_path():
    """The retirement reuses C1's disable, which cannot delete. Nothing here may
    grow its own path around that (2.0-C1 T4 / AT-829)."""
    import inspect

    source = inspect.getsource(pack_grace).upper()
    for forbidden in ("DELETE FROM", "DROP TABLE", "TRUNCATE"):
        assert forbidden not in source
    callables = {
        name
        for name, value in vars(pack_grace).items()
        if callable(value) and not name.startswith("__")
    }
    assert not {
        name
        for name in callables
        if "delete" in name.lower() or "purge" in name.lower()
    }


def test_retirement_writes_a_disable_and_never_removes_prior_history(deprecate):
    """The customer's own earlier transitions survive the automatic retirement."""
    from app.pack_state import disable_pack

    disable_pack(ORG, PACK, actor_id="user_owner", reason="paused for a sprint")
    enable_pack(ORG, PACK, actor_id="user_owner", reason="resumed")
    before = pack_state_history(ORG, PACK)
    assert len(before) == 2

    deprecate(grace_ends_on=EXPIRED_ON)
    enforce_grace_expiry(org_id=ORG, pack_ids=[PACK])

    after = pack_state_history(ORG, PACK)
    assert len(after) == 3
    # Newest first, and every earlier row is still exactly as it was.
    assert after[1:] == before
    assert after[0]["actor_id"] == SYSTEM_ACTOR


# ── Derived, not scheduled ────────────────────────────────────────────────────


def test_a_failed_state_write_still_excludes_the_pack(deprecate, monkeypatch):
    """The exclusion is derived, so a store failure costs the visible row and the
    history entry — never the guarantee."""
    deprecate(grace_ends_on=EXPIRED_ON)

    def _boom(*args, **kwargs):
        raise RuntimeError("state store unavailable")

    monkeypatch.setattr("app.pack_state.disable_pack", _boom)

    expiries = enforce_grace_expiry(org_id=ORG, pack_ids=[PACK])

    assert [item.pack_id for item in expiries] == [PACK]
    assert expiries[0].persisted is False
    assert expiries[0].disabled is False

    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])
    assert decision.activated_pack_ids == [OTHER_PACK]


def test_an_unreadable_deprecation_treats_nothing_as_expired(monkeypatch):
    """Fail-soft in ONE direction: a read error must never retire a working pack."""

    def _boom(*args, **kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(
        "discovery.packs.pack_deprecation.get_pack_deprecation", _boom
    )

    assert expired_grace_packs([PACK]) == []
    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])
    assert decision.activated_pack_ids == [PACK]


def test_enforcement_is_org_scoped(deprecate):
    deprecate(grace_ends_on=EXPIRED_ON)

    enforce_grace_expiry(org_id=ORG, pack_ids=[PACK])

    assert get_pack_state("org_other", PACK) == STATE_ACTIVE
    assert pack_state_history("org_other", PACK) == []


def test_state_reason_reads_for_a_human_months_later():
    reason = state_reason(
        pack_grace.GraceExpiry(
            pack_id=PACK,
            grace_ends_on=EXPIRED_ON,
            replacement_pack_id=REPLACEMENT,
        )
    )
    assert EXPIRED_ON in reason
    assert REPLACEMENT in reason
    assert len(reason) <= 1000
