"""2.0-C2 T3 (AT-833) — certification level surfacing, DB-free.

Sub-task scope: *level is visible wherever a pack is selected, activated, or
attributed — including on findings the pack produced and in exported reports, so a
board paper says which level of pack produced a claim.*

Parent-story criterion discharged here:

  * AC2 — level is displayed at selection, activation, on findings, and in exports.

The load-bearing rule carried through every surface: what is displayed is the
EFFECTIVE (signature-verified) level. A pack claiming Certified whose signature does
not verify must read as Community at selection, at activation, on its findings, and
in an export — all at once (2.0-C2 AC1). Each surface is tested against a seeded
unsigned claim for exactly that.

The UI half is pinned in
``frontend/src/__tests__/PackCertificationSurfacing.test.tsx``.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app.executive_report_engine import (  # noqa: E402
    build_executive_report,
    pack_certifications,
)
from app.opportunity_display import (  # noqa: E402
    with_display_all,
    with_display_title,
    with_pack_certification,
    with_pack_certifications,
)
from app.pack_activation import certification_snapshot  # noqa: E402
from app.pack_state import (  # noqa: E402
    InMemoryPackStateStore,
    pack_state_view,
    set_pack_state_store,
)
from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_certification import (  # noqa: E402
    LEVEL_CERTIFIED,
    LEVEL_COMMUNITY,
    LEVEL_LABELS,
    certification_badge,
    certification_badges,
)
from discovery.packs.pack_config import list_packs  # noqa: E402

PACK = "cloud_ops"
ORG = "org_at833"


@pytest.fixture(autouse=True)
def in_memory_pack_state():
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_pack_state_store(None)


@pytest.fixture
def unsigned_pack(monkeypatch):
    """Seed a pack whose Certified claim carries no signature.

    The single most important fixture in this file: every surface must show this
    pack as Community.
    """
    declaration = dict(pack_config.PACK_REGISTRY[PACK]["certification"])
    declaration["signature"] = {"keyId": "", "algorithm": "", "value": ""}
    monkeypatch.setitem(pack_config.PACK_REGISTRY[PACK], "certification", declaration)
    return PACK


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


# ── The badge projection ──────────────────────────────────────────────────────


def test_badge_is_the_compact_display_shape():
    badge = certification_badge(PACK)
    # Kept as an EXACT set: the point of the compact projection is that the full
    # audit shape (signature key ids, downgrade reasons, scope) never leaks onto
    # every row of a 200-finding list. `reviewDueDetail`/`reviewDueOn` are the
    # 2.0-C2 T5 additions.
    assert set(badge) == {
        "packId",
        "level",
        "label",
        "statusLabel",
        "declaredLevel",
        "reviewDue",
        "reviewDueDetail",
        "reviewDueOn",
    }
    assert badge["level"] == LEVEL_CERTIFIED
    assert badge["label"] == LEVEL_LABELS[LEVEL_CERTIFIED]


def test_badge_reports_the_effective_level_and_keeps_the_claim(unsigned_pack):
    badge = certification_badge(PACK)
    assert badge["level"] == LEVEL_COMMUNITY        # what a surface renders
    assert badge["declaredLevel"] == LEVEL_CERTIFIED  # what the pack asked for


def test_badges_default_to_every_registered_pack():
    assert set(certification_badges()) == set(list_packs())


def test_badges_can_be_scoped_to_a_selection():
    assert set(certification_badges([PACK, "ncino"])) == {PACK, "ncino"}


# ── Surface 1: selection ──────────────────────────────────────────────────────


def test_selection_lists_every_pack_with_its_level():
    view = {row["packId"]: row for row in pack_state_view(ORG)}
    assert set(view) == set(list_packs())
    for pack_id, row in view.items():
        assert row["certification"]["packId"] == pack_id
        assert row["certification"]["level"] in {
            LEVEL_CERTIFIED,
            "partner",
            LEVEL_COMMUNITY,
        }


def test_selection_shows_community_for_an_unproved_claim(unsigned_pack):
    row = next(row for row in pack_state_view(ORG) if row["packId"] == PACK)
    assert row["certification"]["level"] == LEVEL_COMMUNITY


def test_selection_row_keeps_its_lifecycle_fields():
    """Certification is ADDITIVE — the 2.0-C1 fields are untouched."""
    row = next(row for row in pack_state_view(ORG) if row["packId"] == PACK)
    for key in ("state", "packVersion", "pinnedVersion", "effectiveVersion",
                "availableVersions", "registered"):
        assert key in row


def test_orphaned_row_reports_no_certification():
    """A pack the registry no longer declares has no level to report."""
    from app.pack_state import disable_pack

    store = InMemoryPackStateStore()
    set_pack_state_store(store)
    disable_pack(ORG, PACK, actor_id="owner")
    # Remove the pack from the registry AFTER the state row exists.
    removed = pack_config.PACK_REGISTRY.pop(PACK)
    try:
        row = next(row for row in pack_state_view(ORG) if row["packId"] == PACK)
        assert row["registered"] is False
        assert row["certification"] is None
    finally:
        pack_config.PACK_REGISTRY[PACK] = removed


# ── Surface 2: activation ─────────────────────────────────────────────────────


def test_activation_snapshot_records_the_level_per_pack():
    snapshot = certification_snapshot([PACK, "ncino"])
    assert set(snapshot) == {PACK, "ncino"}
    assert snapshot[PACK]["level"] == LEVEL_CERTIFIED


def test_activation_snapshot_records_a_downgrade(unsigned_pack):
    assert certification_snapshot([PACK])[PACK]["level"] == LEVEL_COMMUNITY


def test_activation_snapshot_is_fail_soft(monkeypatch):
    """A launch must never fail because a label could not be resolved."""
    import discovery.packs.pack_certification as certification_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("crypto backend down")

    monkeypatch.setattr(certification_module, "certification_badges", boom)
    assert certification_snapshot([PACK]) == {}


def test_run_health_row_reports_the_level():
    from app.health_aggregation import _certification_fields

    fields = _certification_fields(PACK)
    assert fields["certification_level"] == LEVEL_CERTIFIED
    assert fields["certification_label"] == LEVEL_LABELS[LEVEL_CERTIFIED]
    assert fields["certification_review_due"] is False
    # 2.0-C2 T5 (AT-835): the row also carries WHY it is due and WHEN it falls due.
    assert set(fields) == {
        "certification_level",
        "certification_label",
        "certification_review_due",
        "certification_review_due_detail",
        "certification_review_due_on",
    }


def test_run_health_row_reports_community_for_an_unproved_claim(unsigned_pack):
    from app.health_aggregation import _certification_fields

    assert _certification_fields(PACK)["certification_level"] == LEVEL_COMMUNITY


def test_run_health_row_for_a_removed_pack_reports_no_badge():
    """A pack the registry no longer declares must not wear the DEFAULT pack's badge.

    ``get_pack()`` resolves an unknown id to service_cloud, which is right for
    detectors and wrong here: a panel whose job is attribution would otherwise
    attribute service_cloud's certification to a pack that is gone.
    """
    from app.health_aggregation import _certification_fields

    assert _certification_fields("a_pack_that_was_removed") == {}


# ── Surface 3: findings ───────────────────────────────────────────────────────


def test_finding_carries_the_level_of_the_pack_that_produced_it():
    stamped = with_pack_certification(_opp())
    assert stamped["packCertificationLevel"] == LEVEL_CERTIFIED
    assert stamped["packCertificationLabel"] == LEVEL_LABELS[LEVEL_CERTIFIED]


def test_finding_shows_community_for_an_unproved_claim(unsigned_pack):
    stamped = with_pack_certification(_opp())
    assert stamped["packCertificationLevel"] == LEVEL_COMMUNITY
    assert stamped["packCertificationLabel"] == LEVEL_LABELS[LEVEL_COMMUNITY]


def test_finding_stamping_is_additive():
    """Nothing else on the finding is touched — score, evidence, version stamp."""
    original = _opp()
    stamped = with_pack_certification(original)
    for key, value in original.items():
        assert stamped[key] == value


def test_finding_without_a_pack_stamp_is_returned_unchanged():
    original = {"id": "legacy", "title": "Pre-R16-B1 finding"}
    assert with_pack_certification(original) == original


def test_review_due_is_flagged_only_when_due(monkeypatch):
    assert "packCertificationReviewDue" not in with_pack_certification(_opp())

    badges = {PACK: {**certification_badge(PACK), "reviewDue": True}}
    stamped = with_pack_certification(_opp(), certifications=badges)
    assert stamped["packCertificationReviewDue"] is True
    # Flagged, never downgraded (AC4's display half).
    assert stamped["packCertificationLevel"] == LEVEL_CERTIFIED


def test_display_funnel_stamps_certification():
    """Every serve site routes through this funnel, so one wiring covers them all."""
    stamped = with_display_title(_opp())
    assert stamped["packCertificationLevel"] == LEVEL_CERTIFIED
    # …and the 2.0-C1 pack-state label still lands.
    assert stamped["packState"] == "active"


def test_list_helpers_stamp_every_finding():
    opps = [_opp(), _opp(pack_id="ncino")]
    for stamped in (with_display_all(opps), with_pack_certifications(opps)):
        assert all(row["packCertificationLevel"] for row in stamped)


def test_list_resolution_reads_badges_once(monkeypatch):
    """A 200-finding list must cost ONE verification pass, not 200."""
    import app.opportunity_display as display

    calls = {"n": 0}
    real = display._resolve_pack_certifications

    def counted():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(display, "_resolve_pack_certifications", counted)
    display.with_pack_certifications([_opp() for _ in range(50)])
    assert calls["n"] == 1


def test_finding_stamping_is_fail_soft(monkeypatch):
    """An unresolvable badge omits the fields — it never invents a level."""
    import app.opportunity_display as display

    monkeypatch.setattr(display, "_resolve_pack_certifications", dict)
    stamped = display.with_pack_certification(_opp())
    assert "packCertificationLevel" not in stamped
    assert stamped["packId"] == PACK  # the finding itself is untouched


# ── Surface 4: exports ────────────────────────────────────────────────────────


def test_report_names_the_level_of_every_contributing_pack():
    report = build_executive_report(
        "run_1", [_opp(), _opp(pack_id="ncino")], {}, ["servicenow"]
    )
    levels = {item["packId"]: item["level"] for item in report["packCertifications"]}
    assert levels == {PACK: LEVEL_CERTIFIED, "ncino": LEVEL_CERTIFIED}


def test_report_shows_community_for_an_unproved_claim(unsigned_pack):
    report = build_executive_report("run_1", [_opp()], {}, [])
    assert report["packCertifications"][0]["level"] == LEVEL_COMMUNITY


def test_report_lists_each_pack_once_in_first_appearance_order():
    opps = [_opp(pack_id="ncino"), _opp(), _opp(pack_id="ncino")]
    assert [item["packId"] for item in pack_certifications(opps)] == ["ncino", PACK]


def test_report_with_no_findings_lists_no_packs():
    assert build_executive_report("run_1", [], {}, [])["packCertifications"] == []


def test_report_ignores_findings_with_no_pack_stamp():
    assert pack_certifications([{"id": "legacy"}]) == []


def test_report_generation_is_fail_soft(monkeypatch):
    import discovery.packs.pack_certification as certification_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(certification_module, "certification_badges", boom)
    report = build_executive_report("run_1", [_opp()], {}, [])
    # The report still generates; it just carries no badges rather than unproved ones.
    assert report["packCertifications"] == []
    assert report["confidence"]


def test_report_quick_wins_carry_the_level_too():
    """The findings INSIDE the export are labelled as well as the header list."""
    from app.opportunity_display import with_exec_report_display_titles

    report = build_executive_report("run_1", [_opp()], {}, [])
    displayed = with_exec_report_display_titles(report)
    assert displayed["topQuickWins"][0]["packCertificationLevel"] == LEVEL_CERTIFIED
