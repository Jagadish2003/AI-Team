"""Contract tests for 2.0-D1 T1 — Financial Services Cloud pack registration.

T1 registers `financial_services_cloud` on the EXISTING pack framework — a
registry entry plus its supporting label data, copying the shape of the `ncino`
entry (same connector, same BFSI domain family, same compliance posture). No new
pack machinery.

Covers the T1-owned slices of the story's acceptance criteria:

  * AC3 — findings and reports use FSC terminology, DRIVEN BY PACK CONFIG. The
    terminology lives in `financial_services_cloud_ui_labels.json`, not in code,
    and resolves through `get_ui_labels()` (the same path runner.py uses to stamp
    title / category / description / s9_roadmap / s10_exec onto every finding).
  * AC4 — delivered with zero template-model and zero scoring-engine code
    changes: the registry entry declares only keys already used by shipped packs.
  * AC5 — no output names an individual. Enforced here at the only layer T1 owns
    (the label wording); the detector-output sweep lands with the detectors (T2).

Plus the task's own definition of done: the pack resolves through `get_pack()` by
id, its labels load, its version resolves and is stamped, and its `llm_context`
carries the BFSI guardrail.

One caution the tests below deliberately guard: `get_pack()` FALLS BACK to
service_cloud for an unknown pack id and logs a WARNING rather than raising, so a
typo in the registration key yields Service Cloud detectors and a green-looking
run. Every assertion here checks the RESOLVED pack id explicitly rather than
inferring correct wiring from the absence of an error.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os

import pytest


def _pack_config():
    try:
        import backend.discovery.packs.pack_config as m
    except ModuleNotFoundError:
        import discovery.packs.pack_config as m
    return m


PACK_ID = "financial_services_cloud"

# The five FSC detectors the story names, keyed by their planned DETECTOR_ID.
# The label file carries wording for all five at registration; the modules land
# in T2, which then adds their paths to the registry's `detectors` list.
DETECTOR_IDS = (
    "FSC_SERVICING_REQUEST_RECURRENCE",
    "FSC_REFERRAL_HANDOFF_FRICTION",
    "FSC_APPROVAL_REVIEW_CYCLE",
    "FSC_SERVICE_QUEUE_AGEING",
    "FSC_CROSS_OBJECT_REWORK",
)

# Label fields runner.py reads per detector (runner.py:2796-2803).
LABEL_FIELDS = ("s6_title", "s6_desc", "s7_category", "s9_roadmap", "s10_exec")


def _labels():
    return _pack_config().get_ui_labels(PACK_ID)


def _detector_label_entries():
    """Every per-detector label entry (skipping the `_meta` / `_terminology` blocks)."""
    return {
        key: value
        for key, value in (_labels() or {}).items()
        if not key.startswith("_")
    }


# ── Registration: identity resolves explicitly, not by fallback ──────────────────

class TestPackRegistration:

    def test_pack_id_in_registry(self):
        assert PACK_ID in _pack_config().PACK_REGISTRY

    def test_pack_id_in_list_packs(self):
        assert PACK_ID in _pack_config().list_packs()

    def test_get_pack_resolves_to_fsc_not_the_service_cloud_fallback(self):
        """The registration key is spelled correctly.

        get_pack() logs-and-falls-back on an unknown id, so asserting the
        RESOLVED packId is the only way to prove the key is right — a run that
        merely completes proves nothing.
        """
        m = _pack_config()
        assert m.get_pack(PACK_ID)["packId"] == PACK_ID
        assert m.get_pack(PACK_ID)["packId"] != m.DEFAULT_PACK

    def test_a_typo_in_the_pack_id_would_silently_yield_service_cloud(self):
        """Documents exactly the failure mode the test above defends against."""
        m = _pack_config()
        assert m.get_pack("financial_services_clod")["packId"] == "service_cloud"

    def test_pack_name(self):
        assert _pack_config().get_pack(PACK_ID)["packName"] == "Financial Services Cloud"

    def test_domain_and_pack_domain(self):
        pack = _pack_config().get_pack(PACK_ID)
        assert pack["domain"] == PACK_ID
        assert pack["pack_domain"] == PACK_ID

    def test_get_pack_domain_resolves(self):
        assert _pack_config().get_pack_domain(PACK_ID) == PACK_ID

    def test_required_registry_keys_present(self):
        pack = _pack_config().get_pack(PACK_ID)
        for key in ("packId", "packVersion", "packName", "domain", "pack_domain",
                    "detectors", "ui_labels_path", "llm_context"):
            assert key in pack, f"registry entry missing {key!r}"

    def test_is_financial_services_cloud_pack_true(self):
        assert _pack_config().is_financial_services_cloud_pack(PACK_ID) is True

    @pytest.mark.parametrize(
        "other",
        ["service_cloud", "ncino", "strs_benefits", "sqlserver_opsignal",
         "github_engineering", "enterprise_ops", "cloud_ops", "security_ops",
         None, "", "nope"],
    )
    def test_is_financial_services_cloud_pack_false_for_others(self, other):
        assert _pack_config().is_financial_services_cloud_pack(other) is False

    def test_pack_selectable_through_the_registry_alone(self):
        """Selection needs no discovery-runner special case (no new machinery)."""
        m = _pack_config()
        assert m.get_pack(PACK_ID)["packId"] == PACK_ID
        assert isinstance(m.get_detector_modules(PACK_ID), list)

    def test_existing_packs_undisturbed(self):
        m = _pack_config()
        for pid in ("service_cloud", "ncino", "strs_benefits", "sqlserver_opsignal",
                    "github_engineering", "enterprise_ops", "cloud_ops", "security_ops"):
            assert pid in m.PACK_REGISTRY, f"{pid} missing after {PACK_ID} added"
        assert m.DEFAULT_PACK == "service_cloud"

    def test_sibling_pack_predicates_still_discriminate(self):
        """The new domain must not be swept up by an existing pack's predicate."""
        m = _pack_config()
        assert m.is_ncino_pack(PACK_ID) is False
        assert m.is_security_ops_pack(PACK_ID) is False
        assert m.is_cloud_ops_pack(PACK_ID) is False
        assert m.is_ncino_pack("ncino") is True


