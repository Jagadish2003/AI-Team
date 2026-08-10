"""2.0-C4 T1 (AT-842) — pack deprecation metadata.

Sub-task scope: *a pack version can be marked deprecated with a reason, a grace
period, and an optional replacement pack.*

Parent-story criteria this sub-task contributes to:

  * AC1 — the notice carries a DATE and a REPLACEMENT. This file pins the metadata
    and the notice text those three surfaces (run configuration, run health,
    findings) render; the surfacing itself is AT-843.
  * AC3 — the pack is in ``grace`` during the grace period and ``grace_expired``
    after it. This file pins the phase evaluation; acting on it (safe-disable, with
    history intact) is AT-845.

Also pinned: the shipped declarations stay honest. A pack that ships a deprecation
declaration with any named defect fails the build, so the tolerant runtime posture
(surface the notice anyway, never auto-disable on bad data) is a safety net rather
than the contract.

Pure-Python and offline — no DB and no credentials. Every date-dependent assertion
injects ``as_of`` so a grace-period test cannot become a time bomb the day its
fixture dates age out.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.packs.pack_config import (  # noqa: E402
    DEPRECATION_KEY,
    PACK_REGISTRY,
    get_pack_deprecation_declaration,
    list_packs,
)
from discovery.packs.pack_deprecation import (  # noqa: E402
    ISSUE_CONFLICTING_GRACE,
    ISSUE_INVALID_GRACE_PERIOD,
    ISSUE_INVALID_STATUS,
    ISSUE_MISSING_DEPRECATED_ON,
    ISSUE_MISSING_REASON,
    ISSUE_SELF_REPLACEMENT,
    ISSUE_UNKNOWN_REPLACEMENT,
    ISSUE_UNKNOWN_VERSION_SCOPE,
    ISSUE_UNREADABLE_DATE,
    PHASE_ACTIVE,
    PHASE_GRACE,
    PHASE_GRACE_EXPIRED,
    STATUS_ACTIVE,
    STATUS_DEPRECATED,
    deprecated_pack_ids,
    deprecation_notice,
    deprecation_notices,
    deprecation_summary,
    get_pack_deprecation,
    is_grace_expired,
    is_pack_deprecated,
    parse_deprecation_date,
    replacement_pack_id,
)

TEST_PACK = "at842_test_pack"
TEST_PACK_VERSION = "2.3.0"
TODAY = date(2026, 8, 3)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def registered_pack(monkeypatch):
    """Register a synthetic pack, returning a setter for its deprecation block."""

    def _register(deprecation, pack_id=TEST_PACK, version_history=None):
        pack = {
            "packId": pack_id,
            "packVersion": TEST_PACK_VERSION,
            "packName": "AT-842 Test Pack",
            "domain": "service_cloud",
            "pack_domain": "service_cloud",
            "compatibility": {
                "minPlatformVersion": "1.0.0",
                "maxPlatformVersion": None,
                "requiredConcepts": [],
                "optionalConcepts": [],
            },
            "detectors": [],
            "ui_labels_path": None,
            "llm_context": "test",
        }
        if deprecation is not None:
            pack[DEPRECATION_KEY] = deprecation
        if version_history is not None:
            pack["versionHistory"] = version_history
        monkeypatch.setitem(PACK_REGISTRY, pack_id, pack)
        return pack_id

    return _register


def _declaration(**overrides):
    """A complete, well-formed deprecation declaration."""
    declaration = {
        "status": STATUS_DEPRECATED,
        "versions": [],
        "reason": "Superseded by the Cloud Operations pack.",
        "deprecatedOn": "2026-07-01",
        "gracePeriodDays": 90,
        "replacement": {
            "packId": "cloud_ops",
            "minVersion": "1.2.0",
            "notes": "Reconnect the AWS and Azure event sources before migrating.",
        },
    }
    declaration.update(overrides)
    return declaration


# ── Structural: the shipped declarations stay honest ──────────────────────────


def test_every_shipped_deprecation_declaration_is_well_formed():
    """A declaration defect must fail the build, not surface to a customer."""
    for pack_id in list_packs():
        deprecation = get_pack_deprecation(pack_id)
        assert deprecation.issues == [], (
            f"pack '{pack_id}' ships a deprecation declaration with defects: "
            f"{deprecation.issues}"
        )


def test_undeclared_packs_are_not_deprecated():
    """No pack is deprecated by accident — the default is emphatically 'active'."""
    for pack_id in list_packs():
        if DEPRECATION_KEY in PACK_REGISTRY[pack_id]:
            continue
        deprecation = get_pack_deprecation(pack_id)
        assert deprecation.deprecated is False
        assert deprecation.phase == PHASE_ACTIVE
        assert deprecation.declared_status == STATUS_ACTIVE


def test_shipped_deprecations_name_a_registered_replacement():
    for pack_id in list_packs():
        deprecation = get_pack_deprecation(pack_id)
        if not deprecation.deprecated or not deprecation.has_replacement:
            continue
        assert deprecation.replacement_pack_id in PACK_REGISTRY


# ── Declaration normalisation ─────────────────────────────────────────────────


def test_undeclared_pack_reads_as_active(registered_pack):
    pack_id = registered_pack(None)
    declaration = get_pack_deprecation_declaration(pack_id)
    assert declaration["status"] == ""
    assert declaration["versions"] == []
    assert declaration["replacement"] == {
        "packId": "",
        "minVersion": "",
        "notes": "",
    }
    assert get_pack_deprecation(pack_id).deprecated is False


def test_partial_declaration_is_filled_not_rejected(registered_pack):
    pack_id = registered_pack({"status": STATUS_DEPRECATED, "reason": "  gone  "})
    declaration = get_pack_deprecation_declaration(pack_id)
    assert declaration["reason"] == "gone"
    assert declaration["graceEndsOn"] == ""
    assert declaration["gracePeriodDays"] is None


def test_scoped_versions_are_deduplicated_order_preservingly(registered_pack):
    pack_id = registered_pack(
        _declaration(versions=[" 1.1.0 ", "1.0.0", "1.1.0", "", 7])
    )
    assert get_pack_deprecation_declaration(pack_id)["versions"] == [
        "1.1.0",
        "1.0.0",
    ]


def test_unknown_pack_id_reads_the_default_packs_deprecation():
    """An unknown id resolves through get_pack(), exactly as it does everywhere."""
    assert get_pack_deprecation("no_such_pack").pack_id == get_pack_deprecation(
        None
    ).pack_id


def test_non_integer_grace_period_survives_normalisation(registered_pack):
    """The accessor must not coerce a defect away before it can be named."""
    pack_id = registered_pack(_declaration(gracePeriodDays="ninety"))
    assert get_pack_deprecation_declaration(pack_id)["gracePeriodDays"] == "ninety"


# ── The three declared facts: reason, grace period, replacement ───────────────


def test_a_pack_version_can_be_marked_deprecated_with_reason_grace_replacement(
    registered_pack,
):
    """AT-842's whole sentence, in one assertion block."""
    pack_id = registered_pack(_declaration())
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)

    assert deprecation.deprecated is True
    assert deprecation.version == TEST_PACK_VERSION
    assert deprecation.reason == "Superseded by the Cloud Operations pack."
    assert deprecation.deprecated_on == "2026-07-01"
    assert deprecation.grace_period_days == 90
    assert deprecation.grace_ends_on == "2026-09-29"
    assert deprecation.replacement_pack_id == "cloud_ops"
    assert deprecation.replacement_min_version == "1.2.0"
    assert deprecation.has_replacement is True
    assert deprecation.valid is True


