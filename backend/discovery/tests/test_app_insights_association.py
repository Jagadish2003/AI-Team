"""
2.0-D3 T3 — IIS / .NET / CMDB association by EXPLICIT reference only.

Offline / DB-free: both target resolvers are injected, so the entire decision table
runs with no database, no configured estate and no network.

D3-AC3: "Application-to-component/CI association occurs only on explicit reference;
ambiguous cases remain unassociated."

This suite is deliberately lopsided. The positive path (an operator declared an
app_id, we resolved it) is three tests. Everything else proves a REFUSAL, because
the failure mode that matters here is not "we missed an association" — it is "we
invented one". A wrong association makes AgentIQ blame the wrong system during an
incident, and name-based matching is the tempting wrong answer: right often enough
to look like it works, wrong often enough to be dangerous.

So the bulk of what follows seeds the situations where a guess would be easy —
identical display names, matching hostnames and URLs, same resource group, same
owner, same environment, same tags, events at the same time — and asserts that
nothing is associated.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from discovery.ingest import app_insights_association as aia
from discovery.ingest import azure_events as ae
from discovery.ingest import azure_events_config as cfg

SUB = "11111111-2222-3333-4444-555555555555"
COMPONENT = (
    f"/subscriptions/{SUB}/resourceGroups/prod/providers"
    "/microsoft.insights/components/checkout-api"
)
OTHER_COMPONENT = COMPONENT.replace("checkout-api", "orders-api")

APP_ID = "checkout-api"
SYS_ID = "a1b2c3d4e5f6000011112222333344455"


# ── builders ────────────────────────────────────────────────────────────────────


def _entry(**over):
    base = {"component_id": COMPONENT}
    base.update(over)
    return base


def _config(*entries):
    """Build the component-keyed config index from raw entry dicts."""
    index = {}
    for raw in entries:
        e = aia._coerce_entry(raw)
        index.setdefault(e.component_id.strip().lower(), []).append(e)
    return index


class _Target:
    """Stand-in for DotNetAppTarget — only ``app_id`` is an association input."""

    def __init__(self, app_id, name=None):
        self.app_id = app_id
        self.name = name or app_id


class _Entity:
    """Stand-in for the resolved CMDB entity row."""

    def __init__(self, sys_id=SYS_ID, org_id="acme", name="Checkout API", eid="e-1"):
        self.id = eid
        self.org_id = org_id
        self.display_name = name
        self.canonical_name = (name or "").lower()
        self.source_record_id = sys_id
        self.resolution_status = "resolved"
        self.resolution_confidence = 1.0


def _resolver(*entries, targets=None, cmdb=None, org="acme"):
    """A resolver over an explicit config, with both target lookups injected."""
    return aia.AppInsightsAssociationResolver(
        org,
        config=_config(*entries) if entries else {},
        dotnet_resolver=aia.build_dotnet_resolver(org, targets=targets or []),
        cmdb_resolver=cmdb if cmdb is not None else (lambda s: None),
    )


def _cmdb_ok(entity=None):
    """A CMDB resolver backed by the REAL builder over a fake lookup."""
    ent = entity or _Entity()

    def _lookup(**kw):
        _lookup.calls.append(kw)
        if kw.get("source_record_id") == ent.source_record_id:
            return ent
        return None

    _lookup.calls = []
    return aia.build_cmdb_resolver("acme", lookup=_lookup), _lookup


# ── the positive path ───────────────────────────────────────────────────────────


class TestExactAssociation:

    def test_exact_dotnet_association(self):
        out = _resolver(
            _entry(dotnet_app_id=APP_ID), targets=[_Target(APP_ID)]
        ).resolve(COMPONENT)
        assert len(out.associations) == 1
        a = out.associations[0]
        assert a.target_type == aia.TARGET_DOTNET_APP
        assert a.target_id == APP_ID          # the STABLE identifier, not a name
        assert a.org_id == "acme"
        assert out.unresolved == ()

    def test_exact_cmdb_association(self):
        cmdb, _ = _cmdb_ok()
        out = _resolver(_entry(cmdb_ci_sys_id=SYS_ID), cmdb=cmdb).resolve(COMPONENT)
        assert len(out.associations) == 1
        a = out.associations[0]
        assert a.target_type == aia.TARGET_CMDB_CI
        assert a.target_id == SYS_ID          # the sys_id, not the CI name
        assert out.unresolved == ()

    def test_combined_association(self):
        cmdb, _ = _cmdb_ok()
        out = _resolver(
            _entry(dotnet_app_id=APP_ID, cmdb_ci_sys_id=SYS_ID),
            targets=[_Target(APP_ID)], cmdb=cmdb,
        ).resolve(COMPONENT)
        assert {a.target_type for a in out.associations} == {
            aia.TARGET_DOTNET_APP, aia.TARGET_CMDB_CI
        }
        assert out.unresolved == ()

    def test_a_partial_combination_associates_what_it_can(self):
        """One valid reference is not invalidated by the other being wrong."""
        out = _resolver(
            _entry(dotnet_app_id=APP_ID, cmdb_ci_sys_id="not-a-known-ci"),
            targets=[_Target(APP_ID)],
        ).resolve(COMPONENT)
        assert [a.target_type for a in out.associations] == [aia.TARGET_DOTNET_APP]
        assert [u["reason"] for u in out.unresolved] == [aia.REASON_CMDB_NOT_RESOLVED]

    def test_arm_resource_id_case_is_identity_not_name_matching(self):
        """Azure resource ids are case-insensitive, so folding case is an identity
        comparison — it is not the fuzzy matching the no-inference rule forbids."""
        out = _resolver(
            _entry(component_id=COMPONENT.replace("resourceGroups", "RESOURCEGROUPS"),
                   dotnet_app_id=APP_ID),
            targets=[_Target(APP_ID)],
        ).resolve(COMPONENT)
        assert len(out.associations) == 1


# ── evidence / traceability ─────────────────────────────────────────────────────


class TestEvidence:

    def test_every_association_names_the_configuration_that_produced_it(self):
        cmdb, _ = _cmdb_ok()
        out = _resolver(
            _entry(dotnet_app_id=APP_ID, cmdb_ci_sys_id=SYS_ID, notes="onboarding"),
            targets=[_Target(APP_ID)], cmdb=cmdb,
        ).resolve(COMPONENT)
        for a in out.associations:
            ev = a.evidence
            assert ev["source"] == "configuration"
            assert ev["config_key"] == aia.CONFIG_ENV
            assert ev["component_id"] == COMPONENT
            assert ev["declared_reference"]        # the exact declared identifier
            assert ev["resolved_against"]          # how it was resolved
            assert ev["notes"] == "onboarding"

    def test_the_declared_reference_is_the_identifier_that_was_configured(self):
        out = _resolver(
            _entry(dotnet_app_id=APP_ID), targets=[_Target(APP_ID)]
        ).resolve(COMPONENT)
        assert out.associations[0].evidence["declared_reference"] == APP_ID

    def test_the_output_carries_type_id_and_org(self):
        out = _resolver(
            _entry(dotnet_app_id=APP_ID), targets=[_Target(APP_ID)]
        ).resolve(COMPONENT)
        d = out.associations[0].to_dict()
        assert set(d) == {"target_type", "target_id", "org_id", "evidence"}
        assert d["target_type"] in aia.TARGET_TYPES


# ── the no-inference rule: every tempting guess is refused ──────────────────────


class TestNoInference:

    def test_matching_display_names_alone_associate_nothing(self):
        """The headline negative case. Identical names in both systems, and no
        explicit identifier — so there is no association."""
        out = _resolver(
            _entry(),                                   # component declared, no target
            targets=[_Target("checkout-api", name="Checkout API")],
        ).resolve(COMPONENT)
        assert out.associations == ()
        assert [u["reason"] for u in out.unresolved] == [aia.REASON_NO_TARGETS]

    def test_no_configuration_at_all_associates_nothing_and_says_nothing(self):
        """An unconfigured component is not a problem to report — it is a component
        whose owner has not declared an association."""
        out = _resolver(targets=[_Target(APP_ID)]).resolve(COMPONENT)
        assert out.is_empty
        assert out.to_wrapper() == {}

    @pytest.mark.parametrize("field", [
        "name", "url", "hostname", "iis_site", "resource_group", "owner",
        "environment", "tags", "server", "display_name",
    ])
    def test_no_soft_signal_is_ever_an_association_input(self, field):
        """A config entry stuffed with every tempting soft signal, and no explicit
        identifier, still associates nothing."""
        out = _resolver(
            _entry(**{field: "checkout-api"}),
            targets=[_Target(APP_ID)],
        ).resolve(COMPONENT)
        assert out.associations == ()

    def test_the_cmdb_lookup_is_never_given_a_display_name(self):
        """``lookup_resolved_entity`` falls back to canonical-NAME matching when an
        id finds nothing. Passing a display name would smuggle in exactly the
        name-based association this task forbids, so we never pass one."""
        cmdb, lookup = _cmdb_ok()
        _resolver(_entry(cmdb_ci_sys_id=SYS_ID), cmdb=cmdb).resolve(COMPONENT)
        assert lookup.calls
        for call in lookup.calls:
            assert "display_name" not in call or call["display_name"] is None

    def test_the_cmdb_lookup_keys_on_the_sys_id(self):
        cmdb, lookup = _cmdb_ok()
        _resolver(_entry(cmdb_ci_sys_id=SYS_ID), cmdb=cmdb).resolve(COMPONENT)
        call = lookup.calls[0]
        assert call["source_record_id"] == SYS_ID
        assert call["entity_type"] == aia.CMDB_ENTITY_TYPE == "system"
        assert call["source_system"] == aia.CMDB_SOURCE_SYSTEM == "servicenow"
        assert call["org_id"] == "acme"

    def test_the_config_entry_reads_only_explicit_identifiers(self):
        """Structural, scoped to where an association INPUT can enter.

        ``_coerce_entry`` is the only place a config entry is read, so the set of
        keys it names IS the set of possible association inputs. If someone later
        reads a hostname / url / tag / IIS site name from the entry, this fails.

        Deliberately scoped to that function rather than the whole module: the
        module does read the resolved CMDB entity's ``display_name``, but only to
        record it as evidence CONTEXT alongside an association the sys_id already
        earned — it is never a matching input. A whole-module sweep would conflate
        the two and would have to be weakened to pass, which would make it useless.
        """
        src = Path(aia.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_coerce_entry"
        )
        read_keys = {
            node.args[0].value
            for node in ast.walk(fn)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str))
        }
        assert read_keys == {
            "component_id", "dotnet_app_id", "cmdb_ci_sys_id", "org_id", "notes",
        }, sorted(read_keys)

    @pytest.mark.parametrize("wrong", [
        "Checkout-API",      # different case
        "CHECKOUT-API",
        "checkout_api",      # different separator
        "checkout",          # prefix
        "checkout-api-v2",   # suffix
    ])
    def test_a_near_miss_app_id_does_not_resolve(self, wrong):
        """``app_id`` is a chosen identifier; accepting a near-miss would be
        inference by another name. Matching is exact — no case folding, no
        separator normalisation, no prefix/suffix tolerance."""
        out = _resolver(
            _entry(dotnet_app_id=wrong), targets=[_Target(APP_ID)]
        ).resolve(COMPONENT)
        assert out.associations == ()

    @pytest.mark.parametrize(
        "padded",
        [" checkout-api", "checkout-api ", "\tcheckout-api\n", "  checkout-api  "],
    )
    def test_surrounding_whitespace_in_config_is_trimmed(self, padded):
        """Trimming whitespace around a configured value is config hygiene, not
        matching: a trailing newline from a hand-edited JSON file must not silently
        cost an operator their association. The identifier itself is still compared
        exactly (see the near-miss cases above)."""
        out = _resolver(
            _entry(dotnet_app_id=padded), targets=[_Target(APP_ID)]
        ).resolve(COMPONENT)
        assert len(out.associations) == 1
        assert out.associations[0].target_id == APP_ID


# ── missing / conflicting / duplicate / ambiguous / cross-org ───────────────────


class TestRefusals:

    def test_a_dotnet_app_id_that_is_not_configured_is_unresolved(self):
        out = _resolver(
            _entry(dotnet_app_id="never-configured"), targets=[_Target(APP_ID)]
        ).resolve(COMPONENT)
        assert out.associations == ()
        assert out.unresolved[0]["reason"] == aia.REASON_DOTNET_NOT_CONFIGURED
        assert out.unresolved[0]["declared_reference"] == "never-configured"

    def test_an_unknown_cmdb_sys_id_is_unresolved(self):
        cmdb, _ = _cmdb_ok()
        out = _resolver(_entry(cmdb_ci_sys_id="0000deadbeef"), cmdb=cmdb).resolve(COMPONENT)
        assert out.associations == ()
        assert out.unresolved[0]["reason"] == aia.REASON_CMDB_NOT_RESOLVED

    def test_an_ambiguous_cmdb_id_is_unresolved(self):
        """Several confidently-resolved rows for one sys_id: the existing lookup
        returns None for that, and we must not paper over it."""
        cmdb = aia.build_cmdb_resolver("acme", lookup=lambda **kw: None)
        out = _resolver(_entry(cmdb_ci_sys_id=SYS_ID), cmdb=cmdb).resolve(COMPONENT)
        assert out.associations == ()
        assert out.unresolved[0]["reason"] == aia.REASON_CMDB_NOT_RESOLVED

    def test_duplicate_configuration_for_one_component_is_refused(self):
        """Refused even though picking one would 'work' — a config naming one
        component twice is ambiguous about intent."""
        out = _resolver(
            _entry(dotnet_app_id=APP_ID),
            _entry(dotnet_app_id="something-else"),
            targets=[_Target(APP_ID), _Target("something-else")],
        ).resolve(COMPONENT)
        assert out.associations == ()
        assert out.unresolved[0]["reason"] == aia.REASON_DUPLICATE_CONFIG
        assert out.unresolved[0]["entry_count"] == 2

    def test_even_agreeing_duplicates_are_refused(self):
        out = _resolver(
            _entry(dotnet_app_id=APP_ID),
            _entry(dotnet_app_id=APP_ID),
            targets=[_Target(APP_ID)],
        ).resolve(COMPONENT)
        assert out.associations == ()
        assert out.unresolved[0]["reason"] == aia.REASON_DUPLICATE_CONFIG

    def test_an_entry_declaring_another_org_is_refused(self):
        out = _resolver(
            _entry(dotnet_app_id=APP_ID, org_id="another-org"),
            targets=[_Target(APP_ID)], org="acme",
        ).resolve(COMPONENT)
        assert out.associations == ()
        assert out.unresolved[0]["reason"] == aia.REASON_CROSS_ORG

    def test_a_cmdb_entity_owned_by_another_org_is_refused(self):
        """Belt-and-braces on top of the org-scoped lookup: an association that
        crossed an org boundary would be the worst defect available here."""
        cmdb, _ = _cmdb_ok(entity=_Entity(org_id="another-org"))
        out = _resolver(_entry(cmdb_ci_sys_id=SYS_ID), cmdb=cmdb).resolve(COMPONENT)
        assert out.associations == ()
        assert out.unresolved[0]["reason"] == aia.REASON_CMDB_NOT_RESOLVED

    def test_a_config_entry_for_a_different_component_does_not_leak(self):
        out = _resolver(
            _entry(component_id=OTHER_COMPONENT, dotnet_app_id=APP_ID),
            targets=[_Target(APP_ID)],
        ).resolve(COMPONENT)
        assert out.is_empty

    @pytest.mark.parametrize("component", ["", None, "   "])
    def test_a_blank_component_resolves_to_nothing(self, component):
        out = _resolver(_entry(dotnet_app_id=APP_ID), targets=[_Target(APP_ID)]).resolve(component)
        assert out.is_empty


# ── configuration loading ───────────────────────────────────────────────────────


class TestConfiguration:

    def test_an_entry_with_an_inline_credential_is_rejected(self):
        with pytest.raises(aia.AppInsightsAssociationConfigError):
            aia._coerce_entry({"component_id": COMPONENT, "password": "hunter2"})

    def test_a_credential_bearing_entry_is_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setattr(aia, "_raw_entries", lambda org: [
            {"component_id": COMPONENT, "api_key": "x"},
            {"component_id": OTHER_COMPONENT, "dotnet_app_id": APP_ID},
        ])
        loaded = aia.load_association_config("acme")
        assert list(loaded) == [OTHER_COMPONENT.lower()]

    def test_an_entry_without_a_component_id_is_rejected(self):
        with pytest.raises(aia.AppInsightsAssociationConfigError):
            aia._coerce_entry({"dotnet_app_id": APP_ID})

    def test_the_config_is_org_keyed_with_a_default_fallback(self):
        parsed = {
            "acme": [{"component_id": COMPONENT, "dotnet_app_id": "a"}],
            "default": [{"component_id": OTHER_COMPONENT, "dotnet_app_id": "b"}],
        }
        assert aia._select_org_entries(parsed, "acme")[0]["dotnet_app_id"] == "a"
        assert aia._select_org_entries(parsed, "other")[0]["dotnet_app_id"] == "b"

    def test_a_plain_array_applies_to_every_org(self):
        parsed = [{"component_id": COMPONENT, "dotnet_app_id": "a"}]
        assert aia._select_org_entries(parsed, "anyone")[0]["dotnet_app_id"] == "a"

    def test_a_scalar_metadata_key_is_ignored(self):
        parsed = {"_comment": "docs", "default": [{"component_id": COMPONENT}]}
        assert len(aia._select_org_entries(parsed, "acme")) == 1

    def test_the_shipped_offline_fixture_resolves_cleanly(self):
        """A shipped default that always reported a miss would train operators to
        ignore the field, so the offline fixture must resolve against the offline
        .NET targets."""
        from discovery.ingest.dotnet_app_config import load_targets
        resolver = aia.AppInsightsAssociationResolver(
            "acme",
            dotnet_resolver=aia.build_dotnet_resolver("acme", targets=load_targets("acme")),
            cmdb_resolver=lambda s: None,
        )
        assert resolver.has_configuration
        for component in aia.load_association_config("acme"):
            out = resolver.resolve(component)
            assert out.associations, component
            assert out.unresolved == (), component

    def test_the_fixture_declares_no_credentials(self):
        with open(aia.FIXTURE_PATH, encoding="utf-8") as fh:
            blob = fh.read().lower()
        for token in ('"password"', '"secret"', '"api_key"', '"token"'):
            assert token not in blob


# ── the connector: additive, never a gate, never in the event identity ──────────


class _AlertsFake:
    def __init__(self, rows):
        self._rows = rows

    def fetch_alerts(self, *, token, subscription_id, environment, since_iso):
        return list(self._rows)


def _alert(component=COMPONENT):
    return {"data": {"essentials": {
        "alertId": f"/subscriptions/{SUB}/providers/Microsoft.AlertsManagement/alerts/a-1",
        "alertRule": "checkout-api-availability",
        "severity": "Sev1", "signalType": "Metric", "monitorCondition": "Fired",
        "monitoringService": "Platform", "alertTargetIDs": [component],
        "firedDateTime": "2026-07-20T09:00:00Z",
        "description": "3 of 5 locations failed",
    }, "alertContext": {"conditionType": "WebtestLocationAvailabilityCriteria"}}}


def _ingestor(*, alerts, association_resolver):
    return ae.AzureEventIngestor(
        "acme",
        cfg.AzureEventConfig(
            environment=cfg.resolve_environment(cfg.AZURE_CLOUD),
            mode=cfg.MODE_LIGHTHOUSE,
            subscriptions=[SUB],
        ),
        alerts_client=_AlertsFake(alerts),
        association_resolver=association_resolver,
    )


class TestConnectorIntegration:

    def test_a_resolved_association_rides_the_record_wrapper(self):
        r = _resolver(_entry(dotnet_app_id=APP_ID), targets=[_Target(APP_ID)])
        rec = _ingestor(alerts=[_alert()], association_resolver=r).ingest_alerts(
            token="T"
        ).records[0]
        assocs = rec["app_insights"]["associations"]
        assert len(assocs) == 1
        assert assocs[0]["target_id"] == APP_ID

    def test_the_association_is_never_in_the_b0_event(self):
        """It must not touch the canonical event identity — otherwise the
        deterministic signature and transport equivalence would depend on whether a
        customer happened to configure an association."""
        r = _resolver(_entry(dotnet_app_id=APP_ID), targets=[_Target(APP_ID)])
        rec = _ingestor(alerts=[_alert()], association_resolver=r).ingest_alerts(
            token="T"
        ).records[0]
        blob = json.dumps(rec["event"])
        assert "association" not in blob
        assert APP_ID not in blob or APP_ID in rec["event"]["resource"]["resource_id"]

    def test_the_event_signature_is_identical_with_and_without_an_association(self):
        with_assoc = _ingestor(
            alerts=[_alert()],
            association_resolver=_resolver(
                _entry(dotnet_app_id=APP_ID), targets=[_Target(APP_ID)]
            ),
        ).ingest_alerts(token="T").records[0]
        without = _ingestor(
            alerts=[_alert()], association_resolver=_resolver(),
        ).ingest_alerts(token="T").records[0]
        assert with_assoc["event"] == without["event"]
        assert with_assoc["event_signature"] == without["event_signature"]

    def test_an_event_still_ingests_when_no_association_resolves(self):
        rec = _ingestor(
            alerts=[_alert()],
            association_resolver=_resolver(_entry(dotnet_app_id="never-configured")),
        ).ingest_alerts(token="T").records[0]
        assert rec["event"]["event_signature"]
        assert "associations" not in rec["app_insights"]
        assert rec["app_insights"]["association_unresolved"][0]["reason"] == (
            aia.REASON_DOTNET_NOT_CONFIGURED
        )

    def test_an_unconfigured_component_keeps_the_pre_t3_wrapper_shape(self):
        rec = _ingestor(
            alerts=[_alert()], association_resolver=_resolver(),
        ).ingest_alerts(token="T").records[0]
        assert set(rec["app_insights"]) == {
            "component_id", "component_name", "surface", "signal_kind",
            "monitored_application_id",
        }

    def test_a_failing_resolver_never_breaks_ingestion(self):
        class _Boom:
            def resolve(self, component_id):
                raise RuntimeError("resolver exploded")

        result = _ingestor(alerts=[_alert()], association_resolver=_Boom()).ingest_alerts(
            token="T"
        )
        assert result.emitted_count == 1
        assert "associations" not in result.records[0]["app_insights"]