# ── Versioning: present, resolves, and bumping is enforced ───────────────────────

class TestVersioning:

    def test_pack_declares_a_version(self):
        assert _pack_config().get_pack(PACK_ID).get("packVersion")

    def test_get_pack_version_resolves_from_the_registry(self):
        m = _pack_config()
        assert m.get_pack_version(PACK_ID) == m.PACK_REGISTRY[PACK_ID]["packVersion"]

    def test_version_is_not_the_default_fallback_value(self):
        """The version must be declared explicitly, not inherited from the default."""
        assert "packVersion" in _pack_config().PACK_REGISTRY[PACK_ID]

    def test_version_is_semver_shaped(self):
        version = _pack_config().get_pack_version(PACK_ID)
        parts = version.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts), version


class TestVersionBumpGuard:
    """A change to this pack's surface REQUIRES an intentional version bump.

    The version is stamped onto every opportunity this pack produces and is read
    directly by 2.0-A2 outcome tracking's confounder detection, which flags a
    measurement taken across a version boundary. A version that never moves
    silently breaks that story, so the pack surface is pinned here: when the
    detector list or the label set changes, this fingerprint changes and the test
    fails, forcing the author to bump `packVersion` in pack_config.py and update
    the pins below. T2 (detectors) and T3 (scorer calibration) both trip this.
    """

    # Pinned to financial_services_cloud packVersion 1.2.0.
    #
    # History of this pin — each step is an intentional bump this guard forced:
    #   1.0.0  T1 registration (empty detector list, five label entries).
    #   1.1.0  T2 added the five detector modules (a behaviour change).
    #   1.2.0  T3 added the scorer calibration (a pack-logic change).
    #
    # T3 also WIDENED the fingerprint to cover the calibration surface (the
    # dimension weights and the per-detector automation shape). Before that, a
    # weight edit — which changes the ranked order of every FSC finding — would not
    # have tripped this guard, so a calibration change could have shipped without
    # the version moving. 2.0-A2 confounder detection reads that version.
    PINNED_VERSION = "1.2.0"
    PINNED_FINGERPRINT = "d47f15da3e2d4d362b01eb5eb43a8d10c6a3fe7e29f56f40b6b4f8d6ffba2c2b"

    @staticmethod
    def _surface_fingerprint():
        m = _pack_config()
        labels = _detector_label_entries()
        surface = {
            "detectors": m.get_detector_modules(PACK_ID),
            "label_keys": sorted(labels),
            "guardrails": {
                key: bool(entry.get("compliance_guardrail"))
                for key, entry in sorted(labels.items())
            },
        }
        # T3: the scoring surface is part of the pack's behaviour, so a calibration
        # edit must require a version bump exactly as a detector change does.
        try:
            from discovery.packs.financial_services_cloud_config import get_calibration
        except ModuleNotFoundError:  # pragma: no cover
            from backend.discovery.packs.financial_services_cloud_config import (  # type: ignore
                get_calibration,
            )
        calibration = get_calibration()
        surface["impact_weights"] = calibration.impact_weights
        surface["automation_shape"] = calibration.automation_shape.get("by_detector", {})

        blob = json.dumps(surface, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def test_version_matches_pin(self):
        assert _pack_config().get_pack_version(PACK_ID) == self.PINNED_VERSION, (
            "financial_services_cloud packVersion changed. If that was an "
            "intentional bump, update PINNED_VERSION/PINNED_FINGERPRINT here."
        )

    def test_surface_change_requires_version_bump(self):
        assert self._surface_fingerprint() == self.PINNED_FINGERPRINT, (
            "The financial_services_cloud detector/label surface changed. Bump "
            "packVersion in pack_config.py, then update PINNED_VERSION and "
            "PINNED_FINGERPRINT in this test — the intentional pack-version "
            "update that 2.0-A2 confounder detection depends on."
        )


# ── Detector list: honest at registration, guarded for T2 ────────────────────────

class TestDetectorPaths:

    def test_detectors_is_a_list(self):
        assert isinstance(_pack_config().get_detector_modules(PACK_ID), list)

    def test_every_registered_detector_path_is_importable(self):
        """A real guard since T2 populated the list. The registry-wide sweeps in
        discovery/tests/test_focus_affinity.py and
        tests/contract/test_relationship_mapping.py import these same paths, so a
        path registered before its module exists breaks the build."""
        for path in _pack_config().get_detector_modules(PACK_ID):
            assert isinstance(path, str) and path
            importlib.import_module(path)

    def test_pack_ships_the_five_fsc_detectors(self):
        """T2 populated the detector list the T1 label file was keyed for."""
        paths = _pack_config().get_detector_modules(PACK_ID)
        assert len(paths) == 5, paths
        for fragment in ("servicing_request_recurrence", "referral_handoff_friction",
                         "approval_review_cycle", "service_queue_ageing",
                         "cross_object_rework"):
            assert any(fragment in p for p in paths), f"no detector for {fragment}"

    def test_each_detector_id_matches_a_label_entry(self):
        """The label file's keys must be the detectors' real DETECTOR_IDs.

        T1 keyed the labels on PLANNED ids. If T2 had named a module's
        DETECTOR_ID differently, runner.py's per-detector label lookup would miss
        and every FSC finding would render with a raw detector id as its title —
        silently, since a missing label falls back rather than raising.
        """
        m = _pack_config()
        labels = _labels()
        for path in m.get_detector_modules(PACK_ID):
            detector_id = importlib.import_module(path).DETECTOR_ID
            assert detector_id in labels, (
                f"{path} declares DETECTOR_ID {detector_id!r} which has no entry in "
                f"the FSC label file — its findings would render untitled"
            )

    def test_label_file_has_no_entry_without_a_detector(self):
        """The reverse direction: no orphan label entry."""
        m = _pack_config()
        shipped = {
            importlib.import_module(p).DETECTOR_ID
            for p in m.get_detector_modules(PACK_ID)
        }
        orphans = set(_detector_label_entries()) - shipped
        assert orphans == set(), f"label entries with no detector: {orphans}"


# ── AC3 — terminology is pack config, in FSC language ────────────────────────────

class TestTerminologyIsPackConfig:

    def test_ui_labels_path_registered_and_file_exists(self):
        path = _pack_config().get_pack(PACK_ID).get("ui_labels_path")
        assert path is not None
        assert path.endswith("financial_services_cloud_ui_labels.json")
        assert os.path.isfile(path), f"ui_labels_path file not found: {path}"

    def test_get_ui_labels_loads_the_file(self):
        labels = _labels()
        assert labels is not None and isinstance(labels, dict)

    def test_labels_cover_all_five_planned_detectors(self):
        labels = _labels()
        for det_id in DETECTOR_IDS:
            assert det_id in labels, f"no FSC labels for {det_id}"

    def test_label_keys_are_uppercase_detector_ids(self):
        for key in _detector_label_entries():
            assert key == key.upper(), f"label key {key!r} is not an uppercase DETECTOR_ID"

    @pytest.mark.parametrize("det_id", DETECTOR_IDS)
    @pytest.mark.parametrize("field", LABEL_FIELDS)
    def test_required_label_field_is_a_non_empty_string(self, det_id, field):
        value = _labels()[det_id].get(field)
        assert isinstance(value, str) and value.strip(), (
            f"{det_id}['{field}'] must be a non-empty string — runner.py stamps "
            f"it onto every finding this detector produces"
        )

    @pytest.mark.parametrize("det_id", DETECTOR_IDS)
    def test_compliance_guardrail_key_is_always_present(self, det_id):
        """Present on every entry — null where the detector touches no approval
        or review, populated where it does (asserted separately below)."""
        assert "compliance_guardrail" in _labels()[det_id]

    def test_fsc_vocabulary_is_declared_in_config(self):
        """The FSC terms the story names live in the label file, not in code."""
        terminology = _labels().get("_terminology")
        assert isinstance(terminology, dict) and terminology
        for term in ("household", "relationship_group", "financial_account",
                     "service_process", "referral"):
            assert term in terminology, f"_terminology missing {term!r}"
            assert terminology[term].strip(), f"_terminology[{term!r}] is empty"

    def test_label_wording_speaks_fsc_not_generic_salesforce(self):
        """Every detector's user-visible copy uses FSC domain language."""
        fsc_terms = ("household", "relationship group", "financial account",
                     "service process", "referral", "service queue", "client")
        for det_id, entry in _detector_label_entries().items():
            blob = " ".join(str(entry.get(f, "")) for f in LABEL_FIELDS).lower()
            assert any(term in blob for term in fsc_terms), (
                f"{det_id} labels use no FSC terminology: {blob[:120]!r}"
            )

    def test_label_wording_carries_no_lending_pack_leakage(self):
        """FSC is not nCino — copied-template wording must have been rewritten."""
        # nCino DOMAIN concepts. "financial spreading" (not the bare word
        # "spreading", which is ordinary English) is the lending concept.
        leaked = ("borrower", "covenant", "loan origination", "financial spreading",
                  "credit memo", "underwriter")
        for det_id, entry in _detector_label_entries().items():
            blob = " ".join(str(entry.get(f, "")) for f in LABEL_FIELDS).lower()
            for term in leaked:
                assert term not in blob, (
                    f"{det_id} labels carry nCino lending wording {term!r}"
                )


class TestMetaHonesty:
    """Placeholder wording must announce itself.

    ncino_ui_labels.json carries a `_meta.status` and an explicit
    placeholders-until-sign-off note. The same convention applies here, because
    the alternative is unreviewed FSC copy reaching an executive report with no
    signal that it was never approved.
    """

    def test_meta_block_present(self):
        assert isinstance(_labels().get("_meta"), dict)

    def test_meta_declares_a_status(self):
        status = _labels()["_meta"].get("status")
        assert isinstance(status, str) and status.strip()

    def test_status_does_not_claim_sme_approval_while_the_note_says_placeholder(self):
        """The status must honestly reflect whether the FSC wording was reviewed."""
        meta = _labels()["_meta"]
        status = meta["status"].upper()
        note = str(meta.get("note", "")).upper()
        if "PLACEHOLDER" in note:
            assert "APPROVED" not in status or "PENDING" in status, (
                "status claims approval while the note admits placeholder text"
            )
        assert "PENDING" in status or "APPROVED" in status, (
            f"status must state review posture explicitly, got {status!r}"
        )

    def test_meta_note_explains_the_replacement_obligation(self):
        note = str(_labels()["_meta"].get("note", "")).lower()
        assert "sme" in note and ("replace" in note or "approved" in note)


# ── BFSI compliance guardrail (carried over from the nCino entry) ────────────────

class TestComplianceGuardrail:

    def test_llm_context_is_non_empty(self):
        ctx = _pack_config().get_llm_context(PACK_ID)
        assert isinstance(ctx, str) and ctx.strip()

    def test_llm_context_speaks_fsc_language(self):
        ctx = _pack_config().get_llm_context(PACK_ID).lower()
        for term in ("household", "relationship group", "financial account",
                     "service process", "referral"):
            assert term in ctx, f"llm_context missing FSC term {term!r}"

    def test_llm_context_forbids_automated_credit_and_compliance_decisions(self):
        """Equivalent language to the nCino entry and to the financial_services
        industry suffix ('never suggest automated credit or compliance decisions')."""
        ctx = _pack_config().get_llm_context(PACK_ID).lower()
        assert "never suggest automated credit" in ctx
        assert "compliance decision" in ctx

    def test_llm_context_requires_human_approval(self):
        ctx = _pack_config().get_llm_context(PACK_ID).lower()
        assert "human approval" in ctx

    def test_llm_context_names_the_regulatory_regime(self):
        ctx = _pack_config().get_llm_context(PACK_ID).lower()
        for regulator in ("fca", "sec", "occ"):
            assert regulator in ctx, f"llm_context missing {regulator.upper()}"

    def test_llm_context_matches_the_ncino_guardrail_posture(self):
        """The FSC pack serves the same regulated BFSI market as nCino, so its
        guardrail must be no weaker."""
        m = _pack_config()
        fsc = m.get_llm_context(PACK_ID).lower()
        ncino = m.get_llm_context("ncino").lower()
        for signal in ("automated", "credit", "human approval"):
            assert signal in ncino  # the posture being carried over
            assert signal in fsc, f"FSC guardrail missing {signal!r}"

    def test_approval_and_review_detectors_carry_a_populated_guardrail(self):
        """Any detector touching approvals or reviews needs a real guardrail."""
        for det_id, entry in _detector_label_entries().items():
            if "APPROVAL" in det_id or "REVIEW" in det_id:
                guardrail = entry.get("compliance_guardrail")
                assert isinstance(guardrail, str) and guardrail.strip(), (
                    f"{det_id} touches approvals/reviews but has no "
                    f"compliance_guardrail"
                )

    def test_approval_guardrail_forbids_automated_decisions(self):
        guardrail = _labels()["FSC_APPROVAL_REVIEW_CYCLE"]["compliance_guardrail"].lower()
        assert "no automated" in guardrail
        assert "human approval" in guardrail

    def test_referral_guardrail_forbids_automated_suitability_assessment(self):
        guardrail = _labels()["FSC_REFERRAL_HANDOFF_FRICTION"]["compliance_guardrail"].lower()
        assert "no automated" in guardrail
        assert "suitability" in guardrail or "recommendation" in guardrail

    def test_at_least_three_detectors_carry_a_guardrail(self):
        populated = [
            det_id for det_id, entry in _detector_label_entries().items()
            if isinstance(entry.get("compliance_guardrail"), str)
            and entry["compliance_guardrail"].strip()
        ]
        assert len(populated) >= 3, f"only {populated} carry a guardrail"


# ── AC5 — no output names an individual (label layer) ────────────────────────────

class TestNoIndividualsNamed:
    """FSC aggregates to households, relationship groups, teams, queues and
    service processes. The detector-output sweep arrives with the detectors (T2);
    T1 owns the wording, so the wording is swept here."""

    # Person-shaped references. Role nouns used as a TEAM ("relationship team")
    # are fine; the singular actor forms below are not.
    FORBIDDEN = (
        "assignee", "individual client", "individual adviser", "individual banker",
        "named adviser", "named banker", "each adviser", "each banker",
        "the adviser's", "the banker's", "@",
    )

    def test_no_detector_label_names_an_individual(self):
        for det_id, entry in _detector_label_entries().items():
            blob = " ".join(
                str(value) for value in entry.values() if isinstance(value, str)
            ).lower()
            for token in self.FORBIDDEN:
                assert token not in blob, (
                    f"{det_id} label text references an individual via {token!r}"
                )

    def test_llm_context_forbids_naming_individuals(self):
        ctx = _pack_config().get_llm_context(PACK_ID).lower()
        assert "never an individual" in ctx

    def test_meta_records_the_aggregation_floor(self):
        floor = str(_labels()["_meta"].get("aggregation_floor", "")).lower()
        assert "individual" in floor


# ── AC4 — no new pack machinery, no engine changes ───────────────────────────────

class TestNoNewMachinery:

    def test_registry_entry_declares_only_keys_shipped_packs_already_use(self):
        """A registry entry, not a framework extension."""
        m = _pack_config()
        established = set()
        for pid, config in m.PACK_REGISTRY.items():
            if pid != PACK_ID:
                established |= set(config)
        novel = set(m.PACK_REGISTRY[PACK_ID]) - established
        assert novel == set(), f"FSC entry introduces new registry keys: {novel}"

    def test_pack_resolves_through_the_shared_public_api(self):
        """No FSC-specific accessor: the standard helpers serve it."""
        m = _pack_config()
        assert m.get_pack(PACK_ID)["packId"] == PACK_ID
        assert m.get_pack_version(PACK_ID)
        assert m.get_pack_domain(PACK_ID) == PACK_ID
        assert isinstance(m.get_detector_modules(PACK_ID), list)
        assert isinstance(m.get_ui_labels(PACK_ID), dict)
        assert m.get_llm_context(PACK_ID)

    def test_multi_pack_normalisation_accepts_fsc_alongside_lending(self):
        """R191-P1 composition surface — FSC and nCino select together (AC2's
        registration-layer slice; the seeded multi-pack run is T4's)."""
        m = _pack_config()
        assert m.normalize_pack_ids([PACK_ID, "ncino"]) == [PACK_ID, "ncino"]
        assert m.normalize_pack_ids([PACK_ID, PACK_ID]) == [PACK_ID]

    def test_pack_config_path_registered_and_exists(self):
        """T2 externalised the firing thresholds, so a config_path now exists.

        (At T1 this asserted `is None`: registration claimed no config file
        because none existed. T2 added one — thresholds and the aggregation floor
        — and T3 fills its calibration.impact_weights.)
        """
        path = _pack_config().get_pack_config_path(PACK_ID)
        assert path is not None
        assert path.endswith("financial_services_cloud_pack_config.json")
        assert os.path.isfile(path), f"config_path file not found: {path}"

    def test_registry_and_config_versions_do_not_drift(self):
        m = _pack_config()
        try:
            from discovery.packs.financial_services_cloud_config import load_fsc_config
        except ModuleNotFoundError:  # pragma: no cover
            from backend.discovery.packs.financial_services_cloud_config import (  # type: ignore
                load_fsc_config,
            )
        assert load_fsc_config().pack_version == m.get_pack_version(PACK_ID)

    def test_scorer_impact_weights_are_populated_by_t3(self):
        """T2 deliberately left these EMPTY (an invented weight silently ranks);
        2.0-D1 T3 filled them with the three dimensions D1 names. Detailed scorer
        coverage lives in test_fsc_scorer.py — this only pins that the T2 placeholder
        was actually replaced rather than left dangling."""
        try:
            from discovery.packs.financial_services_cloud_config import get_calibration
        except ModuleNotFoundError:  # pragma: no cover
            from backend.discovery.packs.financial_services_cloud_config import (  # type: ignore
                get_calibration,
            )
        weights = get_calibration().impact_weights
        assert set(weights) == {"effort_concentration", "breadth", "automation_shape"}
        assert round(sum(weights.values()), 6) == 1.0


# ── Registry honesty (story item 5) ──────────────────────────────────────────────

class TestRegistryHonesty:

    def test_fsc_is_selectable_only_once_it_has_detectors(self):
        """The FSC entry appears on a selectable surface only when the pack ships.

        At T1 the pack is registered but carries no detectors, so it must not yet
        be offered as an industry pack hint. T4 wires the hint AND T2 lands the
        detectors — this invariant holds before and after both, with no edit.
        """
        try:
            from discovery.packs.industry_registry import INDUSTRY_REGISTRY
        except ModuleNotFoundError:  # pragma: no cover
            from backend.discovery.packs.industry_registry import INDUSTRY_REGISTRY

        detectors = _pack_config().get_detector_modules(PACK_ID)
        hinting = [
            industry_id
            for industry_id, config in INDUSTRY_REGISTRY.items()
            if PACK_ID in config.pack_hints
        ]
        if hinting:
            assert detectors, (
                f"{PACK_ID} is offered as a pack hint by {hinting} but ships no "
                f"detectors — an industry cannot recommend a pack that finds nothing"
            )

    def test_every_pack_hint_remains_a_registered_pack(self):
        """Adding a pack must not disturb the registry/industry cross-check."""
        try:
            from discovery.packs.industry_registry import INDUSTRY_REGISTRY
        except ModuleNotFoundError:  # pragma: no cover
            from backend.discovery.packs.industry_registry import INDUSTRY_REGISTRY

        m = _pack_config()
        for industry_id, config in INDUSTRY_REGISTRY.items():
            for hint in config.pack_hints:
                assert hint in m.PACK_REGISTRY, (
                    f"{industry_id} hints unregistered pack {hint!r}"
                )