def test_replacement_is_optional(registered_pack):
    pack_id = registered_pack(_declaration(replacement={}))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.deprecated is True
    assert deprecation.has_replacement is False
    assert deprecation.replacement_label == ""
    assert deprecation.issues == []
    assert "No replacement pack has been named." in deprecation.summary


def test_explicit_grace_end_date_is_used_verbatim(registered_pack):
    pack_id = registered_pack(
        _declaration(gracePeriodDays=None, graceEndsOn="2026-12-31")
    )
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.grace_ends_on == "2026-12-31"
    assert deprecation.grace_period_days is None
    assert deprecation.issues == []


def test_grace_with_no_end_date_is_open_ended_and_never_expires(registered_pack):
    """'Deprecated, no removal date announced yet' is a real state, not a defect."""
    pack_id = registered_pack(_declaration(gracePeriodDays=None))
    deprecation = get_pack_deprecation(pack_id, as_of=date(2099, 1, 1))
    assert deprecation.deprecated is True
    assert deprecation.open_ended_grace is True
    assert deprecation.grace_ends_on == ""
    assert deprecation.phase == PHASE_GRACE
    assert deprecation.grace_expired is False
    assert deprecation.days_remaining is None
    assert deprecation.issues == []


def test_days_remaining_counts_down_and_floors_at_zero(registered_pack):
    pack_id = registered_pack(_declaration())  # grace ends 2026-09-29
    assert get_pack_deprecation(
        pack_id, as_of=date(2026, 9, 19)
    ).days_remaining == 10
    assert get_pack_deprecation(
        pack_id, as_of=date(2026, 9, 29)
    ).days_remaining == 0
    assert get_pack_deprecation(
        pack_id, as_of=date(2026, 10, 30)
    ).days_remaining == 0


