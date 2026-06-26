"""
R16-C2 — T2: focus emphasis applied at ranking time.

These discovery-layer unit tests prove the ranking behaviour deterministically,
without the API/DB:

  AC1/AC7 — two different non-enterprise focuses produce visibly different
            ranking on identical data.
  AC2     — a focus emphasises matching findings: they rank higher than they
            would under the unbiased enterprise-wide view.
  AC4     — enterprise_wide (and None / unknown focus) applies no bias — the
            ordering is identical to the historical baseline.
  AC5     — deterministic: same focus + same data => same ordering every time.

They also confirm the emphasis is additive (scoring fields untouched) and that
the legacy focus-unaware ranking is preserved byte-for-byte.
"""
from discovery.calibration.ranking import rank_opportunities, rank_key, TIER_ORDER
from discovery.packs import focus_affinity as fa


def _opp(detector_id, tier, impact, effort, **extra):
    o = {"detector_id": detector_id, "tier": tier, "impact": impact, "effort": effort}
    o.update(extra)
    return o


# A fixed, identical dataset reused across focuses. Baseline (no focus) order is
# driven by tier: Quick Win (B) -> Strategic (C) -> Complex (A).
def _dataset():
    return [
        _opp("APPROVAL_BOTTLENECK", "Complex", 5, 5),    # A — approvals_compliance
        _opp("HANDOFF_FRICTION", "Quick Win", 8, 2),     # B — cross_system_handoffs
        _opp("KNOWLEDGE_GAP", "Strategic", 6, 3),        # C — member_customer_service
    ]


def _ids(opps):
    return [o["detector_id"] for o in opps]


# ── Baseline / backward compatibility ──────────────────────────────────────────

def test_no_focus_preserves_legacy_order():
    opps = _dataset()
    ranked = rank_opportunities(opps)
    # Pure tier ordering, exactly as before focus existed.
    assert _ids(ranked) == ["HANDOFF_FRICTION", "KNOWLEDGE_GAP", "APPROVAL_BOTTLENECK"]


def test_rank_key_backward_compatible_signature():
    # rank_key must still work with a single arg (existing callers).
    opps = _dataset()
    assert sorted(opps, key=rank_key) == rank_opportunities(opps)


def test_enterprise_wide_equals_baseline():
    opps = _dataset()
    baseline = _ids(rank_opportunities(opps))
    assert _ids(rank_opportunities(opps, focus_id="enterprise_wide")) == baseline
    assert _ids(rank_opportunities(opps, focus_id=None)) == baseline


def test_enterprise_wide_ignores_stale_focus_emphasis_annotation():
    opps = [
        {
            "tier": "Complex",
            "impact": 5,
            "effort": 5,
            "_debug": {"detector_id": "APPROVAL_BOTTLENECK"},
            "focus_emphasis": {"rank": fa.FOCUS_EMPHASIS_RANK, "matched": True},
        },
        {
            "tier": "Quick Win",
            "impact": 8,
            "effort": 2,
            "_debug": {"detector_id": "HANDOFF_FRICTION"},
            "focus_emphasis": {"rank": fa.FOCUS_NEUTRAL_RANK, "matched": False},
        },
    ]

    ranked = rank_opportunities(opps, focus_id="enterprise_wide")

    assert [o["_debug"]["detector_id"] for o in ranked] == [
        "HANDOFF_FRICTION",
        "APPROVAL_BOTTLENECK",
    ]


def test_unknown_focus_degrades_to_baseline():
    opps = _dataset()
    baseline = _ids(rank_opportunities(opps))
    assert _ids(rank_opportunities(opps, focus_id="not_a_focus")) == baseline


# ── AC2 — emphasis raises matching findings ─────────────────────────────────────

def test_focus_emphasises_matching_finding_above_baseline():
    opps = _dataset()
    # APPROVAL_BOTTLENECK is last under the unbiased view (Complex tier)...
    assert _ids(rank_opportunities(opps))[-1] == "APPROVAL_BOTTLENECK"
    # ...but first under approvals_compliance, despite its lower tier.
    ranked = rank_opportunities(opps, focus_id="approvals_compliance")
    assert ranked[0]["detector_id"] == "APPROVAL_BOTTLENECK"


