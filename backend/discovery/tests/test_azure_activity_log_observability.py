"""
Azure Activity Log connector — OBSERVABILITY (logging-only).

A successful poll used to be almost silent: the per-subscription funnel counts were
computed, written into ``subscription_status``, and never logged, so "the endpoint
succeeded but no events appeared" could not be attributed to a stage. These tests
pin the log surface that closes that gap, and pin that it is logging ONLY — the
records, checkpoints, and statuses are asserted alongside every log assertion.

Covers:
  * one INFO funnel summary per successful poll, emitted even when every count is 0
  * an INFO "returned 0 records" line for an empty provider page
  * DEBUG request/response transport traces (url / api-version / $filter / status /
    record count) with NO bearer token in any emitted log record
  * each of the six "where did my events go" stages being individually readable
"""
from __future__ import annotations

import logging

import pytest

from discovery.ingest import azure_admin_events as admin
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg

SUB_A = "aaaaaaaa-0000-0000-0000-000000000001"
SUB_B = "bbbbbbbb-0000-0000-0000-000000000002"

AE_LOGGER = "discovery.ingest.azure_events"
ADMIN_LOGGER = "discovery.ingest.azure_admin_events"


# ── fakes / builders ─────────────────────────────────────────────────────────


class FakeStreamClient:
    def __init__(self, by_sub=None):
        self.by_sub = dict(by_sub or {})

    def fetch(self, *, token, subscription_id, environment, since_iso):
        return list(self.by_sub.get(subscription_id, []))


def _activity(sub, eid, ts, *, operation="Microsoft.Compute/virtualMachines/write",
              category="Administrative", status="Succeeded", level="Informational"):
    rec = {
        "eventDataId": eid,
        "correlationId": f"corr-{eid}",
        "eventTimestamp": ts,
        "subscriptionId": sub,
        "level": level,
        "operationName": {"value": operation},
        "status": {"value": status},
        "resourceId": (
            f"/subscriptions/{sub}/resourceGroups/rg/providers/"
            "Microsoft.Compute/virtualMachines/vm"
        ),
    }
    if category is not None:
        rec["category"] = {"value": category}
    return rec


def _ingestor(subs, *, activity=None):
    return ae.AzureEventIngestor(
        "org1",
        cfg.AzureEventConfig(
            environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
            mode=cfg.MODE_LIGHTHOUSE,
            subscriptions=list(subs),
        ),
        activity_log_client=activity,
    )


def _summaries(caplog):
    """The funnel-summary lines, parsed into {field: value} dicts."""
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("azure_events: stream="):
            body = msg.split("azure_events: ", 1)[1]
            out.append(dict(part.split("=", 1) for part in body.split(" ")))
    return out


def _one_summary(caplog):
    summaries = _summaries(caplog)
    assert len(summaries) == 1, f"expected exactly one summary, got {summaries}"
    return summaries[0]


# ── 1. the per-subscription funnel summary ───────────────────────────────────


