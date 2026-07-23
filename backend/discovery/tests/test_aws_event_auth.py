"""MSP-B1 / AT-642 (T2) — AWS auth + cross-account access contract suite.

Proves:
  * **AC1 (Connect + ingest)** — connecting with vault-held (hub) credentials and
    per-account STS role assumption ingests alarm-history, bounded EventBridge, and
    CloudTrail management events from TWO seeded accounts, each event carrying its
    ``account_scope``.
  * **AC9 (IAM policy — design review)** — the read-only IAM policy artifact exists,
    is minimal (exactly the calls the connector makes, no wildcard actions, no
    write actions), and carries the independent-reviewer sign-off section.

Plus the auth paths: STS AssumeRole from the hub, the direct-per-account-keys
fallback (both no-role and assume-failure), loud degradation when nothing resolves,
secret masking, and the no-secret-in-config rule.

Pure-Python: STS and every service client are seeded fakes, so no test needs boto3
or an AWS account.
"""
from __future__ import annotations

import json
import os

import pytest

from discovery.ingest.aws_auth import (
    AUTH_MODE_DIRECT_KEYS,
    AWSAccountConfig,
    AWSAuthenticator,
    AWSAuthError,
    AWSClientFactory,
    AWSCredentials,
    account_key_connector_id,
    load_aws_accounts,
    parse_account_config,
)
from discovery.ingest.aws_event_connector import AWSEventConnector
from discovery.ingest.aws_poll_source import AWSLivePollSource, build_live_aws_source
from discovery.signals.evidence_store import InMemoryRawEventStore

_ORG = "acme"
_HERE = os.path.dirname(__file__)
_POLICY_DIR = os.path.join(_HERE, "..", "..", "..", "deployment")
_POLICY_JSON = os.path.join(_POLICY_DIR, "aws_readonly_iam_policy.json")
_POLICY_DOC = os.path.join(_POLICY_DIR, "AWS_READONLY_IAM_POLICY.md")


# ─────────────────────────────────────────────────────────────────────────────
# Fake AWS clients (seeded; no boto3, no network)
# ─────────────────────────────────────────────────────────────────────────────

def _account_from_key(access_key_id: str) -> str:
    """Fake creds encode their account in the key tail (ASIA<acct> / AKIA<acct>)."""
    return access_key_id[4:]


class _FakeSTS:
    def __init__(self, fail_accounts=()):
        self.fail_accounts = set(fail_accounts)
        self.calls = []  # (RoleArn, ExternalId)

    def assume_role(self, *, RoleArn, RoleSessionName, ExternalId=None):
        account = RoleArn.split(":")[4]
        self.calls.append((RoleArn, ExternalId))
        if account in self.fail_accounts:
            raise RuntimeError("AccessDenied: not authorized to assume role")
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA{account}",
                "SecretAccessKey": "assumed-secret",
                "SessionToken": f"session-{account}",
            }
        }


class _FakeCloudWatch:
    def __init__(self, data):
        self.data = data

    def describe_alarm_history(self, **kwargs):
        return {"AlarmHistoryItems": self.data.get("alarm_items", []), "NextToken": None}


class _FakeCloudTrail:
    def __init__(self, data):
        self.data = data

    def lookup_events(self, **kwargs):
        events = [{"CloudTrailEvent": json.dumps(rec)} for rec in self.data.get("ct_events", [])]
        return {"Events": events, "NextToken": None}


class _FakeEventBridge:
    def __init__(self, data):
        self.data = data

    def list_rules(self, **kwargs):
        return {"Rules": self.data.get("rules", []), "NextToken": None}

    def describe_rule(self, *, Name):
        for rule in self.data.get("rules", []):
            if rule.get("Name") == Name:
                return rule
        return {"Name": Name}


class FakeAWSClientFactory(AWSClientFactory):
    def __init__(self, accounts_data, *, fail_accounts=()):
        self.accounts_data = accounts_data
        self.sts = _FakeSTS(fail_accounts=fail_accounts)

    def client(self, service, *, region, credentials):
        if service == "sts":
            return self.sts
        account = _account_from_key(credentials.access_key_id)
        data = self.accounts_data.get(account, {})
        if service == "cloudwatch":
            return _FakeCloudWatch(data)
        if service == "events":
            return _FakeEventBridge(data)
        if service == "cloudtrail":
            return _FakeCloudTrail(data)
        raise AssertionError(f"unexpected service {service!r}")


