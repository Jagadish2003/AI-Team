"""2.0-C2 T5 (AT-835) — certification expiry, DB-free.

Sub-task scope: *certification carries a review date and platform-version scope; a
pack certified against an older platform shows as `review due` rather than silently
retaining its badge.*

Parent-story criterion discharged here:

  * AC4 — a pack certified against an out-of-scope platform version displays
    `review due`.

Two independent rules, and the thing that makes the feature honest is what it does
NOT do:

  * **flags, never revokes** — a review-due pack keeps its verified level, still
    surfaces it, and still activates (including under a T4 "Certified only" policy).
    Auto-revoking on a date would take working packs offline with no human decision.
  * **never applies to a pack that was never reviewed** — community, and a pack whose
    claim was downgraded, are never "due"; saying so would imply a badge they do not
    hold.

Every date-dependent assertion injects ``as_of``. A test that read the wall clock
would pass today and fail on a date nobody chose — the exact failure mode this
feature would otherwise introduce into CI.
"""
from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_certification import (  # noqa: E402
    DEFAULT_REVIEW_INTERVAL_DAYS,
    LEVEL_CERTIFIED,
    LEVEL_COMMUNITY,
    REVIEW_DUE_DATE_UNREADABLE,
    REVIEW_DUE_PLATFORM_MOVED,
    REVIEW_DUE_REVIEW_AGED,
    REVIEW_DUE_UNDECLARED,
    REVIEW_INTERVAL_ENV_VAR,
    certification_badge,
    certification_summary,
    get_pack_certification,
    parse_review_date,
    review_due_on,
    review_interval_days,
)
from discovery.packs.pack_config import list_packs  # noqa: E402
from discovery.packs.platform_capabilities import PLATFORM_VERSION  # noqa: E402

PACK = "cloud_ops"
REVIEWED_ON = date(2026, 7, 31)          # the shipped packs' review date
WITHIN = date(2027, 1, 1)                # comfortably inside the interval
JUST_DUE = date(2027, 8, 1)              # one day past reviewDate + 365
LONG_AFTER = date(2029, 1, 1)


def _certification(**kwargs):
    kwargs.setdefault("as_of", WITHIN)
    return get_pack_certification(PACK, **kwargs)


@pytest.fixture
def reviewed_at(monkeypatch):
    """Rewrite the pack's review date / reviewed-against version for a test.

    The signature will not verify afterwards, so the pack downgrades to Community —
    which is wrong for testing a rule that only applies to CERTIFIED packs. Tests
    that need a specific date therefore call the rule helpers directly, and this
    fixture is used only where a downgrade is itself the point.
    """

    def _apply(**fields):
        declaration = dict(pack_config.PACK_REGISTRY[PACK]["certification"])
        declaration.update(fields)
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[PACK], "certification", declaration
        )

    return _apply


# ── The interval ──────────────────────────────────────────────────────────────


def test_default_interval_is_a_year():
    assert DEFAULT_REVIEW_INTERVAL_DAYS == 365
    assert review_interval_days() == 365


def test_interval_is_configurable_per_deployment(monkeypatch):
    """A federal boundary may want a shorter cycle than the default."""
    monkeypatch.setenv(REVIEW_INTERVAL_ENV_VAR, "180")
    assert review_interval_days() == 180


def test_interval_zero_disables_the_date_rule(monkeypatch):
    monkeypatch.setenv(REVIEW_INTERVAL_ENV_VAR, "0")
    assert review_interval_days() == 0
    # Even long after the review, only the date rule is off — the pack is current.
    assert get_pack_certification(PACK, as_of=LONG_AFTER).review_due is False


@pytest.mark.parametrize("value", ["not-a-number", "-30", ""])
def test_a_malformed_interval_falls_back_to_the_default(monkeypatch, value):
    """A mistyped interval must not silently switch expiry off."""
    monkeypatch.setenv(REVIEW_INTERVAL_ENV_VAR, value)
    assert review_interval_days() == DEFAULT_REVIEW_INTERVAL_DAYS


# ── The review date ───────────────────────────────────────────────────────────


def test_review_date_parses_an_iso_date():
    assert parse_review_date("2026-07-31") == REVIEWED_ON


@pytest.mark.parametrize("value", ["", None, "31/07/2026", "soon", "2026-13-45"])
def test_an_unparseable_review_date_is_none(value):
    assert parse_review_date(value) is None