class TestFunnelSummary:

    def test_summary_emitted_on_a_successful_poll(self, caplog):
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "e1", "2026-06-01T12:00:00Z"),
            _activity(SUB_A, "e2", "2026-06-01T13:00:00Z"),
        ]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")

        # Behaviour is unchanged: both events still ingested.
        assert result.emitted_count == 2
        assert result.subscription_status[SUB_A]["status"] == "ok"

        s = _one_summary(caplog)
        assert s["stream"] == "activity_log"
        assert s["subscription"] == SUB_A
        assert s["polled"] == "2"
        assert s["in_scope"] == "2"
        assert s["new"] == "2"
        assert s["mapped"] == "2"
        assert s["mapper_skipped"] == "0"
        assert s["since"] == "(first_run)"
        assert s["checkpoint"].startswith("2026-06-01T13:00:00")

    def test_summary_emitted_when_every_count_is_zero(self, caplog):
        # The whole point: an all-zero poll must still say so.
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        result = _ingestor([SUB_A], activity=FakeStreamClient({})).ingest_activity_log(token="T")

        assert result.emitted_count == 0
        s = _one_summary(caplog)
        assert s["polled"] == "0"
        assert s["in_scope"] == "0"
        assert s["new"] == "0"
        assert s["mapped"] == "0"
        assert s["checkpoint"] == "(unchanged)"

    def test_summary_is_per_subscription(self, caplog):
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        client = FakeStreamClient({SUB_A: [_activity(SUB_A, "e1", "2026-06-01T12:00:00Z")]})
        _ingestor([SUB_A, SUB_B], activity=client).ingest_activity_log(token="T")

        summaries = _summaries(caplog)
        assert [s["subscription"] for s in summaries] == [SUB_A, SUB_B]
        assert summaries[0]["mapped"] == "1"
        assert summaries[1]["polled"] == "0"

    def test_summary_is_info_not_warning(self, caplog):
        caplog.set_level(logging.DEBUG, logger=AE_LOGGER)
        _ingestor([SUB_A], activity=FakeStreamClient({})).ingest_activity_log(token="T")
        levels = {
            r.levelno for r in caplog.records if r.getMessage().startswith("azure_events: stream=")
        }
        assert levels == {logging.INFO}


# ── 2. the six "where did my events go" stages, each readable ────────────────


class TestStageAttribution:

    def test_zero_returned_is_distinguishable(self, caplog):
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        _ingestor([SUB_A], activity=FakeStreamClient({})).ingest_activity_log(token="T")
        s = _one_summary(caplog)
        assert (s["polled"], s["in_scope"], s["new"], s["mapped"]) == ("0", "0", "0", "0")

    def test_out_of_scope_is_distinguishable(self, caplog):
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "admin1", "2026-06-01T12:00:00Z", category="Administrative"),
            _activity(SUB_A, "sec1", "2026-06-01T12:10:00Z", category="Security"),
            _activity(SUB_A, "pol1", "2026-06-01T12:20:00Z", category="Policy"),
        ]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")

        assert result.emitted_count == 1            # scope defence unchanged (T3-AC4)
        s = _one_summary(caplog)
        assert s["polled"] == "3" and s["in_scope"] == "1" and s["mapped"] == "1"

    def test_older_than_checkpoint_is_distinguishable(self, caplog):
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "e1", "2026-06-01T12:00:00Z"),
            _activity(SUB_A, "e2", "2026-06-01T13:00:00Z"),
        ]})
        ing = _ingestor([SUB_A], activity=client)
        first = ing.ingest_activity_log(token="T")

        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        second = ing.ingest_activity_log(token="T", checkpoint=first.next_checkpoint)

        assert second.emitted_count == 0            # incremental semantics unchanged
        s = _one_summary(caplog)
        # Records WERE returned and WERE in scope — they were simply not new.
        assert s["polled"] == "2" and s["in_scope"] == "2" and s["new"] == "0"
        assert s["since"].startswith("2026-06-01T13:00:00")

    def test_mapper_skips_are_distinguishable(self, caplog, monkeypatch):
        real = ae.map_azure_activity_log

        def flaky(payload, *, org_id):
            if payload.get("eventDataId") == "bad":
                raise ValueError("unmappable")
            return real(payload, org_id=org_id)

        monkeypatch.setattr(ae, "map_azure_activity_log", flaky)
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "good", "2026-06-01T12:00:00Z"),
            _activity(SUB_A, "bad", "2026-06-01T13:00:00Z"),
        ]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")

        # Per-record isolation unchanged: the good record still ingests.
        assert result.emitted_count == 1
        s = _one_summary(caplog)
        assert s["new"] == "2" and s["mapped"] == "1" and s["mapper_skipped"] == "1"

    def test_admission_dedup_is_distinguishable(self, caplog):
        # Two deliveries of the SAME provider event id — folded by MSP-B7 admission.
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "e1", "2026-06-01T12:00:00Z"),
            _activity(SUB_A, "e1", "2026-06-01T12:00:00Z"),
        ]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")

        assert result.emitted_count == 1            # dedup behaviour unchanged
        s = _one_summary(caplog)
        assert s["polled"] == "2" and s["new"] == "2"
        assert s["mapped"] == "1" and s["deduped"] == "1"


