"""MSP-B1 / AT-646 (T6) — failure-loudness + outbound-only contract suite.

Proves failures are loud, never silent, and the connector is outbound-only:
  * **AC8 (Partial failure is loud)** — a revoked role on one account surfaces
    that account as ``auth_failed`` in run health while other accounts continue
    (their data still ingested); one account's failure never removes the others'
    data and never hides its own absence.
  * **AC6 (Outbound only)** — the connector makes outbound polling calls only: no
    inbound listener / subscription / push API appears in its modules, and it
    ingests normally under ``NETWORK_PROFILE=no_public_inbound`` (no inbound
    dependency).

Plus throttle back-off that RETRIES (never thins data) and REPORTS the back-off,
and a throttle budget that, once exhausted, reports the scope failed rather than
returning partial data as if complete.

Pure-Python: seeded fakes (no boto3, no network); a no-op sleeper so back-off
adds no wall-clock.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from discovery.ingest.aws_auth import (
    AWSAccountConfig,
    AWSAuthenticator,
    AWSClientFactory,
    AWSCredentials,
)
from discovery.ingest.aws_event_connector import SURFACE_CLOUDTRAIL, AWSEventConnector
from discovery.ingest.aws_health import is_throttle_error
from discovery.ingest.aws_poll_source import AWSLivePollSource

_ORG = "acme"
_REGION = "us-east-1"
_BACKEND = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────────────────────
# Fakes
# ─────────────────────────────────────────────────────────────────────────────

class _ThrottleError(Exception):
    """Mimics a botocore ClientError throttling response."""

    def __init__(self):
        super().__init__("Rate exceeded")
        self.response = {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}}


class _FakeSTS:
    def __init__(self, fail_accounts, calls):
        self.fail_accounts = set(fail_accounts)
        self.calls = calls

    def assume_role(self, *, RoleArn, RoleSessionName, ExternalId=None):
        account = RoleArn.split(":")[4]
        self.calls.append(account)
        if account in self.fail_accounts:
            raise RuntimeError("AccessDenied: role has been revoked")
        return {"Credentials": {"AccessKeyId": f"ASIA{account}",
                                "SecretAccessKey": "s", "SessionToken": "t"}}


class _FakeCloudTrail:
    def __init__(self, records, throttle_state, account_id):
        self.records = records
        self.throttle_state = throttle_state  # {"remaining": N} shared across rebuilds
        self.account_id = account_id

    def lookup_events(self, **kwargs):
        if self.throttle_state.get("remaining", 0) > 0:
            self.throttle_state["remaining"] -= 1
            raise _ThrottleError()
        return {"Events": [{"CloudTrailEvent": json.dumps(r)} for r in self.records],
                "NextToken": None}


class _Factory(AWSClientFactory):
    def __init__(self, accounts_data, *, fail_accounts=(), throttle_by_account=None):
        self.accounts_data = accounts_data
        self.sts = _FakeSTS(fail_accounts, [])
        self.throttle_by_account = throttle_by_account or {}

    def client(self, service, *, region, credentials):
        if service == "sts":
            return self.sts
        account_id = credentials.access_key_id[4:]  # 'ASIA<acct>' / 'AKIA<acct>'
        if service == "cloudtrail":
            return _FakeCloudTrail(
                self.accounts_data.get(account_id, []),
                self.throttle_by_account.setdefault(account_id, {"remaining": 0}),
                account_id,
            )
        raise AssertionError(f"unexpected service {service!r}")


def _mgmt_event(event_id, ts):
    return {
        "eventID": event_id, "eventTime": ts, "eventName": "AssumeRole",
        "eventSource": "sts.amazonaws.com", "eventCategory": "Management",
        "managementEvent": True, "userIdentity": {"arn": "arn:aws:iam::x:user/a"},
    }


def _role_account(account_id):
    return AWSAccountConfig(
        account_id=account_id,
        role_arn=f"arn:aws:iam::{account_id}:role/AgentIQReadOnlyEvents",
        regions=(_REGION,),
    )


def _direct_account(account_id):
    return AWSAccountConfig(account_id=account_id, regions=(_REGION,))


def _run(source):
    conn = AWSEventConnector(source)
    records = [r for b in conn.ingest_changes(_ORG, None) for r in b.records]
    return records, conn.health_report()


def _noop_sleeper(_seconds):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# AC8 — partial failure is loud
# ─────────────────────────────────────────────────────────────────────────────

def test_ac8_revoked_role_on_one_account_is_loud_others_continue():
    good, bad = "111111111111", "222222222222"
    factory = _Factory(
        {good: [_mgmt_event("ct-good", "2026-07-14T03:00:00Z")], bad: []},
        fail_accounts={bad},  # role revoked on the bad account
    )
    auth = AWSAuthenticator(
        client_factory=factory,
        hub_resolver=lambda o: AWSCredentials("AKIAHUB", "s", source="hub"),
        account_key_resolver=lambda o, a: None,  # role only → no fallback
    )
    source = AWSLivePollSource([_role_account(good), _role_account(bad)],
                               auth, surfaces=(SURFACE_CLOUDTRAIL,), sleeper=_noop_sleeper)
    records, health = _run(source)

    # The healthy account's data is still ingested (others continue)...
    assert [r["provider_event_id"] for r in records] == ["ct-good"]
    # ...and the revoked account is LOUD in run health, not silently absent.
    by_account = {a["account_id"]: a for a in health["accounts"]}
    assert by_account[bad]["status"] == "auth_failed"
    assert "credentials" in by_account[bad]["message"].lower()  # a clear reason, not blank
    assert by_account[good]["status"] == "ok"
    assert health["all_healthy"] is False
    assert health["failed_accounts"] == [bad]


# ─────────────────────────────────────────────────────────────────────────────
# Throttle: back off + report, never thin the data quietly
# ─────────────────────────────────────────────────────────────────────────────

def test_throttle_backs_off_retries_and_recovers_without_thinning():
    acct = "111111111111"
    factory = _Factory(
        {acct: [_mgmt_event("ct-1", "2026-07-14T03:00:00Z")]},
        throttle_by_account={acct: {"remaining": 2}},  # throttle twice, then succeed
    )
    auth = AWSAuthenticator(
        client_factory=factory, hub_resolver=lambda o: None,
        account_key_resolver=lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"),
    )
    source = AWSLivePollSource([_direct_account(acct)], auth,
                               surfaces=(SURFACE_CLOUDTRAIL,), sleeper=_noop_sleeper)
    records, health = _run(source)

    # Data fully ingested (NOT thinned) after the back-off recovered.
    assert [r["provider_event_id"] for r in records] == ["ct-1"]
    account = health["accounts"][0]
    assert account["status"] == "ok"
    assert account["throttle_events"] == 2   # the back-off is reported, not hidden


def test_throttle_budget_exhausted_reports_scope_failed_not_silent():
    acct = "111111111111"
    factory = _Factory(
        {acct: [_mgmt_event("ct-1", "2026-07-14T03:00:00Z")]},
        throttle_by_account={acct: {"remaining": 99}},  # never stops throttling
    )
    auth = AWSAuthenticator(
        client_factory=factory, hub_resolver=lambda o: None,
        account_key_resolver=lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"),
    )
    source = AWSLivePollSource([_direct_account(acct)], auth, surfaces=(SURFACE_CLOUDTRAIL,),
                               max_throttle_retries=3, sleeper=_noop_sleeper)
    records, health = _run(source)

    # No data returned — but the failure is LOUD (reported), not a silent partial.
    assert records == []
    account = health["accounts"][0]
    assert account["status"] == "failed"
    assert account["throttle_events"] == 3           # exhausted the retry budget
    assert SURFACE_CLOUDTRAIL in account["surfaces_failed"]
    assert health["all_healthy"] is False


def test_is_throttle_error_recognises_client_error_and_named_exceptions():
    assert is_throttle_error(_ThrottleError())

    class ThrottlingException(Exception):
        pass

    assert is_throttle_error(ThrottlingException())
    assert not is_throttle_error(ValueError("nope"))


# ─────────────────────────────────────────────────────────────────────────────
# AC6 — outbound only
# ─────────────────────────────────────────────────────────────────────────────

def test_ac6_no_inbound_or_push_infrastructure_in_connector_modules():
    modules = [
        "discovery/ingest/aws_event_connector.py",
        "discovery/ingest/aws_auth.py",
        "discovery/ingest/aws_poll_source.py",
        "discovery/ingest/aws_partitions.py",
        "discovery/ingest/aws_health.py",
        "discovery/ingest/cloud_event_connector.py",
    ]
    # Push / inbound tokens that would betray a non-outbound design.
    forbidden = [
        "subscribe", "webhook", "socketserver", "http.server", "httpserver",
        "websocket", "put_targets", "add_api_route", "create_event_bus",
        "sns_", ".listen(", "ngrok", "inbound listener",
    ]
    offenders = []
    for rel in modules:
        text = (_BACKEND / rel).read_text(encoding="utf-8").lower()
        for token in forbidden:
            if token in text:
                offenders.append(f"{rel}: {token!r}")
    assert not offenders, "outbound-only violated (AC6): " + ", ".join(offenders)


def test_ac6_ingests_under_no_public_inbound_profile(monkeypatch):
    monkeypatch.setenv("NETWORK_PROFILE", "no_public_inbound")
    from app.network_profile import is_no_public_inbound
    assert is_no_public_inbound()  # sanity: the posture is active

    acct = "111111111111"
    factory = _Factory({acct: [_mgmt_event("ct-1", "2026-07-14T03:00:00Z")]})
    auth = AWSAuthenticator(
        client_factory=factory, hub_resolver=lambda o: None,
        account_key_resolver=lambda o, a: AWSCredentials(f"AKIA{a}", "s", source="direct_keys"),
    )
    source = AWSLivePollSource([_direct_account(acct)], auth,
                               surfaces=(SURFACE_CLOUDTRAIL,), sleeper=_noop_sleeper)
    records, health = _run(source)
    # Outbound-only polling works with no inbound surface available.
    assert [r["provider_event_id"] for r in records] == ["ct-1"]
    assert health["all_healthy"] is True


def test_offline_connector_health_report_is_empty_dict():
    # The offline (StaticCloudPollSource) connector has no live health surface.
    from discovery.ingest.aws_event_connector import build_offline_aws_source
    conn = AWSEventConnector(build_offline_aws_source())
    assert conn.health_report() == {}