def test_due_date_is_the_review_date_plus_the_interval():
    assert review_due_on("2026-07-31", interval_days=365) == "2027-07-31"


def test_due_date_is_none_when_the_rule_is_disabled():
    assert review_due_on("2026-07-31", interval_days=0) is None


def test_due_date_is_none_for_an_unreadable_review_date():
    assert review_due_on("whenever") is None


# ── Rule 1: platform scope (AC4) ──────────────────────────────────────────────


def test_a_pack_reviewed_against_an_older_platform_is_review_due():
    """AC4, directly."""
    certification = get_pack_certification(
        PACK, platform_version="2.1.0", as_of=WITHIN
    )
    assert certification.review_due is True
    assert certification.review_due_reason == REVIEW_DUE_PLATFORM_MOVED
    assert "2.0.0" in certification.review_due_detail
    assert "2.1.0" in certification.review_due_detail


def test_a_patch_level_platform_bump_is_not_review_due():
    assert (
        get_pack_certification(PACK, platform_version="2.0.9", as_of=WITHIN).review_due
        is False
    )


def test_an_unreadable_reviewed_against_version_is_review_due(reviewed_at):
    from discovery.packs.pack_certification import _review_due_reasons

    assert _review_due_reasons(
        LEVEL_CERTIFIED, "", PLATFORM_VERSION, "2026-07-31", WITHIN, None
    ) == [REVIEW_DUE_UNDECLARED]


# ── Rule 2: review age ────────────────────────────────────────────────────────


def test_a_certification_inside_the_interval_is_current():
    assert _certification().review_due is False


def test_a_certification_past_the_interval_is_review_due():
    certification = get_pack_certification(PACK, as_of=JUST_DUE)
    assert certification.review_due is True
    assert certification.review_due_reason == REVIEW_DUE_REVIEW_AGED
    assert "2026-07-31" in certification.review_due_detail
    assert "2027-07-31" in certification.review_due_detail


def test_the_boundary_day_itself_is_not_yet_due():
    """Due ON the date means due AFTER it — a review is current for its full term."""
    assert get_pack_certification(PACK, as_of=date(2027, 7, 31)).review_due is False
    assert get_pack_certification(PACK, as_of=date(2027, 8, 1)).review_due is True


def test_a_shorter_configured_interval_brings_the_date_forward():
    certification = get_pack_certification(
        PACK, as_of=date(2027, 2, 1), review_interval_days_override=180
    )
    assert certification.review_due is True
    assert certification.review_due_on == "2027-01-27"


def test_an_unreadable_review_date_on_a_certified_pack_is_review_due():
    from discovery.packs.pack_certification import _review_due_reasons

    assert REVIEW_DUE_DATE_UNREADABLE in _review_due_reasons(
        LEVEL_CERTIFIED, PLATFORM_VERSION, PLATFORM_VERSION, "", WITHIN, None
    )


# ── Both rules at once ────────────────────────────────────────────────────────


def test_a_certification_can_be_due_for_both_reasons():
    """Reporting only the first would understate how stale it is."""
    certification = get_pack_certification(
        PACK, platform_version="2.1.0", as_of=JUST_DUE
    )
    assert certification.review_due_reasons == [
        REVIEW_DUE_PLATFORM_MOVED,
        REVIEW_DUE_REVIEW_AGED,
    ]
    detail = certification.review_due_detail
    assert "2.1.0" in detail          # names the platform rule
    assert "2026-07-31" in detail     # …and the age rule


# ── Flags, never revokes ──────────────────────────────────────────────────────


def test_a_review_due_pack_keeps_its_verified_level():
    certification = get_pack_certification(PACK, as_of=LONG_AFTER)
    assert certification.review_due is True
    assert certification.effective_level == LEVEL_CERTIFIED
    assert certification.signature_verified is True
    assert certification.downgraded is False
    assert "review due" in certification.status_label


