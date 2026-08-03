"""2.0-C4 T2 (AT-843) — deprecation notice surfacing, DB-free.

Sub-task scope: *orgs using a deprecated pack see it at run configuration, in run
health, and on the pack's findings — with the date it stops being supported and what
replaces it.*

Parent-story criterion this discharges (backend half):

  * AC1 — deprecating a pack surfaces notice at run configuration, run health, and
    on its findings, with date and replacement.

The load-bearing rules carried through every surface:

  1. All three surfaces read the SAME notice, built once in
     ``discovery/packs/pack_deprecation.py`` (AT-842), so they cannot word the same
     deprecation differently.
  2. Every surface carries the DATE support ends and the REPLACEMENT — the two
     things AC1 names explicitly.
  3. A pack that is not deprecated surfaces NOTHING. There is no "not deprecated"
     object to render, which is what stops an empty banner appearing on every
     healthy pack.
  4. Every surface is fail-soft, and always in the same direction: a missing notice,
     never a notice invented for a live pack.

The UI half is pinned in
``frontend/src/__tests__/PackDeprecationSurfacing.test.tsx``.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app.health_aggregation import _deprecation_fields  # noqa: E402
from app.opportunity_display import (  # noqa: E402
    with_display_all,
    with_display_title,
    with_pack_deprecation,
    with_pack_deprecations,
)
from app.pack_activation import deprecation_snapshot  # noqa: E402
from app.pack_state import (  # noqa: E402
    InMemoryPackStateStore,
    pack_state_view,
    set_pack_state_store,
)
from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_config import DEPRECATION_KEY, list_packs  # noqa: E402
from discovery.packs.pack_deprecation import (  # noqa: E402
    PHASE_GRACE,
    PHASE_GRACE_EXPIRED,
    STATUS_DEPRECATED,
    deprecation_notice,
)

PACK = "cloud_ops"
REPLACEMENT = "enterprise_ops"
ORG = "org_at843"

#: The grace period runs to 2026-09-29, comfortably in the future relative to the
#: declared dates below. Surfaces read the clock (they have no `as_of` seam), so the
#: fixture is dated to keep "in grace" true for years rather than until next month.
GRACE_ENDS_ON = "2099-09-29"
DEPRECATED_ON = "2026-07-01"
REASON = "Superseded by the Enterprise Operations pack."


@pytest.fixture(autouse=True)
def in_memory_pack_state():
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


def _declaration(**overrides):
    declaration = {
        "status": STATUS_DEPRECATED,
        "reason": REASON,
        "deprecatedOn": DEPRECATED_ON,
        "graceEndsOn": GRACE_ENDS_ON,
        "replacement": {"packId": REPLACEMENT, "minVersion": "1.0.0"},
    }
    declaration.update(overrides)
    return declaration


@pytest.fixture
def deprecated_pack(monkeypatch):
    """Deprecate a real registered pack for the duration of one test."""

    def _deprecate(**overrides):
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[PACK],
            DEPRECATION_KEY,
            _declaration(**overrides),
        )
        return PACK

    return _deprecate


def _opp(pack_id=PACK, **overrides):
    opp = {
        "id": "opp_1",
        "title": "Recurring resolution loop",
        "packId": pack_id,
        "packVersion": "1.2.0",
        "impact": 7,
        "effort": 3,
        "tier": "Quick Win",
        "confidence": "HIGH",
    }
    opp.update(overrides)
    return opp


# ── Baseline: nothing is deprecated, so nothing is surfaced ───────────────────


def test_no_shipped_pack_surfaces_a_notice_today():
    """The healthy default. Every surface stays silent."""
    for pack_id in list_packs():
        assert deprecation_notice(pack_id) is None
        assert _deprecation_fields(pack_id) == {}
    assert all(row["deprecation"] is None for row in pack_state_view(ORG))
    assert "packDeprecated" not in with_pack_deprecation(_opp())


# ── Surface 1: run configuration ──────────────────────────────────────────────


def test_run_configuration_shows_the_notice_with_date_and_replacement(
    deprecated_pack,
):
    """GET /api/packs/state is what the pack picker reads — AC1's first surface."""
    deprecated_pack()
    row = next(row for row in pack_state_view(ORG) if row["packId"] == PACK)
    notice = row["deprecation"]

    assert notice is not None
    assert notice["phase"] == PHASE_GRACE
    assert notice["reason"] == REASON
    assert notice["graceEndsOn"] == GRACE_ENDS_ON          # AC1: the date
    assert notice["replacementPackId"] == REPLACEMENT      # AC1: the replacement
    assert REPLACEMENT in notice["summary"]
    assert GRACE_ENDS_ON in notice["summary"]


def test_run_configuration_leaves_undeprecated_packs_null(deprecated_pack):
    deprecated_pack()
    view = {row["packId"]: row for row in pack_state_view(ORG)}
    assert view[PACK]["deprecation"] is not None
    assert view["ncino"]["deprecation"] is None


