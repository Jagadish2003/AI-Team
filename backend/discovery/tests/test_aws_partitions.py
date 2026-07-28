"""MSP-B1 / AT-645 (T5) — partition-aware endpoint configuration contract suite.

Config-level (no boto3, no network): proves the AWS connector resolves the right
endpoints for the commercial (``aws``) and GovCloud (``aws-us-gov``) partitions,
that a region contradicting its partition is rejected at config time, and that the
partition is selectable per connection (on ``AWSAccountConfig``).

  * **AC7 (Partition config)** — the endpoint map resolves GovCloud endpoints
    correctly; MSP-B9's live verification is explicitly referenced as the
    follow-through (:data:`B9_LIVE_VERIFICATION_NOTE`).
"""
from __future__ import annotations

import pytest

from discovery.ingest.aws_auth import AWSAccountConfig, parse_account_config
from discovery.ingest.aws_partitions import (
    ALL_PARTITIONS,
    B9_LIVE_VERIFICATION_NOTE,
    PARTITION_AWS,
    PARTITION_GOVCLOUD,
    PartitionError,
    arn_partition_for_region,
    endpoint_map,
    resolve_endpoint,
    resolve_partition_for_region,
    resolve_service_endpoint_or_none,
    validate_region,
)
from discovery.ingest.aws_poll_source import _alarm_history_to_state_change


# ─────────────────────────────────────────────────────────────────────────────
# AC7 — the endpoint map (the load-bearing GovCloud assertion)
# ─────────────────────────────────────────────────────────────────────────────

def test_ac7_govcloud_endpoint_map_resolves_correctly():
    m = endpoint_map(PARTITION_GOVCLOUD, "us-gov-west-1")
    assert m == {
        "cloudwatch": "https://monitoring.us-gov-west-1.amazonaws.com",
        "events": "https://events.us-gov-west-1.amazonaws.com",
        "cloudtrail": "https://cloudtrail.us-gov-west-1.amazonaws.com",
        "sts": "https://sts.us-gov-west-1.amazonaws.com",
    }


def test_commercial_endpoint_map_resolves_correctly():
    m = endpoint_map(PARTITION_AWS, "us-east-1")
    assert m == {
        "cloudwatch": "https://monitoring.us-east-1.amazonaws.com",
        "events": "https://events.us-east-1.amazonaws.com",
        "cloudtrail": "https://cloudtrail.us-east-1.amazonaws.com",
        "sts": "https://sts.us-east-1.amazonaws.com",
    }


def test_govcloud_east_region_also_resolves():
    m = endpoint_map(PARTITION_GOVCLOUD, "us-gov-east-1")
    assert m["cloudwatch"] == "https://monitoring.us-gov-east-1.amazonaws.com"


# ─────────────────────────────────────────────────────────────────────────────
# Partition resolution + ARN partition
# ─────────────────────────────────────────────────────────────────────────────

def test_resolve_partition_for_region():
    assert resolve_partition_for_region("us-gov-west-1") == PARTITION_GOVCLOUD
    assert resolve_partition_for_region("us-gov-east-1") == PARTITION_GOVCLOUD
    assert resolve_partition_for_region("us-east-1") == PARTITION_AWS
    assert resolve_partition_for_region("eu-west-1") == PARTITION_AWS
    assert resolve_partition_for_region(None) == PARTITION_AWS


def test_arn_partition_for_region():
    assert arn_partition_for_region("us-gov-west-1") == "aws-us-gov"
    assert arn_partition_for_region("us-east-1") == "aws"


def test_all_partitions_are_the_v1_two():
    assert set(ALL_PARTITIONS) == {PARTITION_AWS, PARTITION_GOVCLOUD}


# ─────────────────────────────────────────────────────────────────────────────
# Region ↔ partition validation (misconfiguration fails at config time)
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_region_accepts_matching():
    validate_region(PARTITION_GOVCLOUD, "us-gov-west-1")
    validate_region(PARTITION_AWS, "us-east-1")
    validate_region(PARTITION_AWS, None)          # empty region is allowed
    validate_region(PARTITION_GOVCLOUD, None)


def test_validate_region_rejects_commercial_region_under_govcloud():
    with pytest.raises(PartitionError):
        validate_region(PARTITION_GOVCLOUD, "us-east-1")


def test_validate_region_rejects_govcloud_region_under_commercial():
    with pytest.raises(PartitionError):
        validate_region(PARTITION_AWS, "us-gov-west-1")