def test_matched_findings_keep_internal_tier_order():
    # Two matching findings: ranking among the emphasised group still follows
    # the tier / net-value / effort tie-breakers.
    opps = [
        _opp("APPROVAL_BOTTLENECK", "Complex", 5, 5),
        _opp("PERMISSION_BOTTLENECK", "Quick Win", 7, 2),
        _opp("HANDOFF_FRICTION", "Quick Win", 9, 1),  # not matched
    ]
    ranked = rank_opportunities(opps, focus_id="approvals_compliance")
    # Both matched come first; within them Quick Win beats Complex.
    assert _ids(ranked) == [
        "PERMISSION_BOTTLENECK",
        "APPROVAL_BOTTLENECK",
        "HANDOFF_FRICTION",
    ]


# ── AC1 / AC7 — different focuses, different emphasis ───────────────────────────

def test_two_different_focuses_produce_different_ordering():
    opps = _dataset()
    order_compliance = _ids(rank_opportunities(opps, focus_id="approvals_compliance"))
    order_handoffs = _ids(rank_opportunities(opps, focus_id="cross_system_handoffs"))
    order_member = _ids(rank_opportunities(opps, focus_id="member_customer_service"))
    assert order_compliance != order_handoffs
    assert order_compliance != order_member
    assert order_compliance[0] == "APPROVAL_BOTTLENECK"
    assert order_member[0] == "KNOWLEDGE_GAP"


def test_focus_differs_from_enterprise_wide():
    opps = _dataset()
    enterprise = _ids(rank_opportunities(opps, focus_id="enterprise_wide"))
    focused = _ids(rank_opportunities(opps, focus_id="approvals_compliance"))
    assert enterprise != focused


# ── AC5 — determinism ───────────────────────────────────────────────────────────

def test_same_focus_same_data_is_deterministic():
    opps = _dataset()
    runs = [_ids(rank_opportunities(_dataset(), focus_id="approvals_compliance")) for _ in range(5)]
    assert all(r == runs[0] for r in runs)


def test_ranking_does_not_mutate_input():
    opps = _dataset()
    before = [dict(o) for o in opps]
    rank_opportunities(opps, focus_id="approvals_compliance")
    assert opps == before  # input list and dicts untouched


# ── Emphasis is not exclusion ────────────────────────────────────────────────────

def test_emphasis_is_not_exclusion():
    # Every input opportunity must still be present after focus ranking.
    opps = _dataset()
    ranked = rank_opportunities(opps, focus_id="approvals_compliance")
    assert sorted(_ids(ranked)) == sorted(_ids(opps))
    assert len(ranked) == len(opps)


# ── Seed path: ranking driven by the carried focus_emphasis annotation ──────────

def test_focus_emphasis_field_drives_ranking_without_focus_id():
    # Mirrors the Track A seed opp, where detector_id is nested and ranking
    # relies on the focus_emphasis.rank the runner already computed.
    opps = [
        {"tier": "Complex", "impact": 5, "effort": 5,
         "focus_emphasis": {"rank": fa.FOCUS_EMPHASIS_RANK, "matched": True}},
        {"tier": "Quick Win", "impact": 8, "effort": 2,
         "focus_emphasis": {"rank": fa.FOCUS_NEUTRAL_RANK, "matched": False}},
    ]
    ranked = rank_opportunities(opps)  # no focus_id — uses the annotation
    assert ranked[0]["focus_emphasis"]["matched"] is True


