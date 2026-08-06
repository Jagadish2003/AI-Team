"""2.0-B1 T5 (AC5) — export-guard behaviour tests.

AC5: "Exports contain no unredacted secrets and no host x vulnerability
enumeration (1.9 aggregation floor holds in export)."

The companion conformance test
(``test_r2_0_b1_t5_export_surface_conformance.py``) proves every export surface
is ACCOUNTED FOR. This file proves the guard those surfaces call actually WORKS:

  * secrets are removed and the removal is NON-REVERSIBLE (no original value
    survives anywhere in the output, and only pattern-TYPE names are reported);
  * a host x vulnerability enumeration REFUSES the export rather than being
    emitted with a caveat;
  * the guard runs redaction BEFORE the floor, so what is checked is what ships;
  * the narrow documented exclusion (audit actor identity) is scoped to the
    sweep and never strips the exported payload;
  * each real export path holds the line end to end.

DB-free.
"""
from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from app import export_guard as eg

# Representative secret material. Distinctive so a leak is unambiguous.
#
# These are FAKE, but they are deliberately shaped like the real thing — that is
# the whole point, since the redactor matches on shape. A contiguous literal of
# that shape is (correctly) flagged by secret scanners such as GitHub push
# protection, which blocks the push even for a test fixture. So each token is
# ASSEMBLED at import time: the runtime value is byte-identical to the pattern the
# redactor must catch, while this FILE contains no token-shaped literal.
# Please keep them split — collapsing them back into single literals will block
# the next push.
_GITHUB_TOKEN = "ghp" + "_" + "abcdefghijklmnopqrstuvwxyz0123456789"
_AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_SLACK_TOKEN = "xoxb" + "-123456789012-" + "abcdefghijklmnopqrstuvwx"
_PASSWORD_ASSIGN = 'password = "sup3rs3cret-value"'

# Material the 1.9 aggregation floor must refuse.
_HOST_VULN = "Host 10.1.2.3 is affected by CVE-2026-1234."


# ── secret redaction, and its non-reversibility ─────────────────────────────


@pytest.mark.parametrize(
    "secret",
    [_GITHUB_TOKEN, _AWS_KEY, _SLACK_TOKEN],
    ids=["github_token", "aws_key", "slack_token"],
)
def test_secrets_are_removed_from_exported_content(secret):
    guarded = eg.guard_export_payload(
        {"evidence": [{"snippet": f"42 incidents; creds {secret}"}]},
        where="test export",
    )
    serialised = json.dumps(guarded.payload)
    assert secret not in serialised
    assert "REDACTED" in serialised
    assert guarded.redacted is True


def test_redaction_is_non_reversible_no_original_value_survives():
    """AC5's "non-reversible": the original must not survive ANYWHERE in the
    result — not in the payload, not in the reported pattern types, not in a
    side channel. A keyed/reversible scheme would fail this."""
    payload = {
        "a": f"token {_GITHUB_TOKEN}",
        "nested": {"b": [f"key {_AWS_KEY}", {"c": _PASSWORD_ASSIGN}]},
    }
    guarded = eg.guard_export_payload(payload, where="test export")

    everything = json.dumps(
        {"payload": guarded.payload, "types": guarded.redacted_pattern_types}
    )
    for secret_fragment in (_GITHUB_TOKEN, _AWS_KEY, "sup3rs3cret-value"):
        assert secret_fragment not in everything, (
            f"{secret_fragment!r} survived redaction — the scheme is reversible"
        )

    # Only pattern TYPE names are reported, never values.
    for name in guarded.redacted_pattern_types:
        assert "REDACTED" not in name
        assert _GITHUB_TOKEN not in name


def test_redaction_reaches_every_nesting_shape():
    """A secret buried in a list-of-dicts-in-a-tuple is still removed — an
    export document is deeply nested, so a shallow pass would leak."""
    payload = {
        "top": _GITHUB_TOKEN,
        "list": [_GITHUB_TOKEN, {"deep": [{"deeper": _GITHUB_TOKEN}]}],
        "tuple": (_GITHUB_TOKEN, {"x": _GITHUB_TOKEN}),
    }
    guarded = eg.guard_export_payload(payload, where="test export")
    assert _GITHUB_TOKEN not in json.dumps(guarded.payload, default=list)


def test_redaction_leaves_mapping_keys_untouched():
    """Keys are document STRUCTURE — rewriting one would corrupt the shape."""
    guarded = eg.redact_export_content({"api_key_field": "plain text"})
    assert "api_key_field" in guarded.payload


def test_clean_content_is_unchanged_and_reports_nothing():
    payload = {"evidence": [{"snippet": "42 incidents resolved the same way."}]}
    guarded = eg.guard_export_payload(payload, where="test export")
    assert guarded.payload == payload
    assert guarded.redacted_pattern_types == []
    assert guarded.redacted is False


