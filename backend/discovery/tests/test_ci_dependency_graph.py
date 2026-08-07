"""
Tests for the SHARED CI-dependency traversal engine (``detectors.ci_dependency_graph``).

The engine exists to remove a divergent code path: the MSP-B6 cloud-ops hotspot
detector and the MSP-B12 SecOps concentration detector each carried their own
adjacency construction and their own copy of the bounded BFS, and they had already
drifted — only the SecOps copy normalised edge DIRECTION, so the same CMDB could
produce different reachability in the two packs.

These tests pin BOTH halves of the fix:
  * the structural half — both packs resolve to the SAME functions (a re-introduced
    private copy fails here, not in review);
  * the behavioural half — the shared rules (direction normalisation, the depth
    bound, determinism, cycle safety) hold, and the depth bound still decides
    whether the cloud-ops hotspot fires (MSP-B6 T3-AC2).

Offline and DB-free, like the detectors themselves.
"""
from __future__ import annotations

import pytest

from discovery.detectors import ci_dependency_graph as g
from discovery.detectors import cloud_ops_shared_ci_hotspot as hotspot
from discovery.detectors import security_ops_common as secops_common
from discovery.detectors import security_ops_shared_infra_concentration as secops_hotspot


# ── the structural guarantee: ONE engine, not three copies ──────────────────────


class TestSingleTraversalEngine:

    def test_both_packs_use_the_shared_bounded_walk(self):
        assert hotspot._reachable_within is g.reachable_within
        assert secops_hotspot._reachable_within is g.reachable_within
        assert secops_common.reachable_within is g.reachable_within

    def test_both_packs_build_the_same_graph_from_the_same_cmdb(self):
        # The two packs read the edges from different block locations, but the
        # construction — and therefore the graph — must be identical.
        edge = {"relationship_type": "depends_on"}
        cloud = hotspot._adjacency({"ci_graph": {"edges": [
            {**edge, "from": "svc", "to": "db"},
        ]}})
        sec = secops_common.cmdb_adjacency({"relationships": [
            {**edge, "source_ci_id": "svc", "target_ci_id": "db"},
        ]})
        assert cloud == sec == {"svc": ["db"]}

    def test_no_detector_keeps_a_private_bfs(self):
        # A re-introduced local copy would shadow the shared name; the identity
        # assertions above catch that, and there must be no second implementation
        # hiding under the old private name either.
        for module in (hotspot, secops_hotspot):
            assert not hasattr(module, "_build_adjacency"), module.__name__


# ── the shared rules ────────────────────────────────────────────────────────────


class TestAdjacencyConstruction:

    @pytest.mark.parametrize("edge", [
        {"source_ci_id": "a", "target_ci_id": "b"},      # SecOps relationship record
        {"from_ci_sys_id": "a", "to_ci_sys_id": "b"},
        {"parent": "a", "child": "b"},
        {"from": "a", "to": "b"},                        # cloud-ops ci_graph edge
        {"source": "a", "target": "b"},
    ])
    def test_every_producer_edge_shape_is_understood(self, edge):
        assert g.build_adjacency([edge]) == {"a": ["b"]}

    @pytest.mark.parametrize("rel", sorted(g.DEPENDS_FORWARD))
    def test_forward_relationship_types_keep_direction(self, rel):
        assert g.build_adjacency([{"from": "a", "to": "b", "relationship_type": rel}]) == {"a": ["b"]}

    @pytest.mark.parametrize("rel", sorted(g.DEPENDS_REVERSE))
    def test_inverse_relationship_types_are_reversed(self, rel):
        # The rule the cloud-ops detector was missing before consolidation: a
        # `used_by`/`hosts`/`runs` edge points at the DEPENDENT, so it flips.
        assert g.build_adjacency([{"from": "a", "to": "b", "relationship_type": rel}]) == {"b": ["a"]}

    def test_absent_relationship_type_is_depends_on(self):
        assert g.build_adjacency([{"from": "a", "to": "b"}]) == {"a": ["b"]}

    def test_half_edges_and_junk_are_skipped_not_half_added(self):
        assert g.build_adjacency([
            {"from": "a"}, {"to": "b"}, {}, "not-an-edge", None,
        ]) == {}

    def test_servicenow_nested_reference_values_resolve_to_the_sys_id(self):
        # A `sysparm_display_value=all` read returns references as mappings; the
        # graph must key on the stable identifier, not the display label.
        adj = g.build_adjacency([{
            "source_ci_id": {"value": "a", "display_value": "App One"},
            "target_ci_id": {"value": "b", "display_value": "DB One"},
        }])
        assert adj == {"a": ["b"]}

    def test_duplicate_edges_collapse_and_neighbours_are_sorted(self):
        adj = g.build_adjacency([
            {"from": "a", "to": "z"}, {"from": "a", "to": "b"}, {"from": "a", "to": "z"},
        ])
        assert adj == {"a": ["b", "z"]}          # deduplicated + deterministic order