# ── 3. the empty-response line ───────────────────────────────────────────────


class TestEmptyResponseLog:

    def test_empty_page_logged_by_name(self, caplog):
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        _ingestor([SUB_A], activity=FakeStreamClient({})).ingest_activity_log(token="T")
        assert (
            f"azure_events: Azure Activity Log returned 0 records for subscription {SUB_A}"
            in caplog.text
        )

    def test_empty_page_is_informational_not_a_warning(self, caplog):
        caplog.set_level(logging.DEBUG, logger=AE_LOGGER)
        _ingestor([SUB_A], activity=FakeStreamClient({})).ingest_activity_log(token="T")
        matching = [r for r in caplog.records if "returned 0 records" in r.getMessage()]
        assert matching and all(r.levelno == logging.INFO for r in matching)

    def test_not_logged_when_records_were_returned(self, caplog):
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        client = FakeStreamClient({SUB_A: [_activity(SUB_A, "e1", "2026-06-01T12:00:00Z")]})
        _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")
        assert "returned 0 records" not in caplog.text

    def test_logged_for_a_page_that_was_fully_filtered_out(self, caplog):
        # Distinguishes "Azure sent nothing" from "Azure sent only out-of-scope rows":
        # the empty-page line must be ABSENT here, while the summary shows the drop.
        caplog.set_level(logging.INFO, logger=AE_LOGGER)
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "sec1", "2026-06-01T12:00:00Z", category="Security"),
        ]})
        _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")
        assert "returned 0 records" not in caplog.text
        s = _one_summary(caplog)
        assert s["polled"] == "1" and s["in_scope"] == "0"


# ── 4. DEBUG transport traces (and no secret ever logged) ────────────────────


class _Recorder:
    def __init__(self, records=None, status=200):
        self.records = list(records or [])
        self.status = status
        self.urls = []

    def handler(self, request):
        import httpx
        self.urls.append(request.url)
        return httpx.Response(self.status, json={"value": self.records})


_DEFAULT = object()   # so an explicit params_builder=None is honoured, not defaulted


def _live_fetch(rec, *, path=None, api_version=None, params_builder=_DEFAULT,
                stream="activity_log", since_iso=None, token="super-secret-bearer-token"):
    import httpx
    client = admin._HttpStreamClient(
        path=path or admin._ACTIVITY_LOG_PATH,
        api_version=api_version or admin.ACTIVITY_LOG_API_VERSION,
        params_builder=admin.activity_log_params if params_builder is _DEFAULT else params_builder,
        transport=httpx.MockTransport(rec.handler),
        stream=stream,
    )
    return client.fetch(
        token=token,
        subscription_id=SUB_A,
        environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
        since_iso=since_iso,
    )