def test_focus_id_falls_back_to_annotation_when_detector_id_nested():
    # focus_id given, but detector_id only under _debug (Track A shape): ranking
    # must fall back to the carried focus_emphasis.rank rather than going neutral.
    opps = [
        {"tier": "Complex", "impact": 5, "effort": 5, "_debug": {"detector_id": "APPROVAL_BOTTLENECK"},
         "focus_emphasis": {"rank": fa.FOCUS_EMPHASIS_RANK, "matched": True}},
        {"tier": "Quick Win", "impact": 8, "effort": 2, "_debug": {"detector_id": "HANDOFF_FRICTION"},
         "focus_emphasis": {"rank": fa.FOCUS_NEUTRAL_RANK, "matched": False}},
    ]
    ranked = rank_opportunities(opps, focus_id="approvals_compliance")
    assert ranked[0]["focus_emphasis"]["matched"] is True


# ── Helper functions ────────────────────────────────────────────────────────────

def test_focus_emphasis_rank_values():
    assert fa.focus_emphasis_rank("approvals_compliance", "APPROVAL_BOTTLENECK") == fa.FOCUS_EMPHASIS_RANK
    assert fa.focus_emphasis_rank("approvals_compliance", "HANDOFF_FRICTION") == fa.FOCUS_NEUTRAL_RANK
    assert fa.focus_emphasis_rank("enterprise_wide", "APPROVAL_BOTTLENECK") == fa.FOCUS_NEUTRAL_RANK
    assert fa.focus_emphasis_rank(None, "APPROVAL_BOTTLENECK") == fa.FOCUS_NEUTRAL_RANK


def test_build_focus_emphasis_matched():
    fe = fa.build_focus_emphasis("approvals_compliance", "APPROVAL_BOTTLENECK")
    assert fe["focus_id"] == "approvals_compliance"
    assert fe["matched"] is True
    assert fe["rank"] == fa.FOCUS_EMPHASIS_RANK
    assert "APPROVAL_BOTTLENECK" in fe["affinity"]
    assert "approvals_compliance" in fe["rationale"]


def test_build_focus_emphasis_unmatched():
    fe = fa.build_focus_emphasis("approvals_compliance", "HANDOFF_FRICTION")
    assert fe["matched"] is False
    assert fe["rank"] == fa.FOCUS_NEUTRAL_RANK
    assert "surfaced but not emphasised" in fe["rationale"]


def test_build_focus_emphasis_enterprise_and_none():
    fe_ent = fa.build_focus_emphasis("enterprise_wide", "APPROVAL_BOTTLENECK")
    assert fe_ent["matched"] is False
    assert fe_ent["affinity"] == []
    assert "no affinity bias" in fe_ent["rationale"]

    fe_none = fa.build_focus_emphasis(None, "APPROVAL_BOTTLENECK")
    assert fe_none["matched"] is False
    assert fe_none["focus_id"] is None


def test_build_focus_emphasis_always_fully_populated():
    for focus in fa.list_focus_ids() + [None, "garbage"]:
        fe = fa.build_focus_emphasis(focus, "APPROVAL_BOTTLENECK")
        assert set(fe.keys()) == {"focus_id", "matched", "rank", "affinity", "rationale"}


# ── load_focus_for_run (run-KV loader) ──────────────────────────────────────────

def test_load_focus_for_run_reads_focus_kv(monkeypatch):
    store = {("focus_id", "run1"): "approvals_compliance"}
    monkeypatch.setattr(fa, "run_kv_get", lambda k, r, *a: store.get((k, r)))
    assert fa.load_focus_for_run("run1") == "approvals_compliance"


def test_load_focus_for_run_falls_back_to_setup_context(monkeypatch):
    store = {("setup_context", "run2"): {"focus_id": "core_operations"}}
    monkeypatch.setattr(fa, "run_kv_get", lambda k, r, *a: store.get((k, r)))
    assert fa.load_focus_for_run("run2") == "core_operations"


def test_load_focus_for_run_safe_on_missing_and_error(monkeypatch):
    monkeypatch.setattr(fa, "run_kv_get", lambda k, r, *a: None)
    assert fa.load_focus_for_run("run3") is None
    assert fa.load_focus_for_run("") is None
    assert fa.load_focus_for_run(None) is None

    def _boom(*a, **k):
        raise RuntimeError("kv down")

    monkeypatch.setattr(fa, "run_kv_get", _boom)
    assert fa.load_focus_for_run("run4") is None  # never raises
