"""2.0-B2 T1 — the org-configured alias table (tier 2's input).

Tier 2 is allowed to AUTO-MERGE because a human asserted the identity, so the
table itself has to be trustworthy. These tests pin what makes it so:

  * aliases normalise through the SAME canonicalisation the entity layer uses, so
    the table and the graph cannot disagree about what a name is;
  * a group is scoped to one entity type — a team named "Payments" is never
    merged with a system named "Payments";
  * a CONFLICTING table (one alias claimed by two groups) is rejected, not
    silently resolved by iteration order — that is how a wrong merge ships
    invisibly;
  * an invalid table stored for an org degrades to "tier 2 contributes nothing",
    loudly, rather than breaking a run or merging on a half-read table;
  * the store is org-scoped and validated on write, so a bad table cannot be
    persisted in the first place.

DB-free: the ``kv`` layer is monkeypatched.
"""
from __future__ import annotations

import json

import pytest

from app import entity_alias_mappings as eam


# ── normalisation ───────────────────────────────────────────────────────────


def test_aliases_are_canonicalised_like_the_entity_layer():
    from app.entity_resolution import canonical_name_for

    [mapping] = eam.normalize_alias_mappings([
        {"entity_type": "system", "canonical": "  Payments   API ",
         "aliases": ["PAYMENTS-API", "svc payments"]}
    ])

    assert mapping.canonical == canonical_name_for("Payments API")
    assert mapping.aliases == ("payments-api", "svc payments")
    assert mapping.group_id == "system:payments api"
    assert set(mapping.members) == {"payments api", "payments-api", "svc payments"}


def test_duplicate_and_self_aliases_are_collapsed():
    [mapping] = eam.normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api",
         "aliases": ["Payments API", "payments api", "PAYMENTS-API", ""]}
    ])
    assert mapping.aliases == ("payments api",)


def test_mappings_are_returned_in_a_deterministic_order():
    mappings = eam.normalize_alias_mappings([
        {"entity_type": "team", "canonical": "z", "aliases": ["zed"]},
        {"entity_type": "system", "canonical": "b", "aliases": ["bee"]},
        {"entity_type": "system", "canonical": "a", "aliases": ["ay"]},
    ])
    assert [(m.entity_type, m.canonical) for m in mappings] == [
        ("system", "a"), ("system", "b"), ("team", "z"),
    ]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ({"canonical": "x", "aliases": ["y"]}, "entity_type"),
        ({"entity_type": "widget", "canonical": "x", "aliases": ["y"]}, "entity_type"),
        ({"entity_type": "system", "canonical": "  ", "aliases": ["y"]}, "canonical"),
        ({"entity_type": "system", "canonical": "x", "aliases": "not-a-list"}, "aliases"),
        ({"entity_type": "system", "canonical": "x"}, "no aliases"),
        ({"entity_type": "system", "canonical": "x", "aliases": ["X", " x "]}, "no aliases"),
    ],
)
def test_a_malformed_mapping_is_rejected_with_the_reason(raw, expected):
    with pytest.raises(eam.AliasMappingError) as exc:
        eam.normalize_alias_mappings([raw])
    assert expected in str(exc.value)


def test_a_conflicting_alias_table_is_rejected_not_silently_resolved():
    """If one alias were claimed by two groups the merge target would depend on
    iteration order — an invisible wrong merge."""
    with pytest.raises(eam.AliasMappingConflict) as exc:
        eam.normalize_alias_mappings([
            {"entity_type": "system", "canonical": "payments-api", "aliases": ["payments"]},
            {"entity_type": "system", "canonical": "payments-gateway", "aliases": ["payments"]},
        ])
    message = str(exc.value)
    assert "payments" in message
    assert "system:payments-api" in message and "system:payments-gateway" in message


def test_the_same_alias_under_a_different_entity_type_is_not_a_conflict():
    """A team named "Payments" and a system named "Payments" are different
    things, and the type scope is what keeps them apart."""
    mappings = eam.normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api", "aliases": ["payments"]},
        {"entity_type": "team", "canonical": "payments-team", "aliases": ["payments"]},
    ])
    assert len(mappings) == 2


def test_an_empty_table_normalises_to_nothing():
    assert eam.normalize_alias_mappings(None) == []
    assert eam.normalize_alias_mappings([]) == []


# ── index ───────────────────────────────────────────────────────────────────


def test_the_index_finds_a_group_by_any_member_including_the_canonical():
    index = eam.build_alias_index(eam.normalize_alias_mappings([
        {"entity_type": "system", "canonical": "payments-api",
         "aliases": ["Payments API", "svc-payments"]}
    ]))

    for member in ("payments-api", "payments api", "svc-payments"):
        group = index.group_for("system", member)
        assert group is not None and group.group_id == "system:payments-api"

    assert index.group_for("team", "payments-api") is None, "type scoping holds"
    assert index.group_for("system", "unknown") is None
    assert index.group_for("system", "") is None


# ── store ───────────────────────────────────────────────────────────────────