def _account_data(account_id: str) -> dict:
    """One alarm-history item, one EventBridge rule, one CloudTrail event per account."""
    return {
        "alarm_items": [{
            "AlarmName": "HighCPU",
            "Timestamp": "2026-07-14T01:00:00Z",
            "HistoryItemType": "StateUpdate",
            "HistorySummary": "OK -> ALARM",
            "HistoryData": json.dumps({"newState": {"stateValue": "ALARM"}, "oldState": {"stateValue": "OK"}}),
        }],
        "rules": [{
            "Name": f"rule-{account_id}",
            "Arn": f"arn:aws:events:us-east-1:{account_id}:rule/rule-{account_id}",
            "State": "ENABLED",
        }],
        "ct_events": [{
            "eventID": f"ct-{account_id}",
            "eventTime": "2026-07-14T03:00:00Z",
            "eventSource": "sts.amazonaws.com",
            "eventName": "AssumeRole",
            "userIdentity": {"type": "IAMUser", "arn": f"arn:aws:iam::{account_id}:user/alice"},
            "resources": [{"ARN": f"arn:aws:iam::{account_id}:role/admin"}],
        }],
    }


def _hub_resolver(org_id):
    return AWSCredentials("AKIAHUBACCESSKEY", "hub-secret", source="hub")


def _role_account(account_id: str) -> AWSAccountConfig:
    return AWSAccountConfig(
        account_id=account_id,
        role_arn=f"arn:aws:iam::{account_id}:role/AgentIQReadOnlyEvents",
        external_id="agentiq-ext-id",
        regions=("us-east-1",),
    )


# ─────────────────────────────────────────────────────────────────────────────
# AC1 — connect with hub creds + role assumption; ingest from two accounts
# ─────────────────────────────────────────────────────────────────────────────

def test_ac1_two_accounts_role_assumption_ingests_all_surfaces_with_account_scope():
    accounts = [_role_account("111111111111"), _role_account("222222222222")]
    factory = FakeAWSClientFactory({a.account_id: _account_data(a.account_id) for a in accounts})
    auth = AWSAuthenticator(
        client_factory=factory, hub_resolver=_hub_resolver,
        account_key_resolver=lambda o, a: None,  # role assumption only
    )
    source = AWSLivePollSource(accounts, auth)
    store = InMemoryRawEventStore()
    connector = AWSEventConnector(source, raw_store=store)

    records = [r for b in connector.ingest_changes(_ORG, None) for r in b.records]

    # Every record carries its account_scope, and BOTH accounts are represented.
    scopes = {r["account_scope"] for r in records}
    assert scopes == {"111111111111", "222222222222"}

    # Each account contributes all three surfaces (alarm history, EventBridge rule,
    # CloudTrail event), correctly normalised through the MSP-B0 mappers.
    for account_id in ("111111111111", "222222222222"):
        acct_recs = [r for r in records if r["account_scope"] == account_id]
        by_surface = {r["surface"]: r for r in acct_recs}
        assert set(by_surface) == {"cloudwatch", "eventbridge", "cloudtrail"}
        # CloudWatch alarm-history was reconciled to a normalised state_change event.
        cw = by_surface["cloudwatch"]["event"]
        assert cw["event_class"] == "state_change"
        assert cw["resource_type"] == "monitoring"
        assert cw["severity"] == "high"
        assert cw["source_system"] == "aws"        # provider family, per AC4/T1
        # CloudTrail management event mapped as access; its record is scoped too.
        assert by_surface["cloudtrail"]["event"]["event_class"] == "access"

    # Role assumption actually happened, once per account, with the right role ARN.
    assumed = {arn for arn, _ext in factory.sts.calls}
    assert assumed == {
        "arn:aws:iam::111111111111:role/AgentIQReadOnlyEvents",
        "arn:aws:iam::222222222222:role/AgentIQReadOnlyEvents",
    }
    # ExternalId was passed through on assumption (confused-deputy guard).
    assert all(ext == "agentiq-ext-id" for _arn, ext in factory.sts.calls)

    # Evidence resolves back to the raw provider payload (account-partitioned store).
    signals = connector.active_signals(_ORG)
    assert signals
    raws = signals[0].resolve_raw_instances(store)
    assert raws  # the aggregate opens back to its real raw instance(s)


