"""2.0-B4 T3 (AT-812) — detector-portability proof (AC2).

AC2: "Two existing detectors, ported to normalised concepts, produce identical
findings on golden fixtures."

The proof is a round-trip: the RAW Service Cloud approval block (the golden fixture)
is fed to the ORIGINAL detectors; the SAME block, mapped to normalised concepts, is
fed to the concept-native PORTS; the two must produce identical ``DetectorResult``
lists. An explicit expected firing set is asserted against BOTH so a shared bug
cannot make "original == ported" pass vacuously.

The two ported detectors:
  * ``APPROVAL_BOTTLENECK`` — discovery.detectors.approval_delay  → detect_approval_bottleneck
  * ``PERMISSION_BOTTLENECK`` — discovery.detectors.permission_bottleneck → detect_permission_bottleneck
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.provenance import EvidencePointer
from discovery.concepts.model import ActorGroup, Approval, EntityReference
from discovery.concepts.portable_detectors import (
    detect_approval_bottleneck,
    detect_permission_bottleneck,
)
from discovery.detectors import approval_delay, permission_bottleneck

ORG_ID = "acme"

# ── Proof-scaffold mapper (2.0-B4 T3) ───────────────────────────────────────────
#
# This maps the golden `approval_processes` block onto Approval + ActorGroup concepts
# to FEED the portability proof below. It lives in the test, not in production: T2's
# `discovery/concepts/mappers/` package is the sole connector→concept layer, and it
# maps a single ProcessInstance record (map_process_instance_approval). This proof
# instead works on the AGGREGATE, detector-visible `approval_processes` block — the
# exact shape the service_cloud approval detectors (approval_delay / permission_
# bottleneck) read — carrying the source's pre-computed measurements
# (avg_delay_days / bottleneck_score / pending_count) on the concept's `attributes`
# bag, and the approver_count as a normalised ActorGroup.member_count. Deterministic
# so the proof compares byte-for-byte.
MAPPER_OBSERVED_AT = "2026-01-01T00:00:00Z"
_SF = "salesforce"


def _observed_provenance(source_artifact: str) -> dict:
    return EvidencePointer.observed(
        source_system=_SF,
        source_artifact=source_artifact,
        source_timestamp=MAPPER_OBSERVED_AT,
    ).to_dict()


def map_service_cloud_approvals(sf_data, *, org_id, observed_at=MAPPER_OBSERVED_AT):
    """Golden `approval_processes` block → [ActorGroup, Approval] per process, in
    order (group before approval). approver_count → ActorGroup.member_count (a
    normalised aggregate, never a roster); the source scores ride on Approval.attributes."""
    if not org_id:
        raise ValueError("org_id is required — every concept is org-scoped")
    out = []
    for ap in sf_data.get("approval_processes") or []:
        process_name = str(ap.get("process_name") or "")
        anchor = process_name or f"approval-process-{len(out)}"
        approver_count = int(ap.get("approver_count", 0) or 0)
        pending_count = int(ap.get("pending_count", 0) or 0)
        avg_delay_days = float(ap.get("avg_delay_days", 0.0) or 0.0)
        bottleneck_score = float(ap.get("bottleneck_score", 0.0) or 0.0)
        provenance = _observed_provenance(anchor)
        out.append(ActorGroup(
            org_id=org_id, source_system=_SF, signal_id=f"approval-group:{anchor}",
            observed_at=observed_at, provenance=provenance,
            group_type="role", name=anchor, member_count=approver_count,
        ))
        out.append(Approval(
            org_id=org_id, source_system=_SF, signal_id=f"approval:{anchor}",
            observed_at=observed_at, provenance=provenance,
            decision="pending" if pending_count > 0 else "approved",
            approval_type="other",
            approver_group=EntityReference(
                entity_type="team", source_system=_SF,
                source_record_id=anchor, display_name=process_name or None,
            ),
            attributes={
                "process_name": process_name, "avg_delay_days": avg_delay_days,
                "bottleneck_score": bottleneck_score, "pending_count": pending_count,
            },
        ))
    return out
GOLDEN = (
    Path(__file__).resolve().parents[1]
    / ".."
    / "discovery"
    / "concepts"
    / "fixtures"
    / "portability_approvals_golden.json"
)

# The individual approver ids the fixture carries — must never appear on a concept.
INDIVIDUAL_IDS = {
    "005xx000002INS1", "005xx000002INS2", "005xx000002INS3", "005xx000002INS4",
    "005xx000002INS5", "005xx000002INS6", "005xx000002INS7", "005xx000002INS8",
    "005xx000002INS9", "005xx000002INSA", "005xx000002INSB", "005xx000002INSC",
    "005xx000002INSD", "005xx000002INSE", "005xx000002INSF",
}

# The branch coverage the golden fixture is built to exercise (see its _meta).
APPROVAL_EXPECTED_FIRING = {
    "Underwriting Referral Review",   # combined: delay 6.5>3 AND bottleneck 37>10
    "Claim Settlement Authority",     # combined: delay 4.2>3 AND bottleneck 13.7>10
    "Change Freeze Sign-off",         # severe:   delay 8.0>7 (bottleneck 5<=10)
    "Automated Risk Gate",            # combined: delay 5>3 AND bottleneck 20>10 (approvers 0)
}
PERMISSION_EXPECTED_FIRING = {
    "Underwriting Referral Review",   # bottleneck 37>10, approvers 2>0
    "Claim Settlement Authority",     # bottleneck 13.7>10, approvers 3>0
    "Access Request Review",          # bottleneck 13>10, approvers 4>0
}


@pytest.fixture(scope="module")
def raw():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def concepts(raw):
    return map_service_cloud_approvals(raw, org_id=ORG_ID)


def _names(results):
    return [r.raw_evidence["process_name"] for r in results]


# ── AC2 — identical findings ────────────────────────────────────────────────────

class TestPortsProduceIdenticalFindings:

    def test_approval_bottleneck_port_is_byte_identical(self, raw, concepts):
        original = approval_delay.detect(raw)
        ported = detect_approval_bottleneck(concepts)
        assert ported == original, (
            "the concept-native APPROVAL_BOTTLENECK produced different findings"
        )

    def test_permission_bottleneck_port_is_byte_identical(self, raw, concepts):
        original = permission_bottleneck.detect(raw)
        ported = detect_permission_bottleneck(concepts)
        assert ported == original, (
            "the concept-native PERMISSION_BOTTLENECK produced different findings"
        )

    def test_the_proof_is_not_vacuous_both_detectors_actually_fire(self, raw):
        """Identity is meaningless if nothing fires — pin real, multi-finding output."""
        assert len(approval_delay.detect(raw)) >= 3
        assert len(permission_bottleneck.detect(raw)) >= 3

    def test_approval_matches_expected_firing_on_both_sides(self, raw, concepts):
        """Original AND port fire on exactly the expected gates — a shared bug that
        agreed on the wrong answer would fail here."""
        assert set(_names(approval_delay.detect(raw))) == APPROVAL_EXPECTED_FIRING
        assert set(_names(detect_approval_bottleneck(concepts))) == APPROVAL_EXPECTED_FIRING

    def test_permission_matches_expected_firing_on_both_sides(self, raw, concepts):
        assert set(_names(permission_bottleneck.detect(raw))) == PERMISSION_EXPECTED_FIRING
        assert set(_names(detect_permission_bottleneck(concepts))) == PERMISSION_EXPECTED_FIRING

    def test_field_by_field_identity_including_evidence(self, raw, concepts):
        """Every DetectorResult field matches — id, source, metric, threshold,
        provenance_type, and the full raw_evidence dict."""
        for orig, port in [
            (approval_delay.detect(raw), detect_approval_bottleneck(concepts)),
            (permission_bottleneck.detect(raw), detect_permission_bottleneck(concepts)),
        ]:
            assert len(orig) == len(port)
            for o, p in zip(orig, port):
                assert o.detector_id == p.detector_id
                assert o.signal_source == p.signal_source
                assert o.metric_value == p.metric_value
                assert o.threshold == p.threshold
                assert o.provenance_type == p.provenance_type
                assert o.raw_evidence == p.raw_evidence

    def test_the_approver_count_the_port_reports_comes_from_the_actor_group(
        self, raw, concepts
    ):
        """The discriminating count is read from the normalised ActorGroup.member_count,
        and it equals what the original read from the connector field."""
        by_name = {
            ap["process_name"]: int(ap["approver_count"])
            for ap in raw["approval_processes"]
        }
        for r in detect_permission_bottleneck(concepts):
            assert r.raw_evidence["approver_count"] == by_name[r.raw_evidence["process_name"]]


# ── The ports are genuinely concept-native ──────────────────────────────────────

class TestPortsAreConceptNative:

    def test_ports_ignore_raw_connector_dicts(self, raw):
        """Fed the raw connector shape (plain dicts, not concept instances) the ports
        emit nothing — they respond only to normalised concept objects."""
        assert detect_approval_bottleneck(raw["approval_processes"]) == []
        assert detect_permission_bottleneck(raw["approval_processes"]) == []

    def test_port_module_names_no_connector_field_path(self):
        """The ports must not reach into a connector shape: no ``sf_data`` /
        ``approval_processes`` / ``sn_data`` / ``jira_data`` anywhere in the module."""
        src = (
            Path(__file__).resolve().parents[1]
            / ".."
            / "discovery"
            / "concepts"
            / "portable_detectors.py"
        ).read_text(encoding="utf-8").lower()
        # Strip the module docstring, which legitimately *names* the old field path
        # when explaining what changed. Only the executable body must be clean.
        body = src.split('"""', 2)[-1] if src.count('"""') >= 2 else src
        for forbidden in ("sf_data", "approval_processes", "sn_data", "jira_data"):
            assert forbidden not in body, (
                f"the port reaches into a connector shape: {forbidden!r}"
            )

    def test_ports_read_concept_classes(self, concepts):
        """Sanity: the stream the ports consume is concept instances, not dicts."""
        assert any(isinstance(s, Approval) for s in concepts)
        assert any(isinstance(s, ActorGroup) for s in concepts)


# ── The mapper normalises honestly ──────────────────────────────────────────────

class TestMapperNormalisation:

    def test_actor_group_member_count_equals_approver_count(self, raw, concepts):
        by_name = {
            ap["process_name"]: int(ap["approver_count"])
            for ap in raw["approval_processes"]
        }
        groups = [s for s in concepts if isinstance(s, ActorGroup)]
        assert groups
        for g in groups:
            assert g.member_count == by_name[g.name]

    def test_no_individual_is_ever_surfaced(self, concepts):
        """Groups, never individuals: the approver roster is dropped, no concept
        carries an individual id, and no reference is a person."""
        blob = json.dumps([s.to_dict() for s in concepts])
        leaked = sorted(i for i in INDIVIDUAL_IDS if i in blob)
        assert leaked == [], f"individual approver ids leaked onto concepts: {leaked}"
        for s in concepts:
            for ref in ([s.approver_group] if isinstance(s, Approval) else []):
                if ref is not None:
                    assert ref.entity_type != "person"

    def test_approver_group_is_a_group_reference(self, concepts):
        for s in concepts:
            if isinstance(s, Approval):
                assert isinstance(s.approver_group, EntityReference)
                assert s.approver_group.entity_type == "team"

    def test_pending_zero_maps_to_an_approved_decision(self, concepts):
        """The one process with pending_count 0 (Policy Endorsement Approval) must
        still be represented — as a decided gate, not dropped."""
        approvals = {
            s.attributes["process_name"]: s
            for s in concepts
            if isinstance(s, Approval)
        }
        assert approvals["Policy Endorsement Approval"].decision == "approved"
        assert approvals["Underwriting Referral Review"].decision == "pending"

    def test_every_process_becomes_exactly_one_group_and_one_approval(self, raw, concepts):
        n = len(raw["approval_processes"])
        assert sum(isinstance(s, ActorGroup) for s in concepts) == n
        assert sum(isinstance(s, Approval) for s in concepts) == n

    def test_every_concept_carries_valid_observed_provenance(self, concepts):
        assert concepts
        for s in concepts:
            ptr = EvidencePointer.from_dict(s.provenance)
            assert ptr.is_valid()
            assert ptr.origin == "observed"
            assert s.observed_at == MAPPER_OBSERVED_AT

    def test_mapping_is_deterministic(self, raw):
        once = [s.to_dict() for s in map_service_cloud_approvals(raw, org_id=ORG_ID)]
        twice = [s.to_dict() for s in map_service_cloud_approvals(raw, org_id=ORG_ID)]
        assert once == twice

    def test_mapper_requires_an_org(self, raw):
        with pytest.raises(ValueError, match="org_id"):
            map_service_cloud_approvals(raw, org_id="")
