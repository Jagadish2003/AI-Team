"""2.0-C2 T4 (AT-834) — the certification activation policy, DB-free.

Sub-task scope: *an org can restrict which levels may be activated (e.g. federal
deployments: Certified only) — Owner-controlled, enforced at activation.*

Parent-story criterion discharged here:

  * AC3 — an org policy restricting to Certified prevents activation of
    Partner/Community packs, with a clear reason.

The two properties that make this a real control rather than a label:

  1. It is enforced at ACTIVATION, in the single resolution both API edges and the
     discovery runner call — so a CLI caller cannot walk around it.
  2. It FAILS CLOSED. An unreadable policy, or a pack whose level cannot be verified,
     refuses activation instead of assuming compliance. Every other read in the pack
     lifecycle fails soft; this one must not, and several tests pin that difference.

The HTTP surface is pinned in
``tests/contract/test_pack_certification_policy_api.py``.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("INGEST_MODE", "offline")

from app.pack_activation import resolve_activatable_packs  # noqa: E402
from app.pack_certification_policy import (  # noqa: E402
    DEFAULT_MINIMUM_LEVEL,
    InMemoryPackCertificationPolicyStore,
    PackCertificationPolicyError,
    PackCertificationPolicyUnavailable,
    PackCertificationPolicyViolation,
    annotate_activation_blocked,
    assert_selection_permitted,
    check_selection,
    get_certification_policy,
    set_certification_policy,
    set_policy_store,
)
from app.pack_state import (  # noqa: E402
    InMemoryPackStateStore,
    disable_pack,
    set_pack_state_store,
)
from discovery.packs import pack_config  # noqa: E402
from discovery.packs.pack_certification import (  # noqa: E402
    LEVEL_CERTIFIED,
    LEVEL_COMMUNITY,
    LEVEL_PARTNER,
)

ORG = "org_at834"
PACK = "cloud_ops"
OTHER_PACK = "ncino"


@pytest.fixture(autouse=True)
def in_memory_stores():
    set_policy_store(InMemoryPackCertificationPolicyStore())
    set_pack_state_store(InMemoryPackStateStore())
    yield
    set_policy_store(None)
    set_pack_state_store(None)


@pytest.fixture
def uncertified_pack(monkeypatch):
    """A pack whose Certified claim carries no signature → effective Community."""
    declaration = dict(pack_config.PACK_REGISTRY[PACK]["certification"])
    declaration["signature"] = {"keyId": "", "algorithm": "", "value": ""}
    monkeypatch.setitem(pack_config.PACK_REGISTRY[PACK], "certification", declaration)
    return PACK


def _restrict(level=LEVEL_CERTIFIED, org=ORG):
    return set_certification_policy(org, level, actor_id="owner@example.com")


# ── The policy itself ─────────────────────────────────────────────────────────


def test_default_is_no_restriction():
    policy = get_certification_policy(ORG)
    assert policy.minimum_level == DEFAULT_MINIMUM_LEVEL == LEVEL_COMMUNITY
    assert policy.restricted is False
    assert policy.revision == 0
    assert policy.label == "No certification restriction"


def test_setting_a_floor_records_who_and_when():
    outcome = _restrict()
    assert outcome.changed is True
    assert outcome.previous_level == LEVEL_COMMUNITY
    assert outcome.policy.minimum_level == LEVEL_CERTIFIED
    assert outcome.policy.restricted is True
    assert outcome.policy.updated_by == "owner@example.com"
    assert outcome.policy.updated_at
    assert outcome.policy.revision == 1


def test_setting_the_same_floor_is_idempotent():
    _restrict()
    repeat = _restrict()
    assert repeat.changed is False
    assert repeat.policy.revision == 1  # no second write


def test_lifting_a_restriction_is_a_write_not_a_delete():
    _restrict()
    lifted = set_certification_policy(ORG, LEVEL_COMMUNITY, actor_id="owner")
    assert lifted.changed is True
    assert lifted.previous_level == LEVEL_CERTIFIED
    # The row survives with a higher revision, so "the floor was lowered, by whom,
    # and when" stays answerable.
    assert lifted.policy.revision == 2
    assert lifted.policy.restricted is False


def test_policy_is_org_scoped():
    _restrict(org="org_a")
    assert get_certification_policy("org_a").restricted is True
    assert get_certification_policy("org_b").restricted is False


def test_illegal_level_is_rejected():
    with pytest.raises(PackCertificationPolicyError):
        set_certification_policy(ORG, "platinum", actor_id="owner")


def test_store_contract_has_no_delete_path():
    from app.pack_certification_policy import PackCertificationPolicyStore

    surface = {
        name for name in dir(PackCertificationPolicyStore) if not name.startswith("_")
    }
    assert not {
        name for name in surface if any(v in name for v in ("delete", "remove", "drop"))
    }


def test_module_contains_no_destructive_sql():
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "pack_certification_policy.py"
    )
    text = source.read_text(encoding="utf-8").upper()
    assert "DELETE FROM" not in text
    assert "TRUNCATE" not in text


# ── The gate: AC3 ─────────────────────────────────────────────────────────────


def test_no_restriction_permits_everything():
    assert check_selection(ORG, [PACK, OTHER_PACK]) == []
    assert assert_selection_permitted(ORG, [PACK]).restricted is False


def test_certified_only_permits_a_certified_pack():
    _restrict()
    assert check_selection(ORG, [PACK]) == []


def test_certified_only_refuses_an_uncertified_pack(uncertified_pack):
    """AC3, directly."""
    _restrict()
    with pytest.raises(PackCertificationPolicyViolation) as exc:
        assert_selection_permitted(ORG, [PACK])

    message = str(exc.value)
    assert PACK in message                      # names the pack
    assert "Community" in message               # names the level it holds
    assert "CloudFulcrum Certified" in message  # names what is required
    assert exc.value.pack_ids == [PACK]


def test_refusal_says_the_claim_could_not_be_verified(uncertified_pack):
    """A pack that CLAIMS Certified must not be reported as merely 'Community'.

    Without this, an operator reads "cloud_ops is Community" and goes looking for a
    pack that is, on paper, Certified.
    """
    _restrict()
    with pytest.raises(PackCertificationPolicyViolation) as exc:
        assert_selection_permitted(ORG, [PACK])
    assert "could not be verified" in str(exc.value)


def test_refusal_names_every_offending_pack(monkeypatch):
    _restrict()
    for pack_id in (PACK, OTHER_PACK):
        declaration = dict(pack_config.PACK_REGISTRY[pack_id]["certification"])
        declaration["signature"] = {"keyId": "", "algorithm": "", "value": ""}
        monkeypatch.setitem(
            pack_config.PACK_REGISTRY[pack_id], "certification", declaration
        )

    with pytest.raises(PackCertificationPolicyViolation) as exc:
        assert_selection_permitted(ORG, [PACK, OTHER_PACK])
    assert set(exc.value.pack_ids) == {PACK, OTHER_PACK}


def test_partner_floor_permits_certified_but_not_community(uncertified_pack):
    """The floor is ORDERED: accepting Partner necessarily accepts Certified."""
    _restrict(LEVEL_PARTNER)
    # cloud_ops is now effectively Community → blocked by a Partner floor.
    assert [v.pack_id for v in check_selection(ORG, [PACK])] == [PACK]
    # ncino is genuinely Certified → clears a Partner floor.
    assert check_selection(ORG, [OTHER_PACK]) == []


def test_violation_serialises_for_an_api_response(uncertified_pack):
    _restrict()
    with pytest.raises(PackCertificationPolicyViolation) as exc:
        assert_selection_permitted(ORG, [PACK])
    payload = exc.value.to_dict()
    assert payload["error"] == "pack_certification_policy"
    assert payload["packs"][0]["packId"] == PACK
    assert payload["packs"][0]["minimumLevel"] == LEVEL_CERTIFIED
    assert payload["packs"][0]["reason"]


# ── Fail closed ───────────────────────────────────────────────────────────────


def test_an_unreadable_policy_refuses_rather_than_assuming_none():
    """The single most important test in this file.

    Failing open here would lift a federal deployment's restriction at exactly the
    moment it matters — a database blip.
    """

    class Broken(InMemoryPackCertificationPolicyStore):
        def get(self, org_id):
            raise RuntimeError("policy store unreachable")

    set_policy_store(Broken())
    with pytest.raises(PackCertificationPolicyUnavailable):
        get_certification_policy(ORG)
    with pytest.raises(PackCertificationPolicyUnavailable):
        assert_selection_permitted(ORG, [PACK])


def test_unverifiable_levels_refuse_when_a_restriction_is_in_force(monkeypatch):
    _restrict()
    import discovery.packs.pack_certification as certification_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("crypto backend down")

    monkeypatch.setattr(certification_module, "certification_badges", boom)
    with pytest.raises(PackCertificationPolicyUnavailable):
        assert_selection_permitted(ORG, [PACK])


def test_an_unknown_pack_under_a_restriction_is_a_violation(monkeypatch):
    """"We could not tell" must never read as "it qualifies"."""
    _restrict()
    import app.pack_certification_policy as policy_module

    monkeypatch.setattr(
        policy_module, "certification_badges", lambda *_a, **_k: {}, raising=False
    )
    import discovery.packs.pack_certification as certification_module

    monkeypatch.setattr(
        certification_module, "certification_badges", lambda *_a, **_k: {}
    )
    violations = check_selection(ORG, [PACK])
    assert [v.pack_id for v in violations] == [PACK]


def test_no_restriction_costs_nothing(monkeypatch):
    """A deployment that has not opted in never verifies a signature at all."""
    import discovery.packs.pack_certification as certification_module

    def boom(*_args, **_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("certification was verified with no policy in force")

    monkeypatch.setattr(certification_module, "certification_badges", boom)
    assert check_selection(ORG, [PACK, OTHER_PACK]) == []


# ── Enforcement at activation ─────────────────────────────────────────────────


def test_activation_refuses_a_pack_below_the_floor(uncertified_pack):
    """Enforced in the ONE resolution both API edges and the runner call."""
    _restrict()
    with pytest.raises(PackCertificationPolicyViolation):
        resolve_activatable_packs(org_id=ORG, pack_ids=[PACK], run_id="run_1")


def test_activation_permits_a_compliant_selection():
    _restrict()
    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])
    assert decision.activated_pack_ids == [PACK]


def test_a_disabled_pack_cannot_fail_a_run_on_policy_grounds(uncertified_pack):
    """Disabled is evaluated FIRST, so a pack that is not going to execute cannot
    refuse the run — the same reasoning that orders disabled before compatibility."""
    _restrict()
    disable_pack(ORG, PACK, actor_id="owner")
    decision = resolve_activatable_packs(org_id=ORG, pack_ids=[PACK, OTHER_PACK])
    assert decision.activated_pack_ids == [OTHER_PACK]
    assert decision.excluded_pack_ids == [PACK]


def test_policy_refusal_is_recorded_as_telemetry(uncertified_pack, monkeypatch):
    _restrict()
    events = []
    import app.telemetry as telemetry

    monkeypatch.setattr(
        telemetry, "record_event", lambda name, payload: events.append((name, payload))
    )
    with pytest.raises(PackCertificationPolicyViolation):
        resolve_activatable_packs(org_id=ORG, pack_ids=[PACK], run_id="run_1")

    refusals = [e for e in events if e[0] == "pack.certification_policy_refused"]
    assert refusals, "the refusal was not recorded"
    assert refusals[0][1]["pack_ids"] == [PACK]
    assert refusals[0][1]["minimum_level"] == LEVEL_CERTIFIED


def test_telemetry_failure_never_masks_the_refusal(uncertified_pack, monkeypatch):
    _restrict()
    import app.telemetry as telemetry

    def boom(*_args, **_kwargs):
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(telemetry, "record_event", boom)
    with pytest.raises(PackCertificationPolicyViolation):
        resolve_activatable_packs(org_id=ORG, pack_ids=[PACK])


# ── Selection-surface annotation ──────────────────────────────────────────────


def _rows():
    return [
        {"packId": PACK, "certification": {"level": LEVEL_COMMUNITY,
                                           "declaredLevel": LEVEL_CERTIFIED}},
        {"packId": OTHER_PACK, "certification": {"level": LEVEL_CERTIFIED,
                                                 "declaredLevel": LEVEL_CERTIFIED}},
    ]


def test_annotation_marks_blocked_packs_under_a_restriction():
    _restrict()
    annotated = {row["packId"]: row for row in annotate_activation_blocked(ORG, _rows())}
    assert annotated[PACK]["activationBlocked"] is True
    assert PACK in annotated[PACK]["activationBlockedReason"]
    assert annotated[OTHER_PACK]["activationBlocked"] is False


def test_annotation_blocks_nothing_without_a_restriction():
    annotated = annotate_activation_blocked(ORG, _rows())
    assert all(row["activationBlocked"] is False for row in annotated)


def test_annotation_is_fail_soft_while_the_gate_is_not(uncertified_pack):
    """Display degrades; enforcement does not. A surfacing hiccup must never become
    a way past the policy."""

    class Broken(InMemoryPackCertificationPolicyStore):
        def get(self, org_id):
            raise RuntimeError("policy store unreachable")

    set_policy_store(Broken())
    annotated = annotate_activation_blocked(ORG, _rows())
    assert "activationBlocked" not in annotated[0]  # unannotated, not crashed
    with pytest.raises(PackCertificationPolicyUnavailable):
        assert_selection_permitted(ORG, [PACK])


def test_annotation_leaves_the_row_otherwise_untouched():
    _restrict()
    annotated = annotate_activation_blocked(ORG, _rows())
    assert annotated[0]["packId"] == PACK
    assert annotated[0]["certification"]["level"] == LEVEL_COMMUNITY