def test_ac1_incremental_run_advances_per_scope_watermark():
    accounts = [_role_account("111111111111")]
    factory = FakeAWSClientFactory({"111111111111": _account_data("111111111111")})
    auth = AWSAuthenticator(client_factory=factory, hub_resolver=_hub_resolver,
                            account_key_resolver=lambda o, a: None)
    source = AWSLivePollSource(accounts, auth)
    connector = AWSEventConnector(source)
    batches = list(connector.ingest_changes(_ORG, None))
    assert batches[-1].is_complete
    # The terminal checkpoint carries a per-scope watermark (the newest event time),
    # so a later run resumes by time rather than re-reading everything.
    assert "2026-07-14T03:00:00Z" in batches[-1].next_checkpoint  # cloudtrail watermark


# ─────────────────────────────────────────────────────────────────────────────
# Auth paths — assume-role, direct-keys fallback, loud failure
# ─────────────────────────────────────────────────────────────────────────────

def test_direct_keys_account_without_a_role():
    account = AWSAccountConfig(account_id="333333333333", regions=("us-east-1",))
    assert account.auth_mode == AUTH_MODE_DIRECT_KEYS  # no role → direct keys
    factory = FakeAWSClientFactory({"333333333333": _account_data("333333333333")})

    def account_keys(org_id, account_id):
        return AWSCredentials(f"AKIA{account_id}", "direct-secret", source="direct_keys")

    auth = AWSAuthenticator(client_factory=factory, hub_resolver=lambda o: None,
                            account_key_resolver=account_keys)
    creds = auth.credentials_for(_ORG, account)
    assert creds.source == "direct_keys"
    assert factory.sts.calls == []  # never attempted AssumeRole

    source = AWSLivePollSource([account], auth)
    connector = AWSEventConnector(source)
    records = [r for b in connector.ingest_changes(_ORG, None) for r in b.records]
    assert {r["account_scope"] for r in records} == {"333333333333"}


def test_assume_role_failure_falls_back_to_direct_keys():
    account = _role_account("444444444444")
    factory = FakeAWSClientFactory({"444444444444": _account_data("444444444444")},
                                   fail_accounts={"444444444444"})

    def account_keys(org_id, account_id):
        return AWSCredentials(f"AKIA{account_id}", "direct-secret", source="direct_keys")

    auth = AWSAuthenticator(client_factory=factory, hub_resolver=_hub_resolver,
                            account_key_resolver=account_keys)
    creds = auth.credentials_for(_ORG, account)
    assert creds.source == "direct_keys"         # assume failed → fell back
    assert factory.sts.calls                      # it did TRY to assume first


def test_no_credentials_raises_and_scope_degrades_without_crashing():
    account = _role_account("555555555555")
    factory = FakeAWSClientFactory({"555555555555": _account_data("555555555555")})
    auth = AWSAuthenticator(client_factory=factory, hub_resolver=lambda o: None,
                            account_key_resolver=lambda o, a: None)
    with pytest.raises(AWSAuthError):
        auth.credentials_for(_ORG, account)

    # Through the poll source the failure degrades the scope to empty, not a crash.
    source = AWSLivePollSource([account], auth)
    connector = AWSEventConnector(source)
    records = [r for b in connector.ingest_changes(_ORG, None) for r in b.records]
    assert records == []


def test_assumed_credentials_are_cached_per_account():
    account = _role_account("111111111111")
    factory = FakeAWSClientFactory({"111111111111": _account_data("111111111111")})
    auth = AWSAuthenticator(client_factory=factory, hub_resolver=_hub_resolver,
                            account_key_resolver=lambda o, a: None)
    source = AWSLivePollSource([account], auth)
    connector = AWSEventConnector(source)
    list(connector.ingest_changes(_ORG, None))
    # 3 surfaces polled for one account, but AssumeRole happened only ONCE (cached).
    assert len(factory.sts.calls) == 1