class TestBoundedWalk:

    @pytest.fixture
    def chain(self):
        return g.build_adjacency([
            {"from": "a", "to": "b"}, {"from": "b", "to": "c"}, {"from": "c", "to": "d"},
        ])

    def test_returns_shortest_hop_within_the_bound(self, chain):
        assert g.reachable_within(chain, "a", 2) == {"b": 1, "c": 2}

    def test_a_ci_beyond_the_bound_does_not_count(self, chain):
        # MSP-B6 T3-AC2 / MSP-B12: the depth bound is a guarantee, not a hint.
        assert "d" not in g.reachable_within(chain, "a", 2)
        assert g.reachable_within(chain, "a", 1) == {"b": 1}

    def test_start_is_excluded(self, chain):
        assert "a" not in g.reachable_within(chain, "a", 3)

    def test_shortest_hop_wins_when_a_ci_is_reachable_two_ways(self):
        adj = g.build_adjacency([
            {"from": "a", "to": "b"}, {"from": "b", "to": "shared"}, {"from": "a", "to": "shared"},
        ])
        assert g.reachable_within(adj, "a", 3)["shared"] == 1

    def test_cycles_terminate(self):
        adj = g.build_adjacency([{"from": "a", "to": "b"}, {"from": "b", "to": "a"}])
        assert g.reachable_within(adj, "a", 5) == {"b": 1}

    @pytest.mark.parametrize("hops", [0, -1])
    def test_non_positive_bound_reaches_nothing(self, chain, hops):
        assert g.reachable_within(chain, "a", hops) == {}

    def test_unknown_or_empty_start_reaches_nothing(self, chain):
        assert g.reachable_within(chain, "nope", 2) == {}
        assert g.reachable_within(chain, "", 2) == {}


# ── the behaviour that rides on it stays correct ────────────────────────────────


_BLOCK = {
    "ci_graph": {"edges": [
        {"from": "ci-s1", "to": "ci-storage"},
        {"from": "ci-s2", "to": "ci-storage"},
        {"from": "ci-s3", "to": "ci-mid"},
        {"from": "ci-mid", "to": "ci-storage"},
    ]},
    "service_incidents": [
        {"service": "billing", "ci": "ci-s1", "incident_ids": ["INC1", "INC2"]},
        {"service": "orders", "ci": "ci-s2", "incident_ids": ["INC3"]},
        {"service": "search", "ci": "ci-s3", "incident_ids": ["INC4"]},
    ],
    "event_signatures": [
        {"signature": "1:abc", "ci": "ci-storage", "window_overlap": True},
    ],
}

_THRESHOLDS = {"max_hops": 2, "min_services": 3, "require_event_corroboration": True}


class TestCloudOpsHotspotOnTheSharedEngine:

    def test_fires_with_correct_hops_through_the_shared_walk(self):
        [result] = hotspot.detect(sn_data={"cloud_ops": _BLOCK})
        evidence = result.raw_evidence["finding_contract"]["evidence"]
        assert result.raw_evidence["common_ci"] == "ci-storage"
        assert result.raw_evidence["service_count"] == 3
        assert evidence["hops_by_service"] == {"billing": 1, "orders": 1, "search": 2}

    def test_evaluate_agrees_with_detect(self):
        assert hotspot.evaluate(sn_data={"cloud_ops": _BLOCK}).fired is True

    def test_depth_bound_still_decides_firing(self):
        # Tightening the bound to 1 hop drops `search` (2 hops away), leaving 2
        # services — below min_services, so the hotspot must not fire.
        tight = dict(_THRESHOLDS, max_hops=1)
        assert hotspot._hotspots(_BLOCK, tight) == []

    def test_no_dependency_edges_never_fires(self):
        blind = {k: v for k, v in _BLOCK.items() if k != "ci_graph"}
        assert hotspot._hotspots(blind, _THRESHOLDS) == []