class TestDebugTransportTrace:

    def test_request_trace_carries_url_api_version_and_filter(self, caplog):
        caplog.set_level(logging.DEBUG, logger=ADMIN_LOGGER)
        _live_fetch(_Recorder())
        [req] = [r for r in caplog.records if "request stream=" in r.getMessage()]
        msg = req.getMessage()
        assert req.levelno == logging.DEBUG
        assert "stream=activity_log" in msg
        assert f"subscription={SUB_A}" in msg
        assert "/providers/Microsoft.Insights/eventtypes/management/values" in msg
        assert f"api-version={admin.ACTIVITY_LOG_API_VERSION}" in msg
        assert "eventTimestamp ge" in msg          # the generated $filter

    def test_response_trace_carries_status_and_record_count(self, caplog):
        caplog.set_level(logging.DEBUG, logger=ADMIN_LOGGER)
        returned = _live_fetch(_Recorder([
            _activity(SUB_A, "e1", "2026-06-01T12:00:00Z"),
            _activity(SUB_A, "e2", "2026-06-01T13:00:00Z"),
        ]))
        assert len(returned) == 2                  # transport behaviour unchanged
        [resp] = [r for r in caplog.records if "response stream=" in r.getMessage()]
        assert resp.levelno == logging.DEBUG
        assert "status=200" in resp.getMessage()
        assert "records=2" in resp.getMessage()

    def test_empty_live_page_traces_zero_records(self, caplog):
        caplog.set_level(logging.DEBUG, logger=ADMIN_LOGGER)
        assert _live_fetch(_Recorder([])) == []
        [resp] = [r for r in caplog.records if "response stream=" in r.getMessage()]
        assert "records=0" in resp.getMessage()

    def test_traces_are_silent_at_info(self, caplog):
        caplog.set_level(logging.INFO, logger=ADMIN_LOGGER)
        _live_fetch(_Recorder())
        assert "request stream=" not in caplog.text
        assert "response stream=" not in caplog.text

    def test_bearer_token_never_appears_in_any_log_record(self, caplog):
        caplog.set_level(logging.DEBUG)          # root: capture everything
        secret = "super-secret-bearer-token"
        _live_fetch(_Recorder([_activity(SUB_A, "e1", "2026-06-01T12:00:00Z")]), token=secret)
        assert secret not in caplog.text
        assert "Bearer" not in caplog.text
        assert "Authorization" not in caplog.text

    def test_service_health_stream_is_labelled(self, caplog):
        caplog.set_level(logging.DEBUG, logger=ADMIN_LOGGER)
        _live_fetch(
            _Recorder(),
            path=admin._SERVICE_HEALTH_PATH,
            api_version=admin.SERVICE_HEALTH_API_VERSION,
            params_builder=None,
            stream="service_health",
        )
        [req] = [r for r in caplog.records if "request stream=" in r.getMessage()]
        assert "stream=service_health" in req.getMessage()
        assert "filter=(none)" in req.getMessage()     # unchanged: no $filter here

    def test_default_live_clients_carry_their_stream_label(self, monkeypatch):
        monkeypatch.setattr(admin, "is_live", lambda: True)
        assert admin.default_activity_log_client()._stream == "activity_log"
        assert admin.default_service_health_client()._stream == "service_health"
        # The request configuration itself is untouched.
        assert admin.default_activity_log_client()._params_builder is admin.activity_log_params
        assert admin.default_service_health_client()._params_builder is None


# ── 5. logging is logging: identical outcomes regardless of log level ────────


class TestNoBehaviourChange:

    @pytest.mark.parametrize("level", [logging.CRITICAL, logging.INFO, logging.DEBUG])
    def test_outcome_identical_at_every_log_level(self, level, caplog):
        caplog.set_level(level)
        client = FakeStreamClient({SUB_A: [
            _activity(SUB_A, "e1", "2026-06-01T12:00:00Z"),
            _activity(SUB_A, "sec1", "2026-06-01T12:10:00Z", category="Security"),
            _activity(SUB_A, "e2", "2026-06-01T13:00:00Z"),
        ]})
        result = _ingestor([SUB_A], activity=client).ingest_activity_log(token="T")

        assert result.emitted_count == 2
        assert [r["provider_event_id"] for r in result.records] == ["e1", "e2"]
        assert result.next_checkpoint == ae.encode_checkpoints(
            {SUB_A: "2026-06-01T13:00:00Z"}
        )
        assert result.subscription_status[SUB_A] == {
            "status": "ok", "polled": 3, "emitted": 2, "attempts": 1, "in_scope": 2,
        }
