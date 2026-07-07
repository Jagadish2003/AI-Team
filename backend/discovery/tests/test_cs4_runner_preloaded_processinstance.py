"""
nCino ProcessInstance sourcing (reverts CS-4 / AT-310 reuse).

Original CS-4/AT-310 forwarded sf_data["approval_processes"] into
ncino.ingest(preloaded_process_instances=...) to avoid a "duplicate"
ProcessInstance query. That reuse was invalid: approval_processes is an
AGGREGATED per-process-name summary (process_name / pending_count /
approver_count), not raw ProcessInstance rows, so nCino's loan-approval
detector had no TargetObjectId to match and APPROVAL_BOTTLENECK silently
never fired even with live pending approvals. The runner now lets nCino run
its own ProcessInstance fetch.
"""
import os

os.environ["INGEST_MODE"] = "offline"


def test_runner_does_not_forward_aggregated_approvals_to_ncino(monkeypatch):
    """runner.run() must NOT hand nCino the aggregated approval_processes summary.

    nCino must run its own ProcessInstance fetch, so the preloaded argument is
    None (the summary shape cannot satisfy the loan-approval detector).
    """
    from discovery import runner
    from discovery.ingest import ncino

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
    # nCino sources ProcessInstance itself — the runner forwards nothing.
    assert captured["preloaded"] is None


def test_ncino_builds_approval_metrics_from_its_own_process_instances(monkeypatch):
    """End-to-end: offline nCino ingest populates approval_metrics from its own
    ProcessInstance rows (raw, with TargetObjectId), independent of the
    Salesforce CRM approval rollup."""
    from discovery import runner
    from discovery.ingest import ncino

    captured = {}
    real_ncino_ingest = ncino.ingest

    def spy_ingest(preloaded_process_instances=None):
        data = real_ncino_ingest(
            preloaded_process_instances=preloaded_process_instances
        )
        captured["approval_metrics"] = data.get("approval_metrics")
        return data

    monkeypatch.setattr(ncino, "ingest", spy_ingest)

    runner.run("offline", pack="ncino", systems=["salesforce"])

    # approval_metrics is built from nCino's own ProcessInstance rows.
    assert captured["approval_metrics"] is not None
    assert "pending_count" in captured["approval_metrics"]
