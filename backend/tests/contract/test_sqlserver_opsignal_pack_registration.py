"""
Contract tests for T2-S11-A Task T7 — sqlserver_opsignal pack registration.

Covers acceptance criteria that belong to this task:
  AC9  — sqlserver_opsignal registered in PACK_REGISTRY; get_pack() returns
          correct config; is_sqlserver_opsignal_pack() returns True/False correctly.
  AC10 — sqlserver_opsignal_ui_labels.json contains required fields for all
          three detector IDs; get_ui_labels('sqlserver_opsignal') loads it.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------

def _pack_config():
    try:
        import backend.discovery.packs.pack_config as m
    except ModuleNotFoundError:
        import discovery.packs.pack_config as m
    return m


# ---------------------------------------------------------------------------
# AC9 — pack registered, get_pack() correct, is_sqlserver_opsignal_pack()
# ---------------------------------------------------------------------------

class TestAC9:

    def test_pack_id_in_list_packs(self):
        m = _pack_config()
        assert "sqlserver_opsignal" in m.list_packs()

    def test_pack_id_in_registry(self):
        m = _pack_config()
        assert "sqlserver_opsignal" in m.PACK_REGISTRY

    def test_get_pack_returns_correct_pack_id(self):
        m = _pack_config()
        pack = m.get_pack("sqlserver_opsignal")
        assert pack["packId"] == "sqlserver_opsignal"

    def test_get_pack_returns_correct_pack_name(self):
        m = _pack_config()
        pack = m.get_pack("sqlserver_opsignal")
        assert pack["packName"] == "SQL Server Operational Signals"

    def test_get_pack_returns_correct_domain(self):
        m = _pack_config()
        pack = m.get_pack("sqlserver_opsignal")
        assert pack["domain"] == "sqlserver_opsignal"

    def test_get_pack_returns_correct_pack_domain(self):
        m = _pack_config()
        pack = m.get_pack("sqlserver_opsignal")
        assert pack["pack_domain"] == "sqlserver_opsignal"

    def test_get_pack_contains_three_detectors(self):
        m = _pack_config()
        pack = m.get_pack("sqlserver_opsignal")
        assert len(pack["detectors"]) == 3

    def test_get_pack_detector_paths_are_strings(self):
        m = _pack_config()
        for path in m.get_pack("sqlserver_opsignal")["detectors"]:
            assert isinstance(path, str)

    def test_detector_paths_include_ticket_volume_surge(self):
        m = _pack_config()
        detectors = m.get_pack("sqlserver_opsignal")["detectors"]
        assert any("db_ticket_volume_surge" in d for d in detectors)

    def test_detector_paths_include_sla_breach_rate(self):
        m = _pack_config()
        detectors = m.get_pack("sqlserver_opsignal")["detectors"]
        assert any("db_sla_breach_rate" in d for d in detectors)

    def test_detector_paths_include_queue_depth_elevated(self):
        m = _pack_config()
        detectors = m.get_pack("sqlserver_opsignal")["detectors"]
        assert any("db_queue_depth_elevated" in d for d in detectors)

    def test_ui_labels_path_points_to_correct_file(self):
        m = _pack_config()
        path = m.get_pack("sqlserver_opsignal").get("ui_labels_path")
        assert path is not None
        assert "sqlserver_opsignal_ui_labels.json" in path

    def test_llm_context_is_non_empty_string(self):
        m = _pack_config()
        ctx = m.get_pack("sqlserver_opsignal").get("llm_context", "")
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_llm_context_mentions_sql_server(self):
        m = _pack_config()
        ctx = m.get_pack("sqlserver_opsignal")["llm_context"].lower()
        assert "sql server" in ctx

    def test_llm_context_mentions_ticket_volume(self):
        m = _pack_config()
        ctx = m.get_pack("sqlserver_opsignal")["llm_context"].lower()
        assert "ticket volume" in ctx

    def test_llm_context_mentions_sla(self):
        m = _pack_config()
        ctx = m.get_pack("sqlserver_opsignal")["llm_context"].lower()
        assert "sla" in ctx

    def test_llm_context_mentions_queue_depth(self):
        m = _pack_config()
        ctx = m.get_pack("sqlserver_opsignal")["llm_context"].lower()
        assert "queue depth" in ctx

    def test_llm_context_states_no_automated_resolution(self):
        """LLM context must contain the no-automated-resolution guardrail."""
        m = _pack_config()
        ctx = m.get_pack("sqlserver_opsignal")["llm_context"].lower()
        assert "no automated" in ctx or "not automatically" in ctx

    def test_is_sqlserver_opsignal_pack_true_for_this_pack(self):
        m = _pack_config()
        assert m.is_sqlserver_opsignal_pack("sqlserver_opsignal") is True

    def test_is_sqlserver_opsignal_pack_false_for_service_cloud(self):
        m = _pack_config()
        assert m.is_sqlserver_opsignal_pack("service_cloud") is False

    def test_is_sqlserver_opsignal_pack_false_for_ncino(self):
        m = _pack_config()
        assert m.is_sqlserver_opsignal_pack("ncino") is False

    def test_is_sqlserver_opsignal_pack_false_for_strs(self):
        m = _pack_config()
        assert m.is_sqlserver_opsignal_pack("strs_benefits") is False

    def test_is_sqlserver_opsignal_pack_false_for_none(self):
        """None falls back to DEFAULT_PACK (service_cloud), not sqlserver_opsignal."""
        m = _pack_config()
        assert m.is_sqlserver_opsignal_pack(None) is False

    def test_is_sqlserver_opsignal_pack_false_for_unknown(self):
        """Unknown pack_id falls back to DEFAULT_PACK."""
        m = _pack_config()
        assert m.is_sqlserver_opsignal_pack("not_a_pack") is False

    def test_get_detector_modules_returns_three_paths(self):
        m = _pack_config()
        modules = m.get_detector_modules("sqlserver_opsignal")
        assert len(modules) == 3

    def test_get_pack_domain_returns_sqlserver_opsignal(self):
        m = _pack_config()
        assert m.get_pack_domain("sqlserver_opsignal") == "sqlserver_opsignal"

    def test_get_llm_context_returns_string(self):
        m = _pack_config()
        ctx = m.get_llm_context("sqlserver_opsignal")
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_existing_packs_still_present(self):
        """Existing packs must not be disturbed by the new entry."""
        m = _pack_config()
        for pack_id in ("service_cloud", "ncino", "strs_benefits"):
            assert pack_id in m.PACK_REGISTRY, f"{pack_id} missing after sqlserver_opsignal added"

    def test_default_pack_unchanged(self):
        m = _pack_config()
        assert m.DEFAULT_PACK == "service_cloud"

    def test_is_ncino_pack_still_works(self):
        m = _pack_config()
        assert m.is_ncino_pack("ncino") is True
        assert m.is_ncino_pack("sqlserver_opsignal") is False

    def test_is_strs_benefits_pack_still_works(self):
        m = _pack_config()
        assert m.is_strs_benefits_pack("strs_benefits") is True
        assert m.is_strs_benefits_pack("sqlserver_opsignal") is False


# ---------------------------------------------------------------------------
# AC10 — ui_labels JSON correct and loaded by get_ui_labels()
# ---------------------------------------------------------------------------

class TestAC10:

    _DETECTOR_IDS = (
        "DB_TICKET_VOLUME_SURGE",
        "DB_SLA_BREACH_RATE",
        "DB_QUEUE_DEPTH_ELEVATED",
    )
    _REQUIRED_FIELDS = ("s6_title", "agentType", "s6_why", "s6_action")

    def test_get_ui_labels_returns_non_none(self):
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        assert labels is not None

    def test_get_ui_labels_returns_dict(self):
        m = _pack_config()
        assert isinstance(m.get_ui_labels("sqlserver_opsignal"), dict)

    def test_all_three_detector_ids_present(self):
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        for det_id in self._DETECTOR_IDS:
            assert det_id in labels, f"Missing labels for {det_id}"

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    @pytest.mark.parametrize("field", _REQUIRED_FIELDS)
    def test_required_field_present(self, det_id, field):
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        entry = labels.get(det_id, {})
        assert field in entry, f"{det_id} missing '{field}'"

    @pytest.mark.parametrize("det_id", _DETECTOR_IDS)
    @pytest.mark.parametrize("field", _REQUIRED_FIELDS)
    def test_required_field_is_non_empty_string(self, det_id, field):
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        value = labels[det_id][field]
        assert isinstance(value, str) and len(value) > 0, (
            f"{det_id}['{field}'] must be a non-empty string"
        )

    def test_surge_s6_title(self):
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        assert "surge" in labels["DB_TICKET_VOLUME_SURGE"]["s6_title"].lower() or \
               "volume" in labels["DB_TICKET_VOLUME_SURGE"]["s6_title"].lower()

    def test_sla_s6_title_mentions_sla(self):
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        title = labels["DB_SLA_BREACH_RATE"]["s6_title"].lower()
        assert "sla" in title or "compliance" in title or "breach" in title

    def test_queue_s6_title_mentions_queue(self):
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        title = labels["DB_QUEUE_DEPTH_ELEVATED"]["s6_title"].lower()
        assert "queue" in title or "depth" in title or "critical" in title

    def test_agent_type_is_monitoring_agent(self):
        """All three detectors should be Monitoring Agents per spec Section 2f."""
        m = _pack_config()
        labels = m.get_ui_labels("sqlserver_opsignal")
        for det_id in self._DETECTOR_IDS:
            assert labels[det_id]["agentType"] == "Monitoring Agent", (
                f"{det_id}: agentType must be 'Monitoring Agent'"
            )

    def test_ui_labels_json_file_exists_on_disk(self):
        """The JSON file referenced by ui_labels_path must exist."""
        import os
        m = _pack_config()
        path = m.get_pack("sqlserver_opsignal")["ui_labels_path"]
        assert os.path.isfile(path), f"ui_labels_path file not found: {path}"
