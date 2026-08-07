"""2.0-C4 T3 (AT-844) — org-config pack migration, DB-free.

Sub-task scope: *where a replacement is declared, an org-config migration maps
template/pack selections from deprecated to replacement, previewed before applying
and reversible.*

Parent-story criterion this discharges (backend half):

  * AC2 — migration previews the config change, applies it on confirmation, and is
    reversible.

The load-bearing rules pinned here:

  1. **Preview writes nothing.** A migration a customer never saw is worse than the
     deprecation it fixes.
  2. **A preview is what gets applied.** The fingerprint ties the two together, so a
     configuration that moved in between is refused rather than silently migrated.
  3. **Revert restores, it does not invert.** A selection that already pointed at the
     replacement before the migration must still point at it afterwards.
  4. **Template remapping never guesses.** Zero or several candidate templates are
     reported and left alone, never resolved by picking one.
  5. **The ledger is append-only.** Reverting appends; it never edits or removes the
     row it undoes.

The route/UI halves are pinned in ``tests/contract/test_pack_migration_api.py`` and
``frontend/src/__tests__/PackMigrationAssist.test.tsx``.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app import pack_migration  # noqa: E402
from app.pack_migration import (  # noqa: E402
    InMemoryPackMigrationStore,
    PackMigrationConflict,
    PackMigrationNotFound,
    PackMigrationUnavailable,
    RECORD_APPLY,
    RECORD_REVERT,
    SURFACE_SETUP_STATE,
    UNAVAILABLE_NOT_DEPRECATED,
    UNAVAILABLE_NO_REPLACEMENT,
    UNMAPPED_AMBIGUOUS_TEMPLATE,
    UNMAPPED_NO_REPLACEMENT_TEMPLATE,
    WARNING_GRACE_EXPIRED,
    WARNING_REPLACEMENT_DISABLED,
    WARNING_TEMPLATE_CONTRIBUTIONS,
    apply_migration,
    get_migration,
    migration_history,
    preview_migration,
    revert_migration,
    set_pack_migration_store,
)
from app.pack_state import (  # noqa: E402
    InMemoryPackStateStore,
    PackNotFound,
    disable_pack,
    set_pack_state_store,
)
from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_config import DEPRECATION_KEY  # noqa: E402
from discovery.packs.pack_deprecation import (  # noqa: E402
    STATUS_DEPRECATED,
    deprecation_notice,
)

PACK = "cloud_ops"
#: `enterprise_ops` is declared by NO registered template — the "pack migrates, the
#: template selection cannot" case.
REPLACEMENT = "enterprise_ops"
#: `security_ops` is declared by exactly one template, so a template selection CAN be
#: remapped; `service_cloud` is declared by two, which must stay ambiguous.
REPLACEMENT_ONE_TEMPLATE = "security_ops"
REPLACEMENT_TWO_TEMPLATES = "service_cloud"

ORG = "org_at844"
OTHER_ORG = "org_at844_other"
ACTOR = "user_owner"

DEPRECATED_ON = "2026-07-01"
GRACE_ENDS_ON = "2099-09-29"
REASON = "Superseded by the Enterprise Operations pack."


@pytest.fixture(autouse=True)
def stores():
    """In-memory config + ledger and pack-state stores for every test."""
    store = InMemoryPackMigrationStore()
    set_pack_migration_store(store)
    set_pack_state_store(InMemoryPackStateStore())
    yield store
    set_pack_migration_store(None)
    set_pack_state_store(None)


@pytest.fixture
def deprecated_pack(monkeypatch):
    """Deprecate a real registered pack for the duration of one test."""

    def _deprecate(replacement=REPLACEMENT, **overrides):
        declaration = {
            "status": STATUS_DEPRECATED,
            "reason": REASON,
            "deprecatedOn": DEPRECATED_ON,
            "graceEndsOn": GRACE_ENDS_ON,
            "replacement": {"packId": replacement} if replacement else {},
        }
        declaration.update(overrides)
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[PACK], DEPRECATION_KEY, declaration
        )
        return PACK

    return _deprecate


def _state(**overrides):
    state = {
        "packId": PACK,
        "packIds": [PACK],
        "templateId": None,
        "templateIds": [],
        "selectedSystemIds": ["servicenow", "jira"],
        "currentStep": 4,
    }
    state.update(overrides)
    return state


def _seed(store, org=ORG, **overrides):
    store.seed_setup_state(org, _state(**overrides))


def _fields(plan_or_record):
    return {change.field for change in plan_or_record.changes}


def _change(plan_or_record, field):
    return next(c for c in plan_or_record.changes if c.field == field)


# ── Preview ───────────────────────────────────────────────────────────────────


def test_preview_of_a_healthy_pack_offers_nothing(stores):
    _seed(stores)
    plan = preview_migration(ORG, PACK)

    assert plan.available is False
    assert plan.applicable is False
    assert plan.reason_code == UNAVAILABLE_NOT_DEPRECATED
    assert "not deprecated" in plan.reason
    assert plan.changes == []
    assert plan.deprecation is None


def test_preview_without_a_replacement_says_so_rather_than_guessing(
    stores, deprecated_pack
):
    """A deprecation with no path is an answer a surface must explain, not an error."""
    deprecated_pack(replacement=None)
    _seed(stores)
    plan = preview_migration(ORG, PACK)

    assert plan.available is False
    assert plan.reason_code == UNAVAILABLE_NO_REPLACEMENT
    assert "no registered replacement" in plan.reason
    assert plan.changes == []
    # The notice is still attached: there IS a deprecation to tell the customer about,
    # there is just nothing to migrate them to.
    assert plan.deprecation is not None


def test_preview_maps_pack_selections_to_the_replacement(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores, packIds=[PACK, "service_cloud"])

    plan = preview_migration(ORG, PACK)

    assert plan.available is True
    assert plan.applicable is True
    assert plan.replacement_pack_id == REPLACEMENT
    assert _fields(plan) == {"packId", "packIds"}
    assert _change(plan, "packId").previous_value == PACK
    assert _change(plan, "packId").new_value == REPLACEMENT
    assert _change(plan, "packIds").previous_value == [PACK, "service_cloud"]
    assert _change(plan, "packIds").new_value == [REPLACEMENT, "service_cloud"]
    assert all(c.surface == SURFACE_SETUP_STATE for c in plan.changes)


def test_preview_writes_nothing(stores, deprecated_pack):
    """AC2's first word. Previewing must never be the thing that migrates."""
    deprecated_pack()
    _seed(stores)
    before = stores.read_setup_state(ORG)

    preview_migration(ORG, PACK)

    assert stores.read_setup_state(ORG) == before
    assert stores.records(ORG) == []


