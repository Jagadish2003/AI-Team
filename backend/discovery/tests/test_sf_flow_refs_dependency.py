"""Salesforce named-credential flow refs via MetadataComponentDependency (Option 2).

Unit-tests the dependency-API resolver and the shared result builder offline (no
network): it maps direct Flow→NamedCredential and indirect Flow→Apex→NC edges,
matches on DeveloperName OR MasterLabel, and returns None (→ per-flow fallback)
when the Dependency API is unavailable. This replaces the per-flow Metadata N+1
that had to be time-bounded and could undercount the D5 INTEGRATION_CONCENTRATION
signal.
"""
from __future__ import annotations

from discovery.ingest.salesforce import (
    _build_flow_ref_results,
    _flow_refs_via_dependency_api,
)

NCS = [
    {"credential_name": "My NC Label", "credential_developer_name": "My_NC"},
    {"credential_name": "Other", "credential_developer_name": "Other_NC"},
]


class _FakeClient:
    """Returns scripted MetadataComponentDependency rows; no network."""

    def __init__(self, nc_rows, apex_rows=None, raise_on_nc=False):
        self._nc_rows = nc_rows
        self._apex_rows = apex_rows or []
        self._raise_on_nc = raise_on_nc
        self.queries = []

    def tooling_soql(self, query, max_records=5000):
        self.queries.append(query)
        if "RefMetadataComponentType = 'NamedCredential'" in query:
            if self._raise_on_nc:
                raise RuntimeError("sObject type 'MetadataComponentDependency' is not supported")
            return self._nc_rows
        return self._apex_rows  # the indirect Flow→Apex query


def _flow(comp_id, ref_name):
    return {
        "MetadataComponentId": comp_id,
        "MetadataComponentType": "Flow",
        "RefMetadataComponentName": ref_name,
        "RefMetadataComponentType": "NamedCredential",
    }


def test_direct_flow_refs_counted():
    client = _FakeClient([_flow("301A", "My_NC"), _flow("301B", "My_NC")])
    dep = _flow_refs_via_dependency_api(NCS, client)
    assert dep["My_NC"] == ["301A", "301B"]
    assert dep["Other_NC"] == []

    res = _build_flow_ref_results(NCS, dep, "dependency_api")
    my = next(r for r in res if r["credential_developer_name"] == "My_NC")
    assert my["flow_reference_count"] == 2
    assert my["match_type"] == "dependency_api"
    other = next(r for r in res if r["credential_developer_name"] == "Other_NC")
    assert other["flow_reference_count"] == 0
    assert other["match_type"] == "none"


def test_indirect_via_apex_counted():
    # An Apex class references the NC, and a Flow references that Apex class.
    nc_rows = [{
        "MetadataComponentId": "01pAPX",
        "MetadataComponentType": "ApexClass",
        "RefMetadataComponentName": "My_NC",
        "RefMetadataComponentType": "NamedCredential",
    }]
    apex_rows = [{
        "MetadataComponentId": "301C",
        "MetadataComponentType": "Flow",
        "RefMetadataComponentId": "01pAPX",
        "RefMetadataComponentType": "ApexClass",
    }]
    dep = _flow_refs_via_dependency_api(NCS, _FakeClient(nc_rows, apex_rows))
    assert dep["My_NC"] == ["301C"]


def test_matches_by_master_label():
    # Salesforce reports the label instead of the dev name — still mapped.
    dep = _flow_refs_via_dependency_api(NCS, _FakeClient([_flow("301D", "My NC Label")]))
    assert dep["My_NC"] == ["301D"]


def test_fallback_when_dependency_api_unavailable():
    dep = _flow_refs_via_dependency_api(NCS, _FakeClient([], raise_on_nc=True))
    assert dep is None  # → caller runs the bounded per-flow scan


# ── Zero-result corroboration (get_named_credential_flow_refs end-to-end) ────
# A zero answer from the (Beta) dependency graph is ambiguous — its coverage is
# not exhaustive — so the function must corroborate it with the bounded per-flow
# scan instead of trusting it outright. Conversely, a non-zero dependency answer
# is final: no per-flow scan queries may be issued.

from discovery.ingest import salesforce as sf_mod


class _ScanAwareClient:
    """Scripted client covering BOTH the dependency query and the per-flow scan."""

    def __init__(self, dep_rows, flows, flow_metadata):
        self._dep_rows = dep_rows
        self._flows = flows          # rows for "FROM Flow WHERE Status = 'Active'"
        self._flow_metadata = flow_metadata  # {flow_id: metadata dict}
        self.scan_queries = 0

    def tooling_soql(self, query, max_records=5000):
        if "FROM MetadataComponentDependency" in query:
            return self._dep_rows
        if "SELECT Id, MasterLabel FROM Flow" in query:
            self.scan_queries += 1
            return self._flows
        if "SELECT Metadata FROM Flow WHERE Id =" in query:
            self.scan_queries += 1
            flow_id = query.split("'")[1]
            meta = self._flow_metadata.get(flow_id)
            return [{"Metadata": meta}] if meta else []
        raise AssertionError(f"unexpected query: {query}")


def test_zero_dependency_result_is_corroborated_by_flow_scan(monkeypatch):
    monkeypatch.setattr(sf_mod, "is_live", lambda: True)
    monkeypatch.delenv("SF_DISABLE_DEPENDENCY_API", raising=False)
    monkeypatch.delenv("SF_SCAN_APEX_NC_REFS", raising=False)

    # Dependency graph says zero, but a flow's metadata string-matches My_NC —
    # the scan must catch it (false-zero protection for D5).
    client = _ScanAwareClient(
        dep_rows=[],
        flows=[{"Id": "301X", "MasterLabel": "Flow X"}],
        flow_metadata={"301X": {"actionCalls": [{"namedCredential": "My_NC"}]}},
    )
    res = sf_mod.get_named_credential_flow_refs(NCS, client)

    my = next(r for r in res if r["credential_developer_name"] == "My_NC")
    assert my["flow_reference_count"] == 1
    assert my["referencing_flow_ids"] == ["301X"]
    assert my["match_type"] == "apex_flow_trace_scan"
    assert client.scan_queries > 0  # the corroboration scan actually ran


def test_nonzero_dependency_result_skips_flow_scan(monkeypatch):
    monkeypatch.setattr(sf_mod, "is_live", lambda: True)
    monkeypatch.delenv("SF_DISABLE_DEPENDENCY_API", raising=False)

    client = _ScanAwareClient(
        dep_rows=[_flow("301A", "My_NC")],
        flows=[{"Id": "301A", "MasterLabel": "Flow A"}],
        flow_metadata={},
    )
    res = sf_mod.get_named_credential_flow_refs(NCS, client)

    my = next(r for r in res if r["credential_developer_name"] == "My_NC")
    assert my["flow_reference_count"] == 1
    assert my["match_type"] == "dependency_api"
    assert client.scan_queries == 0  # dependency answer was final — no N+1 scan