def test_unknown_partition_rejected():
    with pytest.raises(PartitionError):
        validate_region("aws-cn", "cn-north-1")
    with pytest.raises(PartitionError):
        resolve_endpoint("nonsense", "cloudwatch", "us-east-1")


# ─────────────────────────────────────────────────────────────────────────────
# STS global vs regional; best-effort factory resolution
# ─────────────────────────────────────────────────────────────────────────────

def test_commercial_sts_is_global_without_a_region():
    assert resolve_endpoint(PARTITION_AWS, "sts", None) == "https://sts.amazonaws.com"


def test_govcloud_has_no_global_sts():
    with pytest.raises(PartitionError):
        resolve_endpoint(PARTITION_GOVCLOUD, "sts", None)


def test_factory_endpoint_resolution_is_partition_aware():
    # A GovCloud region resolves the aws-us-gov endpoint...
    assert (
        resolve_service_endpoint_or_none("cloudwatch", "us-gov-west-1")
        == "https://monitoring.us-gov-west-1.amazonaws.com"
    )
    # ...a commercial region the commercial one...
    assert (
        resolve_service_endpoint_or_none("cloudtrail", "us-east-1")
        == "https://cloudtrail.us-east-1.amazonaws.com"
    )
    # ...STS with no region is commercial-global...
    assert resolve_service_endpoint_or_none("sts", None) == "https://sts.amazonaws.com"
    # ...a non-STS service with no region defers to boto3 (None)...
    assert resolve_service_endpoint_or_none("cloudwatch", None) is None
    # ...and a service the connector does not configure defers to boto3.
    assert resolve_service_endpoint_or_none("s3", "us-east-1") is None


# ─────────────────────────────────────────────────────────────────────────────
# Selectable per connection — AWSAccountConfig integration
# ─────────────────────────────────────────────────────────────────────────────

def test_account_defaults_to_commercial_partition():
    account = AWSAccountConfig(account_id="1", regions=("us-east-1",))
    assert account.partition == PARTITION_AWS


def test_account_partition_derived_from_govcloud_region():
    account = AWSAccountConfig(account_id="1", regions=("us-gov-west-1",))
    assert account.partition == PARTITION_GOVCLOUD


def test_account_partition_explicitly_selectable():
    account = AWSAccountConfig(
        account_id="1", regions=("us-gov-east-1",), partition=PARTITION_GOVCLOUD
    )
    assert account.partition == PARTITION_GOVCLOUD


def test_account_rejects_region_partition_mismatch():
    with pytest.raises(PartitionError):
        AWSAccountConfig(account_id="1", regions=("us-gov-west-1",), partition=PARTITION_AWS)
    with pytest.raises(PartitionError):
        AWSAccountConfig(account_id="1", regions=("us-east-1",), partition=PARTITION_GOVCLOUD)


def test_parse_account_config_reads_partition():
    account = parse_account_config({
        "account_id": "111111111111",
        "role_arn": "arn:aws-us-gov:iam::111111111111:role/AgentIQReadOnlyEvents",
        "regions": ["us-gov-west-1"],
        "partition": PARTITION_GOVCLOUD,
    })
    assert account.partition == PARTITION_GOVCLOUD
    assert account.regions == ("us-gov-west-1",)


# ─────────────────────────────────────────────────────────────────────────────
# Partition-aware resource ARNs + the B9 follow-through reference
# ─────────────────────────────────────────────────────────────────────────────

def test_cloudwatch_alarm_arn_uses_govcloud_partition():
    item = {
        "AlarmName": "HighCPU", "Timestamp": "2026-07-14T01:00:00Z",
        "HistoryItemType": "StateUpdate",
    }
    event = _alarm_history_to_state_change(
        item, account_id="111111111111", region="us-gov-west-1",
        timestamp_iso="2026-07-14T01:00:00Z",
    )
    assert event["resources"][0].startswith("arn:aws-us-gov:cloudwatch:us-gov-west-1:")
    # Commercial stays arn:aws:…
    event_c = _alarm_history_to_state_change(
        item, account_id="111111111111", region="us-east-1",
        timestamp_iso="2026-07-14T01:00:00Z",
    )
    assert event_c["resources"][0].startswith("arn:aws:cloudwatch:us-east-1:")


def test_b9_live_verification_is_referenced_as_follow_through():
    assert "B9" in B9_LIVE_VERIFICATION_NOTE
    assert "GovCloud" in B9_LIVE_VERIFICATION_NOTE or "aws-us-gov" in B9_LIVE_VERIFICATION_NOTE