def test_preview_renders_the_same_notice_the_pack_picker_shows(
    stores, deprecated_pack
):
    """One deprecation, one sentence — the AT-843 rule carried into the migration."""
    deprecated_pack()
    _seed(stores)

    plan = preview_migration(ORG, PACK)

    assert plan.deprecation == deprecation_notice(PACK)


def test_preview_collapses_a_duplicate_the_mapping_would_create(
    stores, deprecated_pack
):
    deprecated_pack()
    _seed(stores, packId=REPLACEMENT, packIds=[REPLACEMENT, PACK])

    plan = preview_migration(ORG, PACK)

    # packId already pointed at the replacement, so only the list moves — and it moves
    # to ONE entry, not the same id twice.
    assert _fields(plan) == {"packIds"}
    assert _change(plan, "packIds").new_value == [REPLACEMENT]


def test_preview_for_an_org_with_no_saved_configuration_has_nothing_to_change(
    stores, deprecated_pack
):
    deprecated_pack()

    plan = preview_migration(ORG, PACK)

    assert plan.available is True
    assert plan.applicable is False
    assert plan.changes == []


def test_preview_of_an_unknown_pack_is_not_found(stores):
    """Strict, unlike `get_pack()` — a typo must not preview a migration off the
    default pack the caller never named."""
    with pytest.raises(PackNotFound):
        preview_migration(ORG, "not_a_pack")


def test_preview_is_org_scoped(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores, org=ORG)

    plan = preview_migration(OTHER_ORG, PACK)

    assert plan.applicable is False


# ── Template selections ───────────────────────────────────────────────────────