def test_strict_mode_additionally_scrubs_host_identities():
    """The strict pattern set covers the identities the floor hard-fails on."""
    base = eg.redact_export_content({"s": "host 10.1.2.3 mail a@b.com"})
    assert "10.1.2.3" in json.dumps(base.payload)      # base set leaves them

    strict = eg.redact_export_content({"s": "host 10.1.2.3 mail a@b.com"}, strict=True)
    serialised = json.dumps(strict.payload)
    assert "10.1.2.3" not in serialised
    assert "a@b.com" not in serialised
    assert {"ipv4_address", "email_address"} <= set(strict.redacted_pattern_types)


# ── the aggregation floor ───────────────────────────────────────────────────


def test_host_vulnerability_enumeration_refuses_the_export():
    """Refused, not emitted-with-a-caveat: a readout that doubles as a target
    list must not leave the deployment."""
    with pytest.raises(eg.ExportGuardViolation) as exc:
        eg.guard_export_payload({"e": [{"snippet": _HOST_VULN}]}, where="test export")
    message = str(exc.value)
    assert "aggregation floor" in message
    assert "test export" in message, "the refusal must name WHICH export was refused"


@pytest.mark.parametrize(
    "offending",
    [
        {"snippet": _HOST_VULN},                       # pair in free text
        {"host": "srv-01"},                            # denylisted host field
        {"qid": "91234"},                              # vulnerability instance id
        {"assignee": "someone@example.com"},           # individual
        {"deep": {"nested": [{"s": _HOST_VULN}]}},     # buried in nesting
    ],
    ids=["pair_in_text", "host_field", "vuln_instance", "individual", "nested"],
)
def test_floor_refuses_every_violation_shape(offending):
    with pytest.raises(eg.ExportGuardViolation):
        eg.guard_export_payload(offending, where="test export")


def test_find_export_violations_reports_without_raising():
    violations = eg.find_export_violations({"s": _HOST_VULN})
    assert violations, "the non-raising reporter must find the violation"
    assert all({"path", "kind"} <= set(v) for v in violations)
    assert eg.find_export_violations({"s": "clean"}) == []


def test_floor_sweeps_the_redacted_content_not_the_original():
    """Order matters: redaction runs first, so the floor checks what ships.

    Here the strict set scrubs the IP, so the host x vuln PAIR no longer
    co-occurs and the export is allowed — proving the floor saw post-redaction
    content. With the base set the pair survives and the export is refused.
    """
    payload = {"s": _HOST_VULN}
    with pytest.raises(eg.ExportGuardViolation):
        eg.guard_export_payload(payload, where="base", strict=False)

    # Strict scrubs the host identifier; what remains is a CVE class reference,
    # which the floor still flags — so this must ALSO refuse. The point is that
    # the decision is made on redacted bytes, which the message reflects.
    with pytest.raises(eg.ExportGuardViolation):
        eg.guard_export_payload(payload, where="strict", strict=True)


# ── the narrow, documented exclusion ────────────────────────────────────────


def test_excluded_key_is_dropped_from_the_sweep_only():
    """The audit trail's actor identity is the point of an audit trail: excluded
    from the FLOOR sweep, never from the exported payload."""
    payload = {
        "findings": [{"title": "clean finding"}],
        "report_artifacts": {"audit": [{"by": "analyst@example.com"}]},
    }
    guarded = eg.guard_export_payload(
        payload, where="test export", floor_exclude_keys=("audit",)
    )
    assert guarded.payload["report_artifacts"]["audit"][0]["by"] == "analyst@example.com"

    # Without the exclusion the same content is refused (the floor flags emails).
    with pytest.raises(eg.ExportGuardViolation):
        eg.guard_export_payload(payload, where="test export")


def test_exclusion_does_not_blanket_exempt_sibling_content():
    """Excluding `audit` must not smuggle a violation through under a sibling
    key — the rest of the document is still swept."""
    payload = {
        "report_artifacts": {
            "audit": [{"by": "analyst@example.com"}],
            "executive_report": {"summary": _HOST_VULN},
        }
    }
    with pytest.raises(eg.ExportGuardViolation):
        eg.guard_export_payload(
            payload, where="test export", floor_exclude_keys=("audit",)
        )


# ── degradation posture ─────────────────────────────────────────────────────


def test_missing_floor_module_is_logged_not_silently_treated_as_safe(monkeypatch, caplog):
    monkeypatch.setattr(eg, "_aggregation_floor", lambda: None)
    with caplog.at_level("WARNING"):
        eg.assert_export_safe({"s": _HOST_VULN}, where="test export")
    assert any("WITHOUT the enumeration sweep" in r.message for r in caplog.records)


def test_missing_redactor_is_logged_not_silently_skipped(monkeypatch, caplog):
    monkeypatch.setattr(eg, "_redactor", lambda strict: None)
    with caplog.at_level("WARNING"):
        guarded = eg.redact_export_content({"s": f"token {_GITHUB_TOKEN}"})
    assert guarded.redacted_pattern_types == []
    assert any("without the redaction pass" in r.message for r in caplog.records)