def test_credentials_repr_masks_the_secret():
    creds = AWSCredentials(
        "AKIAEXAMPLE12345", "super-secret-value", session_token="sess-abc-xyz", source="hub"
    )
    text = repr(creds)
    assert "super-secret-value" not in text   # secret value never in repr
    assert "sess-abc-xyz" not in text         # session token value never in repr
    assert "***" in text


# ─────────────────────────────────────────────────────────────────────────────
# Config — no secret in config (secrets are vaulted)
# ─────────────────────────────────────────────────────────────────────────────

def test_inline_secret_in_account_config_is_rejected():
    with pytest.raises(ValueError):
        parse_account_config({"account_id": "1", "secret_access_key": "AKIA/should/not/be/here"})
    with pytest.raises(ValueError):
        parse_account_config({"account_id": "1", "aws_access_key_id": "AKIAEXAMPLE"})


def test_load_aws_accounts_from_env_json():
    accounts = load_aws_accounts(env={"AWS_EVENT_ACCOUNTS": json.dumps([
        {"account_id": "111111111111", "role_arn": "arn:aws:iam::111111111111:role/R", "regions": ["us-east-1"]},
    ])})
    assert len(accounts) == 1
    assert accounts[0].account_id == "111111111111"
    assert accounts[0].regions == ("us-east-1",)


def test_account_key_reserved_vault_id():
    assert account_key_connector_id("123") == "aws_events:account:123"


def test_build_live_aws_source_defaults_are_injectable():
    accounts = [_role_account("111111111111")]
    factory = FakeAWSClientFactory({"111111111111": _account_data("111111111111")})
    auth = AWSAuthenticator(client_factory=factory, hub_resolver=_hub_resolver,
                            account_key_resolver=lambda o, a: None)
    source = build_live_aws_source(accounts=accounts, authenticator=auth)
    assert isinstance(source, AWSLivePollSource)
    scopes = source.list_scopes(_ORG)
    assert len(scopes) == 3  # one account × one region × three surfaces


# ─────────────────────────────────────────────────────────────────────────────
# AC9 — the read-only IAM policy is a minimal, reviewable partner artifact
# ─────────────────────────────────────────────────────────────────────────────

#: The EXACT set of AWS API actions the connector makes — the minimality bar.
_EXPECTED_ACTIONS = {
    "cloudwatch:DescribeAlarmHistory",  # read_cloudwatch
    "events:ListRules",                 # read_eventbridge
    "events:DescribeRule",              # read_eventbridge
    "cloudtrail:LookupEvents",          # read_cloudtrail
    "sts:AssumeRole",                   # hub → per-account role assumption
}

_WRITE_VERBS = ("Create", "Put", "Update", "Delete", "Modify", "Write", "Attach", "Detach", "Remove")


def _policy_actions(doc):
    actions = set()
    for key in ("read_only_role_policy", "hub_assume_role_policy"):
        for stmt in doc[key]["Statement"]:
            acts = stmt["Action"]
            actions.update([acts] if isinstance(acts, str) else acts)
    return actions


def test_ac9_iam_policy_artifact_exists():
    assert os.path.exists(_POLICY_JSON)
    assert os.path.exists(_POLICY_DOC)


def test_ac9_iam_policy_is_exactly_the_calls_used():
    with open(_POLICY_JSON, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert _policy_actions(doc) == _EXPECTED_ACTIONS  # nothing more, nothing less


def test_ac9_iam_policy_has_no_wildcard_or_write_actions():
    with open(_POLICY_JSON, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    for action in _policy_actions(doc):
        assert "*" not in action, f"wildcard action not allowed: {action}"
        verb = action.split(":", 1)[1]
        assert not verb.startswith(_WRITE_VERBS), f"write-like action not allowed: {action}"


def test_ac9_all_statements_are_allow_read_only():
    with open(_POLICY_JSON, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    for key in ("read_only_role_policy", "hub_assume_role_policy"):
        for stmt in doc[key]["Statement"]:
            assert stmt["Effect"] == "Allow"


def test_ac9_doc_carries_independent_reviewer_signoff():
    with open(_POLICY_DOC, "r", encoding="utf-8") as fh:
        text = fh.read().lower()
    assert "someone other than" in text        # the independent-review gate
    assert "reviewer" in text