def test_template_selection_remaps_when_exactly_one_template_declares_the_replacement(
    stores, deprecated_pack
):
    deprecated_pack(replacement=REPLACEMENT_ONE_TEMPLATE)
    _seed(
        stores,
        templateId="managed_cloud_operations",
        templateIds=["managed_cloud_operations"],
    )

    plan = preview_migration(ORG, PACK)

    assert _fields(plan) == {"packId", "packIds", "templateId", "templateIds"}
    assert _change(plan, "templateId").new_value == "security_operations"
    assert _change(plan, "templateIds").new_value == ["security_operations"]
    assert plan.unmapped == []


def test_template_selection_is_left_alone_when_several_templates_declare_the_replacement(
    stores, deprecated_pack
):
    """Two candidates is a guess, and guessing changes what a customer's runs look
    for. The pack still migrates; the template selection is reported instead."""
    deprecated_pack(replacement=REPLACEMENT_TWO_TEMPLATES)
    _seed(
        stores,
        templateId="managed_cloud_operations",
        templateIds=["managed_cloud_operations"],
    )

    plan = preview_migration(ORG, PACK)

    assert _fields(plan) == {"packId", "packIds"}
    assert [item.reason for item in plan.unmapped] == [UNMAPPED_AMBIGUOUS_TEMPLATE]
    assert "service_operations" in plan.unmapped[0].detail
    assert "revenue_operations" in plan.unmapped[0].detail


def test_template_selection_is_left_alone_when_no_template_declares_the_replacement(
    stores, deprecated_pack
):
    deprecated_pack(replacement=REPLACEMENT)
    _seed(
        stores,
        templateId="managed_cloud_operations",
        templateIds=["managed_cloud_operations"],
    )

    plan = preview_migration(ORG, PACK)

    assert _fields(plan) == {"packId", "packIds"}
    assert [item.reason for item in plan.unmapped] == [
        UNMAPPED_NO_REPLACEMENT_TEMPLATE
    ]


def test_an_unrelated_template_selection_is_untouched(stores, deprecated_pack):
    deprecated_pack(replacement=REPLACEMENT_ONE_TEMPLATE)
    _seed(
        stores,
        templateId="commercial_lending",
        templateIds=["commercial_lending"],
    )

    plan = preview_migration(ORG, PACK)

    assert _fields(plan) == {"packId", "packIds"}
    assert plan.unmapped == []


def test_remapped_template_contributions_raise_a_review_warning(
    stores, deprecated_pack
):
    """Contributions are not re-keyed onto the replacement — that would attribute one
    template's system choices to another. The customer is told to review instead."""
    deprecated_pack(replacement=REPLACEMENT_ONE_TEMPLATE)
    _seed(
        stores,
        templateId="managed_cloud_operations",
        templateIds=["managed_cloud_operations"],
        templateContributions={
            "managed_cloud_operations": {
                "packId": PACK,
                "systemIds": ["servicenow", "aws_events"],
            }
        },
    )

    plan = preview_migration(ORG, PACK)

    assert WARNING_TEMPLATE_CONTRIBUTIONS in {w.code for w in plan.warnings}
    assert "templateContributions" not in _fields(plan)


# ── Warnings about the destination ────────────────────────────────────────────


def test_a_disabled_replacement_is_warned_about_but_does_not_block(
    stores, deprecated_pack
):
    deprecated_pack()
    disable_pack(ORG, REPLACEMENT, actor_id=ACTOR, reason="not ready")
    _seed(stores)

    plan = preview_migration(ORG, PACK)

    assert plan.applicable is True
    assert WARNING_REPLACEMENT_DISABLED in {w.code for w in plan.warnings}


def test_an_expired_grace_period_is_warned_about(stores, deprecated_pack):
    deprecated_pack(graceEndsOn="2026-07-31")
    _seed(stores)

    plan = preview_migration(ORG, PACK, as_of=date(2026, 8, 15))

    assert WARNING_GRACE_EXPIRED in {w.code for w in plan.warnings}
    assert plan.applicable is True


# ── Fingerprint ───────────────────────────────────────────────────────────────


def test_fingerprint_is_stable_for_an_unchanged_plan(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)

    assert preview_migration(ORG, PACK).fingerprint == (
        preview_migration(ORG, PACK).fingerprint
    )