def test_run_configuration_row_keeps_its_other_lifecycle_fields(deprecated_pack):
    """Deprecation is ADDITIVE — the 2.0-C1 and 2.0-C2 fields are untouched."""
    deprecated_pack()
    row = next(row for row in pack_state_view(ORG) if row["packId"] == PACK)
    for key in ("state", "packVersion", "pinnedVersion", "effectiveVersion",
                "availableVersions", "registered", "certification"):
        assert key in row


def test_orphaned_row_reports_no_deprecation():
    """A pack the registry no longer declares must not wear the DEFAULT pack's notice."""
    from app.pack_state import disable_pack

    disable_pack(ORG, PACK, actor_id="owner")
    removed = pack_config.PACK_REGISTRY.pop(PACK)
    try:
        row = next(row for row in pack_state_view(ORG) if row["packId"] == PACK)
        assert row["registered"] is False
        assert row["deprecation"] is None
    finally:
        pack_config.PACK_REGISTRY[PACK] = removed


def test_run_configuration_is_fail_soft(monkeypatch):
    """A deprecation-metadata problem must not blank the pack picker."""
    import app.pack_state as pack_state

    monkeypatch.setattr(
        pack_state, "_safe_deprecation_notices", dict
    )
    rows = pack_state_view(ORG)
    assert rows and all(row["deprecation"] is None for row in rows)


# ── Surface 2: run health ─────────────────────────────────────────────────────


def test_run_health_row_reports_the_notice_with_date_and_replacement(
    deprecated_pack,
):
    deprecated_pack()
    fields = _deprecation_fields(PACK)

    assert fields["deprecated"] is True
    assert fields["deprecation_phase"] == PHASE_GRACE
    assert fields["deprecation_reason"] == REASON
    assert fields["deprecation_ends_on"] == GRACE_ENDS_ON        # AC1: the date
    assert fields["deprecation_replacement_pack_id"] == REPLACEMENT  # AC1: the path
    assert fields["deprecation_label"].startswith("Deprecated")
    assert fields["deprecation_notice"]


def test_run_health_reports_an_expired_grace(deprecated_pack):
    deprecated_pack(graceEndsOn="2026-07-31")
    fields = _deprecation_fields(PACK)
    assert fields["deprecation_phase"] == PHASE_GRACE_EXPIRED
    assert fields["deprecation_days_remaining"] == 0


def test_run_health_states_an_open_ended_grace_rather_than_implying_a_date(
    deprecated_pack,
):
    """"No removal date announced" is a real answer the panel must be able to give."""
    deprecated_pack(graceEndsOn="")
    fields = _deprecation_fields(PACK)
    assert fields["deprecated"] is True
    assert fields["deprecation_ends_on"] is None
    assert fields["deprecation_days_remaining"] is None


def test_run_health_row_for_a_removed_pack_reports_nothing():
    """``get_pack()`` resolves an unknown id to the default pack — wrong here."""
    assert _deprecation_fields("a_pack_that_was_removed") == {}


def test_run_health_row_is_fail_soft(monkeypatch):
    import discovery.packs.pack_deprecation as deprecation_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(deprecation_module, "deprecation_notice", boom)
    assert _deprecation_fields(PACK) == {}


def test_run_health_panel_rows_carry_the_fields(deprecated_pack, monkeypatch):
    """The fields reach the actual packs-panel row, not just the helper."""
    import app.health_aggregation as health

    # The row builder enriches from telemetry origin events; this file is DB-free,
    # and the enrichment is not what is under test here.
    monkeypatch.setattr(health, "_safe_range", lambda *_args, **_kwargs: [])
    deprecated_pack()
    view = health._packs_view_multi(
        ORG,
        "run_1",
        {"packId": PACK, "executedDetectorIds": ["d1"]},
        [{"packId": PACK, "packVersion": "1.2.0", "detectorsExecuted": ["d1"]}],
    )
    row = view["packs"][0]
    assert row["deprecated"] is True
    assert row["deprecation_ends_on"] == GRACE_ENDS_ON
    # Additive: the run's immutable execution facts are untouched.
    assert row["pack_version"] == "1.2.0"
    assert row["detectors"] == ["d1"]


# ── Surface 3: findings ───────────────────────────────────────────────────────


def test_finding_carries_the_notice_for_its_producing_pack(deprecated_pack):
    deprecated_pack()
    stamped = with_pack_deprecation(_opp())

    assert stamped["packDeprecated"] is True
    assert stamped["packDeprecationPhase"] == PHASE_GRACE
    assert stamped["packDeprecationEndsOn"] == GRACE_ENDS_ON            # the date
    assert stamped["packDeprecationReplacementPackId"] == REPLACEMENT   # the path
    assert stamped["packDeprecationLabel"].startswith("Deprecated")
    assert REASON in stamped["packDeprecationNotice"]


