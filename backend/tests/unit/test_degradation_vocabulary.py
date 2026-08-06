"""2.0-D4 T5 — the uniform degradation vocabulary and the completeness fact.

Pure unit tests. What is being protected:

* **one shape, not five** — the vocabularies the platform grew independently
  (``auth_failed``, ``unavailable``, ``credential_missing``, ``degraded``, …) all
  map onto one canonical set, so a surface renders any degradation without
  knowing which subsystem produced it;
* **an unmeasured component is never healthy** — generalising R18-B2's refusal to
  report zeros from a store that is down;
* **a partial run is never described as complete**, which is the acceptance bar
  for this subtask and is stated as a negative.
"""

from __future__ import annotations

import pytest

from app.degradation import (
    CANONICAL_STATUSES,
    COMPONENT_CONNECTOR,
    NATIVE_STATUS_MAP,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    STATUS_UNKNOWN,
    ComponentDegradation,
    RunCompleteness,
    canonical_status,
    is_healthy,
    worst,
)
from app.run_completeness import build_run_completeness


def run(**overrides):
    base = {
        "runId": "run_test",
        "inputs": {"systems": ["salesforce", "servicenow", "jira"]},
        "succeeded": ["salesforce", "servicenow", "jira"],
        "ingestErrors": {},
        "opportunities": [{"id": "opp_1"}],
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# One vocabulary
# --------------------------------------------------------------------------


class TestTheVocabularyIsUnified:
    @pytest.mark.parametrize(
        "native,expected",
        [
            ("ok", STATUS_OK),
            ("partial", STATUS_PARTIAL),
            ("auth_failed", STATUS_UNAVAILABLE),      # AWS
            ("failed", STATUS_FAILED),                # AWS
            ("unavailable", STATUS_UNAVAILABLE),      # ServiceNow SecOps tables
            ("credential_missing", STATUS_UNAVAILABLE),  # operational apps
            ("degraded", STATUS_PARTIAL),             # run stages
        ],
    )
    def test_every_native_vocabulary_maps_onto_the_canonical_set(self, native, expected):
        assert canonical_status(native) == expected

    def test_every_mapped_value_is_a_canonical_status(self):
        for native, canonical in NATIVE_STATUS_MAP.items():
            assert canonical in CANONICAL_STATUSES, f"{native} -> {canonical}"

    def test_an_unrecognised_status_is_unknown_never_ok(self):
        """A word this module has not been taught is not evidence of health.

        Treating it as healthy is how a new failure mode ships looking fine.
        """
        assert canonical_status("some_new_subsystem_state") == STATUS_UNKNOWN
        assert canonical_status(None) == STATUS_UNKNOWN
        assert canonical_status("") == STATUS_UNKNOWN

    def test_unknown_is_not_healthy(self):
        """The whole point of having UNKNOWN separate from ok."""
        assert not is_healthy(STATUS_UNKNOWN)
        assert is_healthy(STATUS_OK)
        for status in (STATUS_PARTIAL, STATUS_UNAVAILABLE, STATUS_FAILED):
            assert not is_healthy(status)

    def test_a_roll_up_is_as_bad_as_its_worst_component(self):
        assert worst([STATUS_OK, STATUS_OK]) == STATUS_OK
        assert worst([STATUS_OK, STATUS_PARTIAL]) == STATUS_PARTIAL
        assert worst([STATUS_PARTIAL, STATUS_UNAVAILABLE]) == STATUS_UNAVAILABLE
        assert worst([STATUS_UNAVAILABLE, STATUS_FAILED]) == STATUS_FAILED

    def test_rolling_up_nothing_is_unknown_not_ok(self):
        """An empty health picture is not a clean bill of health."""
        assert worst([]) == STATUS_UNKNOWN


class TestTheReportShapeIsActionable:
    def test_a_degradation_names_what_is_missing_and_what_to_do(self):
        """The four things the subtask asks for. A report missing any of them
        cannot be acted on and will be ignored."""
        d = ComponentDegradation(
            kind=COMPONENT_CONNECTOR, component="servicenow",
            status=STATUS_FAILED, native_status="error",
            attempted="Ingest servicenow", missing="No servicenow data",
            reason="HTTP 401", remedy="Reconnect on the Integration Hub",
        )
        payload = d.to_dict()
        for field in ("attempted", "missing", "reason", "remedy"):
            assert payload[field], f"{field} is what makes this actionable"

    def test_the_native_status_survives_the_mapping(self):
        """Converging must not discard the word an engineer needs to find the
        code path — auth_failed and credential_missing look the same to a
        customer but come from different places."""
        d = ComponentDegradation(
            kind=COMPONENT_CONNECTOR, component="aws",
            status=canonical_status("auth_failed"), native_status="auth_failed",
        )
        assert d.status == STATUS_UNAVAILABLE
        assert d.to_dict()["nativeStatus"] == "auth_failed"


# --------------------------------------------------------------------------
# The acceptance bar, stated as a negative.
# --------------------------------------------------------------------------


class TestAPartialRunIsNeverDescribedAsComplete:
    def test_a_clean_run_is_complete(self):
        c = build_run_completeness(run(), include_environment=False)
        assert c.complete is True
        assert c.status == STATUS_OK

    def test_a_run_missing_a_requested_source_is_not_complete(self):
        """The core failure: three sources requested, two delivered, and until
        now nothing said so — the findings simply came from fewer places."""
        c = build_run_completeness(
            run(succeeded=["salesforce", "jira"]), include_environment=False
        )
        assert c.complete is False
        assert "INCOMPLETE" in c.headline

    def test_the_missing_source_is_named_not_merely_omitted(self):
        c = build_run_completeness(
            run(succeeded=["salesforce", "jira"]), include_environment=False
        )
        assert any("servicenow" in m for m in c.missing_summary)

    def test_a_source_that_errored_is_reported_with_its_reason(self):
        c = build_run_completeness(
            run(succeeded=["salesforce", "jira"],
                ingestErrors={"servicenow": "HTTP 401: Session expired"}),
            include_environment=False,
        )
        component = next(x for x in c.components if x.component == "servicenow")
        assert component.status == STATUS_FAILED
        assert "401" in (component.reason or "")
        assert component.remedy, "a failure with no remedy is not actionable"

    def test_the_headline_tells_the_reader_the_findings_are_partial(self):
        """Not just that something failed — that what they are looking at is
        incomplete. Those are different sentences and only the second changes
        how the findings are read."""
        c = build_run_completeness(
            run(succeeded=["salesforce"]), include_environment=False
        )
        assert "partial" in c.headline.lower()

    def test_an_error_for_a_source_nobody_requested_still_counts(self):
        """An error nobody asked for is more surprising, not less."""
        c = build_run_completeness(
            run(ingestErrors={"confluence": "connection refused"}),
            include_environment=False,
        )
        assert c.complete is False
        assert any(x.component == "confluence" for x in c.components)

    def test_a_degraded_stage_makes_the_run_partial(self):
        c = build_run_completeness(
            run(perSystem={"servicenow": {"status": "degraded", "reason": "partial read"}}),
            include_environment=False,
        )
        assert c.complete is False

    def test_completeness_never_raises_on_a_malformed_run(self):
        """A completeness check that fell over would leave the surface with
        nothing to say — which defaults to looking fine."""
        for bad in (None, {}, {"inputs": "nonsense"}, {"succeeded": "x"},
                    {"ingestErrors": []}):
            build_run_completeness(bad, include_environment=False).to_dict()

    def test_the_report_is_json_serialisable(self):
        import json

        payload = build_run_completeness(
            run(succeeded=["jira"]), include_environment=False
        ).to_dict()
        assert json.loads(json.dumps(payload)) == payload


# --------------------------------------------------------------------------
# AC6 scenario 2 — model-mode unavailability.
# --------------------------------------------------------------------------


class TestModelUnavailabilityIsRunVisible:
    def test_a_hosted_embedding_provider_is_reported_as_unavailable(self, monkeypatch):
        """The subtlest of the three scenarios.

        The hosted provider has no embeddings endpoint, so it returns an empty
        list rather than an error: every chunk stays unembedded, retrieval
        matches nothing, and the run looks entirely normal. It is a run-visible
        degradation precisely because nothing else would reveal it.
        """
        from app.run_completeness import model_degradation

        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
        components = model_degradation({})
        embedding = [c for c in components if c.component == "embedding_provider"]
        assert embedding, "a silently inert embedding provider must be reported"
        assert embedding[0].status == STATUS_UNAVAILABLE
        assert "retrieval" in (embedding[0].missing or "").lower()
        assert embedding[0].remedy

    def test_a_configured_embedding_provider_is_not_flagged(self, monkeypatch):
        from app.run_completeness import model_degradation

        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "customer_tenant")
        components = model_degradation({})
        assert not [c for c in components if c.component == "embedding_provider"]

    def test_the_reason_explains_why_it_is_silent(self, monkeypatch):
        """A reason that says only 'not configured' would not convey that the
        failure mode is invisible, which is the thing an operator must know."""
        from app.run_completeness import model_degradation

        monkeypatch.setenv("MODEL_EMBEDDING_PROVIDER", "hosted")
        reason = model_degradation({})[0].reason or ""
        assert "silent" in reason.lower() or "empty list" in reason.lower()