def test_fingerprint_moves_when_the_configuration_moves(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    before = preview_migration(ORG, PACK).fingerprint

    _seed(stores, packIds=[PACK, "service_cloud"])

    assert preview_migration(ORG, PACK).fingerprint != before


# ── Apply ─────────────────────────────────────────────────────────────────────


def test_apply_rewrites_the_saved_configuration(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores, packIds=[PACK, "service_cloud"])

    record = apply_migration(ORG, PACK, actor_id=ACTOR, reason="ahead of grace")

    state = stores.read_setup_state(ORG)
    assert state["packId"] == REPLACEMENT
    assert state["packIds"] == [REPLACEMENT, "service_cloud"]
    # Everything else in the frontend-owned blob is left exactly as it was.
    assert state["selectedSystemIds"] == ["servicenow", "jira"]
    assert record.kind == RECORD_APPLY
    assert record.changed is True
    assert record.actor_id == ACTOR
    assert record.reason == "ahead of grace"


def test_apply_records_the_previous_values_verbatim(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores, packIds=[PACK, "service_cloud"])

    record = apply_migration(ORG, PACK, actor_id=ACTOR)

    assert _change(record, "packIds").previous_value == [PACK, "service_cloud"]


def test_apply_with_nothing_to_change_is_a_silent_no_op(stores, deprecated_pack):
    """Mirrors a no-op pack-state transition: safe to repeat, and not an audit event."""
    deprecated_pack()
    _seed(stores, packId=REPLACEMENT, packIds=[REPLACEMENT])

    record = apply_migration(ORG, PACK, actor_id=ACTOR)

    assert record.changed is False
    assert stores.records(ORG) == []


def test_applying_twice_is_idempotent(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)

    first = apply_migration(ORG, PACK, actor_id=ACTOR)
    second = apply_migration(ORG, PACK, actor_id=ACTOR)

    assert first.changed is True
    assert second.changed is False
    assert len(stores.records(ORG)) == 1


def test_apply_refuses_when_there_is_no_migration_to_make(stores):
    _seed(stores)
    with pytest.raises(PackMigrationUnavailable):
        apply_migration(ORG, PACK, actor_id=ACTOR)


def test_apply_refuses_a_replacementless_deprecation(stores, deprecated_pack):
    deprecated_pack(replacement=None)
    _seed(stores)
    with pytest.raises(PackMigrationUnavailable):
        apply_migration(ORG, PACK, actor_id=ACTOR)


def test_apply_refuses_a_fingerprint_that_no_longer_matches(stores, deprecated_pack):
    """AC2's "previewed before applying", enforced rather than assumed."""
    deprecated_pack()
    _seed(stores)
    previewed = preview_migration(ORG, PACK).fingerprint

    # Someone edits the configuration between preview and confirmation.
    _seed(stores, packIds=[PACK, "service_cloud"])

    with pytest.raises(PackMigrationConflict):
        apply_migration(
            ORG, PACK, actor_id=ACTOR, expected_fingerprint=previewed
        )
    # …and nothing was migrated on the way to refusing.
    assert stores.read_setup_state(ORG)["packId"] == PACK


def test_apply_accepts_the_fingerprint_it_previewed(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    plan = preview_migration(ORG, PACK)

    record = apply_migration(
        ORG, PACK, actor_id=ACTOR, expected_fingerprint=plan.fingerprint
    )

    assert record.changed is True


def test_apply_does_not_touch_another_org(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores, org=ORG)
    _seed(stores, org=OTHER_ORG)

    apply_migration(ORG, PACK, actor_id=ACTOR)

    assert stores.read_setup_state(OTHER_ORG)["packId"] == PACK
    assert stores.records(OTHER_ORG) == []


# ── Revert ────────────────────────────────────────────────────────────────────


def test_revert_restores_the_previous_configuration(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores, packIds=[PACK, "service_cloud"])
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)

    revert_migration(ORG, applied.id, actor_id=ACTOR, reason="rolling back")

    state = stores.read_setup_state(ORG)
    assert state["packId"] == PACK
    assert state["packIds"] == [PACK, "service_cloud"]


def test_revert_restores_rather_than_inverting_the_mapping(stores, deprecated_pack):
    """A selection that pointed at the replacement BEFORE the migration must still
    point at it afterwards — an inverse mapping would drag it back to the dead pack."""
    deprecated_pack()
    _seed(stores, packId=REPLACEMENT, packIds=[REPLACEMENT, PACK])
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)
    assert stores.read_setup_state(ORG)["packIds"] == [REPLACEMENT]

    revert_migration(ORG, applied.id, actor_id=ACTOR)

    state = stores.read_setup_state(ORG)
    assert state["packId"] == REPLACEMENT
    assert state["packIds"] == [REPLACEMENT, PACK]