def test_a_review_due_pack_still_activates_under_a_certified_only_policy():
    """The T4 policy gate must not treat "due for review" as "not certified".

    Auto-blocking on a date would take working packs offline without a human
    decision — the story says *flagged*, not revoked.
    """
    from app.pack_certification_policy import (
        InMemoryPackCertificationPolicyStore,
        assert_selection_permitted,
        set_certification_policy,
        set_policy_store,
    )
    from app.pack_state import InMemoryPackStateStore, set_pack_state_store

    set_policy_store(InMemoryPackCertificationPolicyStore())
    set_pack_state_store(InMemoryPackStateStore())
    try:
        set_certification_policy("org_expiry", LEVEL_CERTIFIED, actor_id="owner")
        # The badge the policy reads is resolved at "now", and the pack is Certified
        # regardless of review-due state — which is the property under test.
        assert assert_selection_permitted("org_expiry", [PACK]).restricted is True
    finally:
        set_policy_store(None)
        set_pack_state_store(None)


def test_a_community_pack_is_never_review_due():
    from discovery.packs.pack_certification import _review_due_reasons

    assert (
        _review_due_reasons(
            LEVEL_COMMUNITY, "1.0.0", "9.9.9", "2000-01-01", LONG_AFTER, None
        )
        == []
    )


def test_a_downgraded_pack_is_never_review_due(monkeypatch):
    """It is Community now; flagging it would imply a badge it does not hold."""
    declaration = dict(pack_config.PACK_REGISTRY[PACK]["certification"])
    declaration["signature"] = {"keyId": "", "algorithm": "", "value": ""}
    monkeypatch.setitem(pack_config.PACK_REGISTRY[PACK], "certification", declaration)

    certification = get_pack_certification(PACK, as_of=LONG_AFTER)
    assert certification.effective_level == LEVEL_COMMUNITY
    assert certification.review_due is False
    assert certification.review_due_on is None


# ── Warning before the flag flips ─────────────────────────────────────────────


def test_a_current_certification_reports_when_it_falls_due():
    certification = _certification()
    assert certification.review_due is False
    assert certification.review_due_on == "2027-07-31"
    assert "Next review due 2027-07-31" in certification.summary


def test_the_summary_names_the_rule_once_due():
    summary = get_pack_certification(PACK, as_of=JUST_DUE).summary
    assert "Review due" in summary
    assert "last reviewed on 2026-07-31" in summary


# ── Surfacing ─────────────────────────────────────────────────────────────────


def test_the_badge_carries_the_reason_and_the_due_date():
    badge = certification_badge(PACK, as_of=JUST_DUE)
    assert badge["reviewDue"] is True
    assert badge["reviewDueDetail"]
    assert badge["reviewDueOn"] == "2027-07-31"
    # …and the LEVEL is untouched: flagged, not downgraded.
    assert badge["level"] == LEVEL_CERTIFIED


def test_a_current_badge_carries_the_due_date_with_no_flag():
    badge = certification_badge(PACK, as_of=WITHIN)
    assert badge["reviewDue"] is False
    assert badge["reviewDueDetail"] is None
    assert badge["reviewDueOn"] == "2027-07-31"


def test_run_health_reports_the_reason_and_the_due_date():
    from app.health_aggregation import _certification_fields

    fields = _certification_fields(PACK)
    assert fields["certification_review_due_on"] == "2027-07-31"
    assert "certification_review_due_detail" in fields


def test_the_summary_reports_when_each_pack_falls_due():
    summary = certification_summary([PACK, "ncino"])
    assert summary["reviewDueOn"][PACK] == "2027-07-31"


# ── The shipped packs ─────────────────────────────────────────────────────────


def test_every_shipped_pack_has_a_computable_due_date():
    """Not "is it due" — that would be a time bomb in CI on a date nobody chose.

    What must hold is that every shipped certification carries a READABLE review
    date, so its expiry is knowable at all. When one does fall due, the badge says
    so at runtime and `sign_pack_certifications.py --check` reports it.
    """
    for pack_id in list_packs():
        certification = get_pack_certification(pack_id, as_of=REVIEWED_ON)
        assert certification.review_due_on, (
            f"pack '{pack_id}' has no computable review-due date — check its "
            f"reviewDate is a parseable ISO date"
        )
        assert parse_review_date(certification.review_date) is not None


def test_no_shipped_pack_is_due_on_its_own_review_date():
    """The weakest honest invariant: a certification is current the day it is made.

    Deliberately anchored to the review date rather than to today, so this test can
    never start failing because time passed.
    """
    for pack_id in list_packs():
        reviewed_on = parse_review_date(
            get_pack_certification(pack_id).review_date
        )
        assert get_pack_certification(pack_id, as_of=reviewed_on).review_due is False