def test_finding_from_an_undeprecated_pack_is_untouched(deprecated_pack):
    deprecated_pack()
    other = _opp(pack_id="ncino")
    assert with_pack_deprecation(other) == other


def test_finding_stamping_is_additive(deprecated_pack):
    """Nothing else on the finding is touched — score, evidence, version stamp."""
    deprecated_pack()
    original = _opp()
    stamped = with_pack_deprecation(original)
    for key, value in original.items():
        assert stamped[key] == value


def test_finding_without_a_pack_stamp_is_returned_unchanged(deprecated_pack):
    deprecated_pack()
    original = {"id": "legacy", "title": "Pre-R16-B1 finding"}
    assert with_pack_deprecation(original) == original


def test_absent_date_and_replacement_are_omitted_not_blank(deprecated_pack):
    """A surface must never render "stops on " with nothing after it."""
    deprecated_pack(graceEndsOn="", replacement={})
    stamped = with_pack_deprecation(_opp())
    assert stamped["packDeprecated"] is True
    assert "packDeprecationEndsOn" not in stamped
    assert "packDeprecationReplacementPackId" not in stamped


def test_display_funnel_stamps_deprecation(deprecated_pack):
    """Every serve site routes through this funnel, so one wiring covers them all."""
    deprecated_pack()
    stamped = with_display_title(_opp())
    assert stamped["packDeprecated"] is True
    # …and the 2.0-C1 / 2.0-C2 stamps still land.
    assert stamped["packState"] == "active"
    assert stamped["packCertificationLevel"]


def test_list_helpers_stamp_every_finding(deprecated_pack):
    deprecated_pack()
    opps = [_opp(), _opp(pack_id="ncino")]
    for stamped in (with_display_all(opps), with_pack_deprecations(opps)):
        assert stamped[0]["packDeprecated"] is True
        assert "packDeprecated" not in stamped[1]


def test_list_resolution_reads_notices_once(monkeypatch, deprecated_pack):
    """A 200-finding list must cost ONE deprecation evaluation, not 200."""
    import app.opportunity_display as display

    deprecated_pack()
    calls = {"n": 0}
    real = display._resolve_pack_deprecations

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(display, "_resolve_pack_deprecations", counted)
    display.with_pack_deprecations([_opp() for _ in range(50)])
    assert calls["n"] == 1


def test_finding_stamping_is_fail_soft(monkeypatch, deprecated_pack):
    """An unresolvable notice omits the fields — the finding still serves."""
    import app.opportunity_display as display

    deprecated_pack()
    monkeypatch.setattr(display, "_resolve_pack_deprecations", dict)
    stamped = display.with_pack_deprecation(_opp())
    assert "packDeprecated" not in stamped
    assert stamped["packId"] == PACK


# ── The launch snapshot (the audit record beside the live surfaces) ───────────


def test_launch_snapshot_records_the_position_at_launch(deprecated_pack):
    deprecated_pack()
    snapshot = deprecation_snapshot([PACK, "ncino"])

    assert snapshot["evaluated"] == [PACK, "ncino"]
    assert snapshot["deprecated"] == [PACK]
    assert snapshot["inGrace"] == [PACK]
    assert snapshot["replacements"] == {PACK: REPLACEMENT}
    assert snapshot["packs"][0]["graceEndsOn"] == GRACE_ENDS_ON


def test_launch_snapshot_proves_a_clean_run_was_evaluated():
    """A run with nothing deprecated still records that it checked."""
    snapshot = deprecation_snapshot([PACK, "ncino"])
    assert snapshot["evaluated"] == [PACK, "ncino"]
    assert snapshot["deprecated"] == []


def test_launch_snapshot_is_fail_soft(monkeypatch):
    """A launch must never fail because a notice could not be resolved."""
    import discovery.packs.pack_deprecation as deprecation_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(deprecation_module, "deprecation_summary", boom)
    assert deprecation_snapshot([PACK]) == {}


# ── One notice, three surfaces ────────────────────────────────────────────────


def test_all_three_surfaces_report_the_same_notice(deprecated_pack):
    """The whole point of one shared builder: the wording cannot drift."""
    deprecated_pack()
    configuration = next(
        row for row in pack_state_view(ORG) if row["packId"] == PACK
    )["deprecation"]
    health = _deprecation_fields(PACK)
    finding = with_pack_deprecation(_opp())

    assert (
        configuration["summary"]
        == health["deprecation_notice"]
        == finding["packDeprecationNotice"]
    )
    assert (
        configuration["statusLabel"]
        == health["deprecation_label"]
        == finding["packDeprecationLabel"]
    )
    assert (
        configuration["graceEndsOn"]
        == health["deprecation_ends_on"]
        == finding["packDeprecationEndsOn"]
    )
    assert (
        configuration["replacementPackId"]
        == health["deprecation_replacement_pack_id"]
        == finding["packDeprecationReplacementPackId"]
    )
