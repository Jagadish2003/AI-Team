"""resolve_effective_pack — a GitHub-connected run defaults to the
github_engineering pack unless a pack was explicitly chosen (T1-S12 live-path).
"""
from __future__ import annotations

from app.materialize_t2 import resolve_effective_pack


def test_explicit_pack_always_wins():
    # An explicit Stack Builder / run-input pack is never overridden, even when
    # GitHub is connected.
    assert resolve_effective_pack("service_cloud", ["github"]) == "service_cloud"
    assert resolve_effective_pack("ncino", ["salesforce", "github"]) == "ncino"


def test_github_connected_defaults_to_github_engineering():
    assert resolve_effective_pack(None, ["github"]) == "github_engineering"
    assert resolve_effective_pack(None, ["salesforce", "github"]) == "github_engineering"
    # Empty-string explicit pack is treated as "not set".
    assert resolve_effective_pack("", ["github"]) == "github_engineering"


def test_no_github_no_default():
    # Without GitHub the helper returns None → runner falls back to service_cloud,
    # preserving prior behaviour for SoR-only runs.
    assert resolve_effective_pack(None, ["salesforce", "servicenow", "jira"]) is None
    assert resolve_effective_pack(None, []) is None
    assert resolve_effective_pack(None, None) is None