# ── Phases (parent-story AC3's metadata half) ─────────────────────────────────


def test_phase_is_grace_during_the_grace_period(registered_pack):
    pack_id = registered_pack(_declaration())
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.phase == PHASE_GRACE
    assert deprecation.in_grace is True
    assert deprecation.grace_expired is False
    assert is_grace_expired(pack_id, as_of=TODAY) is False


def test_the_last_day_of_grace_is_still_grace(registered_pack):
    pack_id = registered_pack(_declaration())
    assert (
        get_pack_deprecation(pack_id, as_of=date(2026, 9, 29)).phase == PHASE_GRACE
    )


def test_phase_becomes_expired_the_day_after_grace_ends(registered_pack):
    pack_id = registered_pack(_declaration())
    deprecation = get_pack_deprecation(pack_id, as_of=date(2026, 9, 30))
    assert deprecation.phase == PHASE_GRACE_EXPIRED
    assert deprecation.grace_expired is True
    assert deprecation.in_grace is False
    assert is_grace_expired(pack_id, as_of=date(2026, 9, 30)) is True


def test_a_pack_deprecated_before_its_start_date_is_still_deprecated(
    registered_pack,
):
    """The declaration is the fact; the date only positions it in the grace window.

    A future ``deprecatedOn`` is a scheduled notice, and hiding it until the day it
    lands is the opposite of notice.
    """
    pack_id = registered_pack(_declaration(deprecatedOn="2027-01-01"))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.deprecated is True
    assert deprecation.phase == PHASE_GRACE


def test_not_deprecated_pack_reports_no_phase_or_dates(registered_pack):
    pack_id = registered_pack(_declaration(status=STATUS_ACTIVE))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.deprecated is False
    assert deprecation.phase == PHASE_ACTIVE
    assert deprecation.grace_ends_on == ""
    assert deprecation.days_remaining is None
    assert is_pack_deprecated(pack_id, as_of=TODAY) is False


# ── Version scoping ───────────────────────────────────────────────────────────


def test_an_empty_scope_deprecates_every_version(registered_pack):
    pack_id = registered_pack(_declaration(versions=[]))
    assert get_pack_deprecation(pack_id, as_of=TODAY).deprecated is True
    assert (
        get_pack_deprecation(pack_id, version="1.1.0", as_of=TODAY).deprecated
        is True
    )


def test_a_scoped_deprecation_covers_only_the_named_versions(registered_pack):
    pack_id = registered_pack(
        _declaration(versions=["1.1.0"]),
        version_history=[{"version": "1.1.0", "detectors": []}],
    )
    current = get_pack_deprecation(pack_id, as_of=TODAY)
    archived = get_pack_deprecation(pack_id, version="1.1.0", as_of=TODAY)

    assert current.deprecated is False
    assert current.version == TEST_PACK_VERSION
    assert archived.deprecated is True
    assert archived.version == "1.1.0"
    assert archived.applies_to_versions == ["1.1.0"]
    assert archived.issues == []


def test_a_scope_naming_an_undeclared_version_is_flagged(registered_pack):
    """A typo'd scope silently deprecates nothing — the loudest possible failure."""
    pack_id = registered_pack(_declaration(versions=["9.9.9"]))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.deprecated is False
    assert ISSUE_UNKNOWN_VERSION_SCOPE in deprecation.issues


# ── Declaration defects: notice loudly, never auto-disable on bad data ────────