def test_revert_appends_and_marks_the_original_reverted(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)

    reverted = revert_migration(ORG, applied.id, actor_id="user_two")

    assert reverted.kind == RECORD_REVERT
    assert reverted.reverts_migration_id == applied.id
    # Append-only: the apply row is still there, and now reports its own undoing.
    assert len(stores.records(ORG)) == 2
    original = get_migration(ORG, applied.id)
    assert original.reverted is True
    assert original.reverted_by == "user_two"
    assert original.reverted_at == reverted.at


def test_reverting_twice_is_refused(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)
    revert_migration(ORG, applied.id, actor_id=ACTOR)

    with pytest.raises(PackMigrationConflict):
        revert_migration(ORG, applied.id, actor_id=ACTOR)


def test_a_revert_row_cannot_itself_be_reverted(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)
    reverted = revert_migration(ORG, applied.id, actor_id=ACTOR)

    with pytest.raises(PackMigrationConflict):
        revert_migration(ORG, reverted.id, actor_id=ACTOR)


def test_revert_refuses_to_discard_a_later_edit(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)

    # The customer edits the migrated selection themselves.
    edited = stores.read_setup_state(ORG)
    edited["packIds"] = [REPLACEMENT, "service_cloud"]
    stores.write_setup_state(ORG, edited)

    with pytest.raises(PackMigrationConflict) as exc:
        revert_migration(ORG, applied.id, actor_id=ACTOR)
    assert "packIds" in str(exc.value)
    assert stores.read_setup_state(ORG)["packIds"] == [REPLACEMENT, "service_cloud"]


def test_forced_revert_restores_anyway(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)
    edited = stores.read_setup_state(ORG)
    edited["packIds"] = [REPLACEMENT, "service_cloud"]
    stores.write_setup_state(ORG, edited)

    revert_migration(ORG, applied.id, actor_id=ACTOR, force=True)

    assert stores.read_setup_state(ORG)["packIds"] == [PACK]


def test_revert_of_an_unknown_migration_is_not_found(stores):
    with pytest.raises(PackMigrationNotFound):
        revert_migration(ORG, "pmig_missing", actor_id=ACTOR)


def test_a_migration_is_not_visible_to_another_org(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)

    with pytest.raises(PackMigrationNotFound):
        get_migration(OTHER_ORG, applied.id)


# ── Ledger ────────────────────────────────────────────────────────────────────


def test_history_is_newest_first(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)
    reverted = revert_migration(ORG, applied.id, actor_id=ACTOR)

    history = migration_history(ORG)

    assert [record.id for record in history] == [reverted.id, applied.id]


def test_history_of_an_org_with_no_migrations_is_empty(stores):
    assert migration_history(ORG) == []


def test_record_serialises_for_the_api(stores, deprecated_pack):
    deprecated_pack()
    _seed(stores)
    applied = apply_migration(ORG, PACK, actor_id=ACTOR)

    payload = applied.to_dict()

    assert payload["packId"] == PACK
    assert payload["replacementPackId"] == REPLACEMENT
    assert payload["changes"][0]["surface"] == SURFACE_SETUP_STATE
    assert payload["kind"] == RECORD_APPLY
    assert payload["reverted"] is False


# ── Structural ────────────────────────────────────────────────────────────────


def test_the_module_has_no_delete_path():
    """2.0-C1 T4's never-delete discipline, applied to the migration ledger.

    Reverting must APPEND. A ledger a revert can erase cannot answer "what did this
    org do", which is exactly what AT-846 has to read from it.
    """
    import inspect

    source = inspect.getsource(pack_migration).upper()
    for forbidden in ("DELETE FROM", "DROP TABLE", "TRUNCATE"):
        assert forbidden not in source
    defined = {
        name
        for name, value in vars(pack_migration).items()
        if callable(value) and not name.startswith("__")
    }
    assert not {
        name for name in defined if "delete" in name.lower() or "purge" in name.lower()
    }


def test_the_store_contract_exposes_no_delete_operation():
    names = {
        name
        for name in dir(pack_migration.PackMigrationStore)
        if not name.startswith("_")
    }
    assert not {name for name in names if "delete" in name or "remove" in name}
