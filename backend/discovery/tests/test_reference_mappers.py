"""MSP-B0 / AT-637 — tests for the provider reference mappers over golden fixtures.

Covers:
  * T3-AC1 — AWS operational event fixtures normalize correctly.
  * T3-AC2 — Azure operational event fixtures normalize correctly.
  * T3-AC3 — both provider mappings emit identical detector-visible structures.
  * T3-AC4 — mapping contract is documented (reference mappers are the executable
    contract; the golden fixtures pin their behaviour).
"""
import json
import os

import pytest

from discovery.signals import (
    MAPPERS,
    OperationalEvent,
    provider_family,
)
from discovery.signals.reference_mappers import (
    aws_resource_type_from_arn,
    azure_resource_type_from_id,
)

GOLDEN = os.path.join(os.path.dirname(__file__), "fixtures", "msp_provider_mapping_golden.json")


def _cases():
    with open(GOLDEN, "r", encoding="utf-8") as fh:
        return json.load(fh)["cases"]


CASES = _cases()
AWS_CASES = [c for c in CASES if c["expected"]["provider_family"] == "aws"]
AZURE_CASES = [c for c in CASES if c["expected"]["provider_family"] == "azure"]


def _run(case):
    mapper = MAPPERS[case["mapper"]]
    return mapper(case["raw"], org_id=case["org_id"])


def _assert_expected(ev: OperationalEvent, exp: dict):
    assert isinstance(ev, OperationalEvent)
    assert ev.source_system == exp["source_system"]
    assert provider_family(ev.source_system) == exp["provider_family"]
    assert ev.signal_id == exp["signal_id"]
    assert ev.event_type == exp["event_type"]
    assert ev.event_class == exp["event_class"]
    assert ev.resource_type == exp["resource_type"]
    assert ev.severity == exp["severity"]
    assert ev.org_id == "acme"
    # provenance is a valid OBSERVED spine, signature is derived + present
    assert ev.provenance["origin"] == "observed"
    assert ev.event_signature.startswith("1:")
    if "resource_id" in exp:
        assert ev.resource is not None
        assert ev.resource.resource_id == exp["resource_id"]
    if "observed_at" in exp:
        assert ev.observed_at == exp["observed_at"]
    if "message" in exp:
        assert ev.message == exp["message"]
    if "principal" in exp:
        assert ev.payload.get("principal") == exp["principal"]


# ── T3-AC1: AWS fixtures normalize correctly ────────────────────────────────

@pytest.mark.parametrize("case", AWS_CASES, ids=[c["name"] for c in AWS_CASES])
def test_aws_fixtures_normalize(case):
    _assert_expected(_run(case), case["expected"])


# ── T3-AC2: Azure fixtures normalize correctly ──────────────────────────────

@pytest.mark.parametrize("case", AZURE_CASES, ids=[c["name"] for c in AZURE_CASES])
def test_azure_fixtures_normalize(case):
    _assert_expected(_run(case), case["expected"])


# ── T3-AC3: identical detector-visible structure across providers ───────────

def test_all_providers_emit_identical_structure():
    events = [_run(c) for c in CASES]
    key_sets = {frozenset(ev.to_dict().keys()) for ev in events}
    assert len(key_sets) == 1, f"providers produced differing structures: {key_sets}"
    # spot-check the resource sub-structure is also uniform where present
    ref_key_sets = {
        frozenset(ev.to_dict()["resource"].keys())
        for ev in events if ev.resource is not None
    }
    assert len(ref_key_sets) == 1


def test_aws_and_azure_share_the_exact_shape():
    aws = _run(AWS_CASES[0]).to_dict()
    azure = _run(AZURE_CASES[0]).to_dict()
    assert aws.keys() == azure.keys()
    # values differ, but every schema field is present for both
    for key in ("org_id", "source_system", "signal_id", "observed_at", "provenance",
                "resource_type", "event_class", "severity", "event_type",
                "resource", "message", "payload", "event_signature"):
        assert key in aws and key in azure


# ── helper coverage + resilience (missing/optional fields degrade) ──────────

def test_aws_resource_type_helper():
    assert aws_resource_type_from_arn("arn:aws:s3:::my-bucket") == "storage"
    assert aws_resource_type_from_arn("arn:aws:rds:us-east-1:1:db:mydb") == "database"
    assert aws_resource_type_from_arn("arn:aws:lambda:us-east-1:1:function:f") == "serverless"
    assert aws_resource_type_from_arn("") == "other"


def test_azure_resource_type_helper():
    assert azure_resource_type_from_id(
        "/subscriptions/s/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/sa"
    ) == "storage"
    assert azure_resource_type_from_id(
        "/subscriptions/s/providers/Microsoft.Network/virtualNetworks/vn"
    ) == "network"
    assert azure_resource_type_from_id("") == "other"


def test_mapper_tolerates_sparse_payload():
    # a minimal EventBridge-ish payload with no resources / no time still maps
    ev = MAPPERS["map_eventbridge"]({"id": "x", "detail-type": "Some Event"}, org_id="acme")
    assert ev.source_system == "aws"
    assert ev.resource is None
    assert ev.event_signature  # still deterministic + present


def test_same_recurring_provider_event_shares_signature():
    """Two CloudTrail occurrences of the same action by the same principal on the
    same resource collapse to one signature (recurrence), even at different times."""
    base = {
        "eventSource": "sts.amazonaws.com", "eventName": "AssumeRole", "awsRegion": "us-east-1",
        "userIdentity": {"arn": "arn:aws:iam::1:user/alice"},
        "resources": [{"ARN": "arn:aws:iam::1:role/admin", "type": "AWS::IAM::Role"}],
    }
    a = MAPPERS["map_cloudtrail"]({**base, "eventID": "1", "eventTime": "2026-07-14T00:00:00Z"}, org_id="acme")
    b = MAPPERS["map_cloudtrail"]({**base, "eventID": "2", "eventTime": "2026-07-15T00:00:00Z"}, org_id="acme")
    assert a.event_signature == b.event_signature