# --------------------------------------------------------------------------
# AC6 scenario 3 — storage pressure.
# --------------------------------------------------------------------------


class TestStoragePressureIsDistinguishedNotCollapsed:
    def test_a_dead_primary_database_is_reported_as_failed(self, monkeypatch):
        from app import run_completeness as rc

        def boom():
            raise RuntimeError("connection refused")

        monkeypatch.setattr("app.db.connect", lambda *a, **k: boom())
        components = rc.storage_degradation()
        primary = [c for c in components if c.component == "primary_database"]
        assert primary and primary[0].status == STATUS_FAILED
        assert "refused" in (primary[0].reason or "")

    def test_an_unreachable_database_makes_retrieval_unknown_not_ok(self, monkeypatch):
        """R18-B2's posture, generalised: never report a healthy-looking number
        derived from an unhealthy source. If the primary is down, retrieval
        health was not measured — and 'unknown' is the honest word."""
        from app import run_completeness as rc

        def boom():
            raise RuntimeError("connection refused")

        monkeypatch.setattr("app.db.connect", lambda *a, **k: boom())
        components = rc.storage_degradation()
        retrieval = [c for c in components if c.component == "retrieval_store"]
        assert retrieval and retrieval[0].status == STATUS_UNKNOWN
        assert retrieval[0].status != STATUS_OK

    def test_the_two_storage_sub_cases_are_separate_components(self, monkeypatch):
        """Collapsing them would lose the distinction that matters: a dead
        primary stops everything, a dead retrieval store only stops citations."""
        from app import run_completeness as rc

        monkeypatch.setattr("app.db.connect",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        names = {c.component for c in rc.storage_degradation()}
        assert {"primary_database", "retrieval_store"} <= names


# --------------------------------------------------------------------------
# PR #561 review fixes — cases the first implementation got wrong.
# --------------------------------------------------------------------------


class TestASourceThatPartlySucceededIsNotCalledFailed:
    """A source can succeed AND error in the same run.

    Salesforce delivers 400 records and then times out, so it appears in both
    ``succeeded`` and ``ingestErrors``. The first implementation fell through to
    the failure branch and reported "No salesforce data contributed to this run's
    findings" — factually wrong, and damaging in a specific way: it makes every
    finding drawn from that source look unsupported.
    """

    def test_it_is_reported_as_partial(self):
        c = build_run_completeness(
            run(
                succeeded=["salesforce", "servicenow", "jira"],
                ingestErrors={"salesforce": "read timed out after 400 records"},
            ),
            include_environment=False,
        )
        sf = next(x for x in c.components if x.component == "salesforce")
        assert sf.status == STATUS_PARTIAL
        assert sf.status != STATUS_FAILED

    def test_it_does_not_claim_the_source_contributed_nothing(self):
        """The specific false statement the review found."""
        c = build_run_completeness(
            run(
                succeeded=["salesforce", "servicenow", "jira"],
                ingestErrors={"salesforce": "read timed out"},
            ),
            include_environment=False,
        )
        sf = next(x for x in c.components if x.component == "salesforce")
        assert "No salesforce data contributed" not in (sf.missing or "")
        assert sf.delivered, "a partial source must say what it DID deliver"

    def test_the_run_is_still_incomplete(self):
        """Softening the status must not soften the verdict — a partial read is
        still not a complete run."""
        c = build_run_completeness(
            run(
                succeeded=["salesforce", "servicenow", "jira"],
                ingestErrors={"salesforce": "read timed out"},
            ),
            include_environment=False,
        )
        assert c.complete is False

    def test_a_source_that_only_errored_is_still_failed(self):
        """Guards the fix from over-reaching: no success entry means no data."""
        c = build_run_completeness(
            run(
                succeeded=["servicenow", "jira"],
                ingestErrors={"salesforce": "auth failed"},
            ),
            include_environment=False,
        )
        sf = next(x for x in c.components if x.component == "salesforce")
        assert sf.status == STATUS_FAILED
        assert "No salesforce data contributed" in (sf.missing or "")


class TestASilentSourceIsUnknownNotUnavailable:
    """Requested, but the run recorded neither a success nor an error.

    ``unavailable`` is defined as "not an error, and actionable by the customer"
    — an absent credential, an unactivated plugin. A silent empty does not meet
    that bar: it is equally likely to be an ingestor defect that swallowed its own
    failure, and the wrong label sends an operator hunting for a credential
    problem that may not exist.
    """

    def test_it_is_unknown(self):
        c = build_run_completeness(
            run(succeeded=["salesforce", "jira"]),  # servicenow: no success, no error
            include_environment=False,
        )
        sn = next(x for x in c.components if x.component == "servicenow")
        assert sn.status == STATUS_UNKNOWN
        assert sn.status != STATUS_UNAVAILABLE

    def test_the_reason_says_the_cause_could_not_be_established(self):
        c = build_run_completeness(
            run(succeeded=["salesforce", "jira"]), include_environment=False
        )
        sn = next(x for x in c.components if x.component == "servicenow")
        assert "neither" in (sn.reason or "").lower()

    def test_the_remedy_does_not_send_the_operator_only_to_credentials(self):
        """The practical harm of the old label was a misdirected investigation."""
        c = build_run_completeness(
            run(succeeded=["salesforce", "jira"]), include_environment=False
        )
        sn = next(x for x in c.components if x.component == "servicenow")
        assert "defect" in (sn.remedy or "").lower()

    def test_the_run_is_still_incomplete(self):
        c = build_run_completeness(
            run(succeeded=["salesforce", "jira"]), include_environment=False
        )
        assert c.complete is False


class TestARetrievalStoreExceptionIsFailedNotUnavailable:
    """A pgvector misconfiguration raises out of a DB query.

    That is a component reached and not working — ``failed``. Labelling it
    ``unavailable`` understated it twice: it ranks lower in ``worst()``, so a
    run-wide roll-up read one notch healthier than reality, and it pointed the
    remedy at customer configuration rather than at a half-applied migration.
    """

    def test_the_status_is_failed(self, monkeypatch):
        from app import run_completeness as rc

        monkeypatch.setattr("app.db.connect", lambda *a, **k: _WorkingConnection())
        monkeypatch.setattr(
            "app.retrieval.metrics.freshness_metrics",
            lambda *a, **k: (_ for _ in ()).throw(
                RuntimeError('relation "retrieval_chunks" does not exist')
            ),
        )
        retrieval = [
            c for c in rc.storage_degradation() if c.component == "retrieval_store"
        ]
        assert retrieval and retrieval[0].status == STATUS_FAILED

    def test_the_rollup_reflects_the_worse_severity(self, monkeypatch):
        """The reason the status matters beyond wording."""
        assert worst([STATUS_OK, STATUS_FAILED]) == STATUS_FAILED
        assert worst([STATUS_OK, STATUS_UNAVAILABLE]) == STATUS_UNAVAILABLE
        assert worst([STATUS_UNAVAILABLE, STATUS_FAILED]) == STATUS_FAILED

    def test_the_remedy_points_at_the_migration_not_the_customer(self, monkeypatch):
        from app import run_completeness as rc

        monkeypatch.setattr("app.db.connect", lambda *a, **k: _WorkingConnection())
        monkeypatch.setattr(
            "app.retrieval.metrics.freshness_metrics",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("UndefinedTable")),
        )
        remedy = [
            c for c in rc.storage_degradation() if c.component == "retrieval_store"
        ][0].remedy or ""
        assert "migration" in remedy.lower()


class _WorkingConnection:
    """A primary DB that answers, so the retrieval probe is actually reached."""

    def cursor(self):
        return self

    def execute(self, *a, **k):
        return None

    def fetchone(self):
        return (1,)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