@pytest.fixture
def kv(monkeypatch):
    """An in-memory stand-in for the ``kv`` layer."""
    store: dict = {}
    monkeypatch.setattr("app.db.kv_get", lambda key: store.get(key))
    monkeypatch.setattr("app.db.kv_set", lambda key, value: store.__setitem__(key, value))
    monkeypatch.delenv(eam.ALIAS_ENV_VAR, raising=False)
    return store


def test_put_then_get_round_trips_the_validated_table(kv):
    stored = eam.put_alias_mappings("org_a", [
        {"entity_type": "system", "canonical": "Payments API",
         "aliases": ["payments-api"], "created_by": "owner@example.com"}
    ])
    assert [m.canonical for m in stored] == ["payments api"]

    loaded = eam.get_alias_mappings("org_a")
    assert [m.group_id for m in loaded] == ["system:payments api"]
    assert loaded[0].created_by == "owner@example.com"


def test_the_table_is_org_scoped(kv):
    eam.put_alias_mappings("org_a", [
        {"entity_type": "system", "canonical": "a", "aliases": ["alpha"]}
    ])
    assert eam.get_alias_mappings("org_b") == []
    assert f"{eam.ALIAS_KV_KEY}:org_a" in kv


def test_a_conflicting_table_can_never_be_persisted(kv):
    eam.put_alias_mappings("org_a", [
        {"entity_type": "system", "canonical": "payments-api", "aliases": ["payments"]}
    ])
    with pytest.raises(eam.AliasMappingConflict):
        eam.put_alias_mappings("org_a", [
            {"entity_type": "system", "canonical": "a", "aliases": ["payments"]},
            {"entity_type": "system", "canonical": "b", "aliases": ["payments"]},
        ])
    # The previously stored, valid table is untouched.
    assert [m.canonical for m in eam.get_alias_mappings("org_a")] == ["payments-api"]


def test_an_invalid_stored_table_degrades_loudly_instead_of_merging(kv, caplog):
    """A table that cannot be trusted must contribute NOTHING — not break the
    run, and certainly not merge on a half-read table."""
    kv[f"{eam.ALIAS_KV_KEY}:org_a"] = [
        {"entity_type": "system", "canonical": "a", "aliases": ["shared"]},
        {"entity_type": "system", "canonical": "b", "aliases": ["shared"]},
    ]
    with caplog.at_level("WARNING"):
        assert eam.get_alias_mappings("org_a") == []
    assert "alias table" in caplog.text


def test_an_unreadable_store_degrades_rather_than_breaking_a_run(monkeypatch, caplog):
    monkeypatch.delenv(eam.ALIAS_ENV_VAR, raising=False)

    def _boom(_key):
        raise RuntimeError("kv down")

    monkeypatch.setattr("app.db.kv_get", _boom)
    with caplog.at_level("WARNING"):
        assert eam.get_alias_mappings("org_a") == []
    assert "unreadable" in caplog.text


def test_an_org_less_read_returns_nothing(kv):
    assert eam.get_alias_mappings("") == []


def test_an_org_less_write_is_refused(kv):
    with pytest.raises(eam.AliasMappingError):
        eam.put_alias_mappings("", [{"entity_type": "system", "canonical": "a",
                                     "aliases": ["b"]}])


# ── env override ────────────────────────────────────────────────────────────


def test_the_env_override_wins_over_the_stored_table(kv, monkeypatch):
    eam.put_alias_mappings("org_a", [
        {"entity_type": "system", "canonical": "stored", "aliases": ["s"]}
    ])
    monkeypatch.setenv(eam.ALIAS_ENV_VAR, json.dumps([
        {"entity_type": "system", "canonical": "from-env", "aliases": ["e"]}
    ]))
    assert [m.canonical for m in eam.get_alias_mappings("org_a")] == ["from-env"]


def test_the_env_override_can_be_keyed_by_org_with_a_default(monkeypatch):
    monkeypatch.setattr("app.db.kv_get", lambda key: None)
    monkeypatch.setenv(eam.ALIAS_ENV_VAR, json.dumps({
        "org_a": [{"entity_type": "system", "canonical": "for-a", "aliases": ["a"]}],
        "default": [{"entity_type": "system", "canonical": "fallback", "aliases": ["f"]}],
    }))
    assert [m.canonical for m in eam.get_alias_mappings("org_a")] == ["for-a"]
    assert [m.canonical for m in eam.get_alias_mappings("org_z")] == ["fallback"]


def test_a_malformed_env_override_raises_rather_than_reading_as_empty(monkeypatch):
    """An operator who configured this deliberately must see the mistake."""
    monkeypatch.setattr("app.db.kv_get", lambda key: None)
    monkeypatch.setenv(eam.ALIAS_ENV_VAR, "{not json")
    with pytest.raises(eam.AliasMappingError):
        eam.get_alias_mappings("org_a")


def test_get_alias_index_is_the_resolver_ready_form(kv):
    eam.put_alias_mappings("org_a", [
        {"entity_type": "system", "canonical": "payments-api", "aliases": ["Payments API"]}
    ])
    index = eam.get_alias_index("org_a")
    assert index.group_for("system", "payments api").canonical == "payments-api"
