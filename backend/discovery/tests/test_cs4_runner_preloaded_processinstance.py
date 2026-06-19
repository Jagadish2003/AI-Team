"""
CS-4 / AT-310: runner.py forwards Salesforce CRM approval data to the nCino
ingestor so the duplicate ProcessInstance query is eliminated.
"""
import os

os.environ["INGEST_MODE"] = "offline"


def test_runner_passes_approval_processes_to_ncino(monkeypatch):
    """runner.run() calls ncino.ingest(preloaded_process_instances=sf_data['approval_processes'])."""
    from discovery import runner
    from discovery.ingest import ncino, salesforce

    captured = {}

    real_ncino_ingest = ncino.ingest

    def spy_ingest(preloaded_process_instances=None):
        captured["called"] = True
        captured["preloaded"] = preloaded_process_instances
        return real_ncino_ingest(
            preloaded_process_instances=preloaded_process_instances
        )

    monkeypatch.setattr(ncino, "ingest", spy_ingest)

    result = runner.run("offline", pack="ncino", systems=["salesforce"])

    assert isinstance(result, dict)
    assert captured.get("called") is True

    # The preloaded payload must be exactly the Salesforce CRM approval data.
    expected = salesforce.ingest().get("approval_processes", [])
    assert captured["preloaded"] == expected


def test_runner_forwards_none_when_no_approval_processes(monkeypatch):
    """When sf_data has no approval_processes, runner forwards None — not [].

    CS-4 / AT-310-fix (review issue #2): forwarding an empty list when the
    Salesforce CRM pass produced no approval data would suppress the nCino
    ingestor's own independent ProcessInstance fetch, silently dropping nCino
    approval signals. Passing None instead preserves that fallback (ncino.ingest
    only reuses preloaded data when it is genuinely provided).
    """
    from discovery import runner
    from discovery.ingest import ncino, salesforce

    captured = {}

    # Salesforce ingest returns a payload WITHOUT approval_processes.
    monkeypatch.setattr(salesforce, "ingest", lambda: {"cases": [], "flows": []})

    def spy_ingest(preloaded_process_instances=None):
        captured["preloaded"] = preloaded_process_instances
        return {"process_instances": preloaded_process_instances or []}

    monkeypatch.setattr(ncino, "ingest", spy_ingest)

    runner.run("offline", pack="ncino", systems=["salesforce"])

    # None (not []) => ncino.ingest() keeps its independent ProcessInstance fetch.
    assert captured["preloaded"] is None