def test_a_missing_reason_still_surfaces_the_notice(registered_pack):
    pack_id = registered_pack(_declaration(reason=""))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.deprecated is True
    assert ISSUE_MISSING_REASON in deprecation.issues


def test_a_missing_deprecation_date_still_surfaces_the_notice(registered_pack):
    pack_id = registered_pack(_declaration(deprecatedOn=""))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.deprecated is True
    assert ISSUE_MISSING_DEPRECATED_ON in deprecation.issues
    # No start date ⇒ nothing to derive an end from ⇒ open-ended, never expired.
    assert deprecation.grace_ends_on == ""
    assert deprecation.grace_expired is False


def test_an_unreadable_date_never_expires_the_grace(registered_pack):
    pack_id = registered_pack(
        _declaration(gracePeriodDays=None, graceEndsOn="last Tuesday")
    )
    deprecation = get_pack_deprecation(pack_id, as_of=date(2099, 1, 1))
    assert ISSUE_UNREADABLE_DATE in deprecation.issues
    assert deprecation.grace_expired is False
    assert deprecation.phase == PHASE_GRACE


@pytest.mark.parametrize("bad", ["ninety", -30, 12.5, True])
def test_an_invalid_grace_period_never_expires_the_grace(registered_pack, bad):
    pack_id = registered_pack(_declaration(gracePeriodDays=bad))
    deprecation = get_pack_deprecation(pack_id, as_of=date(2099, 1, 1))
    assert ISSUE_INVALID_GRACE_PERIOD in deprecation.issues
    assert deprecation.grace_ends_on == ""
    assert deprecation.grace_expired is False


def test_conflicting_grace_declarations_take_the_later_date(registered_pack):
    """A declaration mistake must never SHORTEN a customer's grace period."""
    pack_id = registered_pack(
        _declaration(gracePeriodDays=90, graceEndsOn="2026-08-01")
    )
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert ISSUE_CONFLICTING_GRACE in deprecation.issues
    assert deprecation.grace_ends_on == "2026-09-29"


def test_agreeing_grace_declarations_are_not_a_conflict(registered_pack):
    pack_id = registered_pack(
        _declaration(gracePeriodDays=90, graceEndsOn="2026-09-29")
    )
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.issues == []
    assert deprecation.grace_ends_on == "2026-09-29"


def test_an_unregistered_replacement_is_dropped_and_named(registered_pack):
    """A path to a pack that does not exist is worse than no path at all."""
    pack_id = registered_pack(
        _declaration(replacement={"packId": "pack_that_never_shipped"})
    )
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert ISSUE_UNKNOWN_REPLACEMENT in deprecation.issues
    assert deprecation.has_replacement is False
    assert replacement_pack_id(pack_id) is None


def test_a_pack_cannot_replace_itself(registered_pack):
    pack_id = registered_pack(_declaration(replacement={"packId": TEST_PACK}))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert ISSUE_SELF_REPLACEMENT in deprecation.issues
    assert deprecation.has_replacement is False


def test_an_unrecognised_status_is_flagged_and_read_as_a_notice(registered_pack):
    pack_id = registered_pack(_declaration(status="sunsetting"))
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert ISSUE_INVALID_STATUS in deprecation.issues
    assert deprecation.deprecated is True


def test_a_populated_block_with_no_status_is_read_as_deprecated(registered_pack):
    """Forgetting ``status`` must not suppress a notice that is plainly there."""
    declaration = _declaration()
    declaration.pop("status")
    pack_id = registered_pack(declaration)
    assert get_pack_deprecation(pack_id, as_of=TODAY).deprecated is True


def test_evaluation_never_raises(registered_pack):
    pack_id = registered_pack(
        {
            "status": 42,
            "versions": "1.1.0",
            "reason": None,
            "deprecatedOn": [],
            "gracePeriodDays": {"days": 30},
            "graceEndsOn": 7,
            "replacement": "cloud_ops",
        }
    )
    deprecation = get_pack_deprecation(pack_id, as_of=TODAY)
    assert deprecation.pack_id == pack_id
    assert deprecation.grace_expired is False


# ── Notice text (AC1's "date and replacement") ────────────────────────────────