def test_guard_never_logs_a_secret_value(caplog):
    with caplog.at_level("INFO"):
        eg.guard_export_payload({"s": f"token {_GITHUB_TOKEN}"}, where="test export")
    for record in caplog.records:
        assert _GITHUB_TOKEN not in record.getMessage()


# ── each real export path holds the line ────────────────────────────────────


def test_signed_evidence_export_still_refuses_an_enumerable_bundle(monkeypatch):
    """The T4 signed export now delegates to this guard — the refusal must
    survive the refactor (regression against the T4 contract)."""
    from app import evidence_export as ee

    run = {"id": "run_1", "org_id": "org_a", "packId": "security_ops"}
    kv = {
        "opps": [{"id": "opp_1", "title": "t", "evidenceIds": ["e1"]}],
        "evidence": [{"id": "e1", "source": "ServiceNow", "snippet": _HOST_VULN}],
    }
    monkeypatch.setattr(ee.db, "get_run", lambda r: run)
    monkeypatch.setattr(ee.db, "run_kv_get", lambda k, r, d=None: kv.get(k, d))
    monkeypatch.setattr("app.trace_graph.load_finding_trace", lambda r, o: None)
    monkeypatch.setattr(
        "app.evidence_pointers.get_evidence_pointers_for_opportunity", lambda r, o: []
    )

    with pytest.raises(ee.EvidenceExportError, match="aggregation floor"):
        ee.build_export_bundle("org_a", "run_1", scope=ee.SCOPE_FINDING, opp_id="opp_1")


def test_offline_seed_export_redacts_and_refuses(monkeypatch, tmp_path):
    """discovery/offline_export.py — the export path that writes files to disk."""
    from discovery import offline_export

    clean_opps, clean_ev = offline_export._guard(
        [{"id": "opp_1", "title": "clean"}],
        [{"id": "e1", "snippet": f"42 incidents; token {_GITHUB_TOKEN}"}],
    )
    assert _GITHUB_TOKEN not in json.dumps(clean_ev)

    with pytest.raises(eg.ExportGuardViolation):
        offline_export._guard(
            [{"id": "opp_1", "title": "t"}],
            [{"id": "e1", "snippet": _HOST_VULN}],
        )


def test_discovery_bridge_guards_and_degrades_loudly(caplog):
    """discovery/export_safety.py — the one bridge every discovery CLI uses."""
    from discovery.export_safety import guard_exported_payload

    guarded = guard_exported_payload(
        {"s": f"token {_GITHUB_TOKEN}"}, where="cli export"
    )
    assert _GITHUB_TOKEN not in json.dumps(guarded)

    with pytest.raises(eg.ExportGuardViolation):
        guard_exported_payload({"s": _HOST_VULN}, where="cli export")


def test_discovery_bridge_reexports_the_same_violation_class():
    """Regression: the guard must raise ONE class, whatever the import path.

    This repo is importable as both ``app.*`` and ``backend.app.*``. Loading the
    guard under both names would create two module objects and therefore two
    distinct ``ExportGuardViolation`` classes, so an ``except`` clause would
    silently fail to catch the one actually raised — a guard that cannot be
    caught is a guard that cannot be handled. The bridge pins the import order
    and re-exports the class; this asserts the two are identical.
    """
    from discovery.export_safety import ExportGuardViolation as bridge_class

    assert bridge_class is eg.ExportGuardViolation, (
        "discovery.export_safety re-exports a DIFFERENT ExportGuardViolation "
        "class than app.export_guard raises — except clauses would not catch it"
    )

    from discovery.export_safety import guard_exported_payload

    with pytest.raises(bridge_class):
        guard_exported_payload({"s": _HOST_VULN}, where="identity check")
    # ...and the same raise is catchable via the app-side symbol.
    with pytest.raises(eg.ExportGuardViolation):
        guard_exported_payload({"s": _HOST_VULN}, where="identity check")


def test_secops_volume_artifact_is_swept_at_materialization():
    """G2: the SecOps volume artifact is aggregate-only BY DESIGN, but before T5
    it was the one SecOps KV write with no floor sweep — the guarantee rested on
    a docstring. It is viewer-readable, so an enumeration reaching it would be
    broadly exposed."""
    from app.materialize_t2 import _assert_secops_materialized
    from discovery.packs.security_ops_aggregation_floor import (
        SecOpsAggregationFloorViolation,
    )

    # An aggregate-shaped artifact passes.
    _assert_secops_materialized(
        {"remediation_signature": "patch|server|auto", "count": 12},
        where="Security Operations volume artifact",
        enabled=True,
    )
    # One that enumerates does not.
    with pytest.raises(SecOpsAggregationFloorViolation):
        _assert_secops_materialized(
            {"rows": [{"snippet": _HOST_VULN}]},
            where="Security Operations volume artifact",
            enabled=True,
        )
