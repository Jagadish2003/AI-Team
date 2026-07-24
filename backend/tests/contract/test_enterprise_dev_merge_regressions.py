"""Regression tests for enterprise_applications -> dev integration fixes."""
from __future__ import annotations

from datetime import datetime, timezone


def _db_signal_payload(connector_id: str) -> dict:
    return {
        "connector_id": connector_id,
        "org_id": "org",
        "run_id": "run",
        "schema_name": "dbo",
        "table_name": "ServiceTickets",
        "ticket_volume": {
            "recent_vs_baseline": 2.0,
            "recent_7d_avg": 40.0,
            "avg_daily": 20.0,
            "peak_daily": 55,
            "peak_date": "2026-05-30",
            "total_90d": 1800,
            "degraded_signal": False,
        },
        "sla_breach": {
            "breach_rate_pct": 20.0,
            "breached_count": 40,
            "total_tickets_30d": 200,
            "degraded_signal": False,
        },
        "queue_depth": {
            "p1_p2_open": 30,
            "total_open": 100,
            "oldest_ticket_hours": 120.0,
            "by_priority": {
                "P1": {"count": 15, "avg_age_hours": 60.0},
                "P2": {"count": 15, "avg_age_hours": 40.0},
            },
            "degraded_signal": False,
        },
    }


def _patch_runner_side_effects(monkeypatch, runner):
    monkeypatch.setattr(runner, "record_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runner,
        "_snapshot_detector_evaluations",
        # R18-C2 pack provenance consumes the snapshot's documented completion
        # timestamp. Keep this side-effect stub aligned with that return contract.
        lambda **kwargs: datetime(2026, 7, 14, tzinfo=timezone.utc),
    )
    try:
        import app.entity_extractor as entity_extractor
    except ModuleNotFoundError:
        import backend.app.entity_extractor as entity_extractor
    monkeypatch.setattr(entity_extractor, "extract_entities", lambda **kwargs: None)


def test_oracle_run_outputs_oracle_signal_source_before_detector_persistence(monkeypatch):
    from discovery import runner
    import connectors.db.oracle_ingestor as oracle_ingestor

    _patch_runner_side_effects(monkeypatch, runner)
    monkeypatch.setattr(
        oracle_ingestor,
        "ingest",
        lambda **kwargs: _db_signal_payload("oracle_db"),
    )

    payload = runner.run(
        mode="offline",
        run_id="run-oracle-source",
        systems=["oracle_db"],
        pack="sqlserver_opsignal",
    )

    assert payload["opportunities"], "Oracle DB signal data should produce opportunities"
    assert {opp["signal_source"] for opp in payload["opportunities"]} == {"oracle_db"}


def test_postgresql_run_outputs_postgresql_signal_source_before_detector_persistence(monkeypatch):
    from discovery import runner
    import connectors.db.postgresql_ingestor as postgresql_ingestor

    _patch_runner_side_effects(monkeypatch, runner)
    monkeypatch.setattr(
        postgresql_ingestor,
        "ingest",
        lambda **kwargs: _db_signal_payload("postgresql"),
    )

    payload = runner.run(
        mode="offline",
        run_id="run-postgresql-source",
        systems=["postgresql"],
        pack="sqlserver_opsignal",
    )

    assert payload["opportunities"], "PostgreSQL signal data should produce opportunities"
    assert {opp["signal_source"] for opp in payload["opportunities"]} == {"postgresql"}


def test_sqlserver_opsignal_rejects_simultaneous_oracle_and_postgresql(monkeypatch, caplog):
    from discovery import runner
    import connectors.db.oracle_ingestor as oracle_ingestor
    import connectors.db.postgresql_ingestor as postgresql_ingestor

    _patch_runner_side_effects(monkeypatch, runner)
    monkeypatch.setattr(oracle_ingestor, "ingest", lambda **kwargs: (_ for _ in ()).throw(AssertionError("oracle called")))
    monkeypatch.setattr(postgresql_ingestor, "ingest", lambda **kwargs: (_ for _ in ()).throw(AssertionError("postgresql called")))

    payload = runner.run(
        mode="offline",
        run_id="run-two-db-connectors",
        systems=["oracle_db", "postgresql"],
        pack="sqlserver_opsignal",
    )

    assert payload["opportunities"] == []
    assert "supports one DB connector per run" in caplog.text


def test_build_db_config_warns_on_missing_live_env_and_uses_documented_keys(monkeypatch, caplog):
    from discovery.runner import _build_db_config

    monkeypatch.setenv("REQUIRE_CONNECTOR_SECRETS", "1")
    monkeypatch.delenv("ORACLE_HOST", raising=False)
    monkeypatch.delenv("POSTGRESQL_HOST", raising=False)

    oracle_config = _build_db_config("oracle_db", "org-1", "live")
    postgresql_config = _build_db_config("postgresql", "org-1", "live")

    assert oracle_config.host == "oracle.local"
    assert oracle_config.username_key == "ORACLE_DB_USERNAME"
    assert oracle_config.password_key == "ORACLE_DB_PASSWORD"
    assert postgresql_config.host == "postgres.local"
    assert postgresql_config.username_key == "POSTGRESQL_USERNAME"
    assert postgresql_config.password_key == "POSTGRESQL_PASSWORD"
    assert "ORACLE_HOST is not configured" in caplog.text
    assert "POSTGRESQL_HOST is not configured" in caplog.text