def test_the_notice_names_the_reason_the_date_and_the_replacement(registered_pack):
    pack_id = registered_pack(_declaration())
    summary = get_pack_deprecation(pack_id, as_of=TODAY).summary
    assert "Superseded by the Cloud Operations pack." in summary
    assert "2026-07-01" in summary       # deprecated on
    assert "2026-09-29" in summary       # grace ends
    assert "cloud_ops" in summary        # replacement
    assert "Cloud Operations" in summary  # replacement, named for a human


def test_the_expired_notice_says_history_is_intact(registered_pack):
    pack_id = registered_pack(_declaration())
    summary = get_pack_deprecation(pack_id, as_of=date(2026, 10, 1)).summary
    assert "grace period ended on 2026-09-29" in summary
    assert "history remain intact" in summary


def test_status_labels_distinguish_the_three_phases(registered_pack):
    pack_id = registered_pack(_declaration())
    assert get_pack_deprecation(pack_id, as_of=TODAY).status_label == (
        "Deprecated — runs until 2026-09-29"
    )
    assert (
        get_pack_deprecation(pack_id, as_of=date(2026, 10, 1)).status_label
        == "Deprecated — grace period ended"
    )
    active = registered_pack(None, pack_id="at842_active_pack")
    assert get_pack_deprecation(active).status_label == "Active"


# ── Surfacing projections ─────────────────────────────────────────────────────


def test_a_non_deprecated_pack_has_no_notice(registered_pack):
    pack_id = registered_pack(None)
    assert deprecation_notice(pack_id) is None


def test_the_compact_notice_carries_what_a_surface_renders(registered_pack):
    pack_id = registered_pack(_declaration())
    notice = deprecation_notice(pack_id, as_of=TODAY)
    assert notice == {
        "packId": pack_id,
        "version": TEST_PACK_VERSION,
        "phase": PHASE_GRACE,
        "label": "Deprecated",
        "statusLabel": "Deprecated — runs until 2026-09-29",
        "reason": "Superseded by the Cloud Operations pack.",
        "deprecatedOn": "2026-07-01",
        "graceEndsOn": "2026-09-29",
        "daysRemaining": 57,
        "replacementPackId": "cloud_ops",
        "replacementLabel": "Cloud Operations (cloud_ops v1.2.0+)",
        "summary": get_pack_deprecation(pack_id, as_of=TODAY).summary,
    }


def test_notices_map_contains_only_deprecated_packs(registered_pack):
    deprecated = registered_pack(_declaration())
    active = registered_pack(None, pack_id="at842_active_pack")
    notices = deprecation_notices([deprecated, active], as_of=TODAY)
    assert set(notices) == {deprecated}
    assert deprecated_pack_ids([deprecated, active], as_of=TODAY) == [deprecated]


def test_summary_proves_it_evaluated_every_selected_pack(registered_pack):
    deprecated = registered_pack(_declaration())
    active = registered_pack(None, pack_id="at842_active_pack")
    summary = deprecation_summary([deprecated, active, deprecated], as_of=TODAY)

    assert summary["evaluatedOn"] == "2026-08-03"
    assert summary["evaluated"] == [deprecated, active]
    assert summary["deprecated"] == [deprecated]
    assert summary["inGrace"] == [deprecated]
    assert summary["graceExpired"] == []
    assert summary["replacements"] == {deprecated: "cloud_ops"}
    assert [entry["packId"] for entry in summary["packs"]] == [deprecated]


def test_summary_reports_an_expired_grace(registered_pack):
    pack_id = registered_pack(_declaration())
    summary = deprecation_summary([pack_id], as_of=date(2026, 10, 1))
    assert summary["graceExpired"] == [pack_id]
    assert summary["inGrace"] == []


def test_summary_over_the_whole_registry_is_clean_today():
    """No shipped pack is deprecated, so the default snapshot is empty."""
    summary = deprecation_summary()
    assert summary["deprecated"] == []
    assert set(summary["evaluated"]) == set(list_packs())


# ── Date parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-07-01", date(2026, 7, 1)),
        ("2026-07-01T12:00:00Z", date(2026, 7, 1)),
        ("  2026-07-01  ", date(2026, 7, 1)),
        ("01/07/2026", None),
        ("", None),
        (None, None),
    ],
)
def test_deprecation_dates_are_parsed_strictly(value, expected):
    assert parse_deprecation_date(value) == expected
