"""2.0-A2 T1 — structural guard on the non-inference rule.

*"There is no code path anywhere that sets ``actioned`` without a human-supplied
date."* That is a definition-of-done item about the whole codebase, not about one
function, so it is enforced structurally: these tests read the source and fail the
build if a new call site appears that could reach ``actioned`` another way.

The failure this guards is silent by nature. If some future background job could
talk the platform into an ``actioned`` state, nothing would go red — an
opportunity would simply acquire an action date nobody supplied, and every
measurement computed from that pivot would look legitimate. T7 ("no outcome
without action") would become decorative.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import opportunity_lifecycle
from app.opportunity_lifecycle_states import ACTOR_HUMAN, STATE_ACTIONED

BACKEND = Path(__file__).resolve().parents[2]

#: The only module allowed to name the actioned state in a write position.
LIFECYCLE_WRITE_MODULES = {
    "app/opportunity_lifecycle.py",
    "app/opportunity_lifecycle_states.py",
}


class TestRecordActionSignature:
    def test_action_date_is_required_and_positional(self):
        """No default means a caller cannot omit it — the rule made structural."""
        sig = inspect.signature(opportunity_lifecycle.record_action)
        params = list(sig.parameters.values())
        names = [p.name for p in params]

        assert "action_date" in names, "record_action must take an action date"
        action_date = sig.parameters["action_date"]
        assert action_date.default is inspect.Parameter.empty, (
            "action_date must have NO default: a defaulted date fabricates the "
            "before/after boundary every later measurement is computed from"
        )
        assert action_date.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ), "action_date must be positional so it cannot be quietly dropped"

    def test_record_action_takes_no_actor_parameter(self):
        """The actor is hard-wired to human — not a caller-supplied value.

        An ``actor`` parameter would let a background job present itself as a
        person, which is exactly the inference this rule forbids.
        """
        sig = inspect.signature(opportunity_lifecycle.record_action)
        assert "actor" not in sig.parameters

    def test_system_transition_takes_no_action_date(self):
        """A system move cannot supply or alter the human-recorded pivot."""
        sig = inspect.signature(opportunity_lifecycle.system_transition)
        assert "action_date" not in sig.parameters


class TestOnlyOneWritePath:
    def test_record_action_is_the_only_caller_that_targets_actioned(self):
        """Inside the store, STATE_ACTIONED appears only in record_action.

        Any other function naming it as a transition target would be a second
        road to the same state, and the two would drift.
        """
        source = Path(BACKEND / "app" / "opportunity_lifecycle.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)

        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = ast.dump(node)
            if "STATE_ACTIONED" in body and node.name not in (
                "record_action",
                "_apply_transition",  # reads it to decide actioned_by/actioned_at
            ):
                offenders.append(node.name)
        assert not offenders, (
            f"these functions reference STATE_ACTIONED as well as record_action: "
            f"{offenders}. There must be exactly one road to 'actioned'."
        )

    def test_record_action_hard_wires_the_human_actor(self):
        source = inspect.getsource(opportunity_lifecycle.record_action)
        assert "ACTOR_HUMAN" in source
        assert "ACTOR_SYSTEM" not in source

    def test_no_other_module_writes_the_actioned_state(self):
        """Whole-tree sweep: only the lifecycle modules may name it.

        A new caller elsewhere is unlisted by construction and fails here without
        any test edit.
        """
        offenders = []
        for path in (BACKEND / "app").rglob("*.py"):
            rel = path.relative_to(BACKEND).as_posix()
            if rel in LIFECYCLE_WRITE_MODULES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "STATE_ACTIONED" in text or '"actioned"' in text or "'actioned'" in text:
                offenders.append(rel)
        assert not offenders, (
            "these modules name the 'actioned' state outside the lifecycle "
            f"modules: {offenders}. Route it through record_action() so the "
            "date requirement cannot be bypassed."
        )


class TestSystemCannotInfer:
    def test_system_transition_refuses_actioned_at_runtime(self):
        """Belt and braces: the data table already forbids it; prove the API does.

        No DB is touched — validation happens before any write.
        """
        from app.opportunity_lifecycle_states import (
            ACTOR_SYSTEM,
            STATE_OPEN,
            LifecycleTransitionError,
            validate_transition,
        )

        with pytest.raises(LifecycleTransitionError) as excinfo:
            validate_transition(STATE_OPEN, STATE_ACTIONED, ACTOR_SYSTEM)
        assert "never infers" in str(excinfo.value)

    def test_the_run_pipeline_only_ever_tracks_never_actions(self):
        """The materialization hooks may start tracking, nothing more.

        A run landing is not evidence that a customer deployed anything, so the
        pipeline's only lifecycle verb is ``ensure_tracked_many``.
        """
        for module in ("materialize_t2.py", "routes_sprint4_t1.py"):
            text = (BACKEND / "app" / module).read_text(encoding="utf-8")
            assert "ensure_tracked_many" in text, f"{module} should track new findings"
            assert "record_action" not in text, (
                f"{module} must never record an action — a run landing is not "
                "evidence that a change was deployed"
            )
            assert "system_transition" not in text, (
                f"{module} must not advance lifecycle state; T3 owns that, and "
                "only once a recorded action exists"
            )


class TestApiEdge:
    def test_there_is_no_generic_state_setting_route(self):
        """No PATCH {state: ...}: a client cannot name an arbitrary target.

        Recording an action is its own route with a required date; monitoring,
        measured and stalled are the platform's own moves.
        """
        source = (BACKEND / "app" / "routes_opportunity_lifecycle.py").read_text(
            encoding="utf-8"
        )
        assert "@router.patch" not in source
        assert "@router.put" not in source
        for platform_state in ("monitoring", "measured", "stalled"):
            assert f'/{platform_state}"' not in source, (
                f"a client must not be able to request the {platform_state!r} state"
            )

    def test_the_action_route_requires_the_date_in_its_request_model(self):
        from app.routes_opportunity_lifecycle import RecordActionRequest

        field = RecordActionRequest.model_fields["actionDate"]
        assert field.is_required(), (
            "actionDate must be required so a missing date is a 422 before any "
            "handler runs — never a defaulted value"
        )

    def test_every_route_is_analyst_gated(self):
        """Reads included: a lifecycle state is operational customer information."""
        source = (BACKEND / "app" / "routes_opportunity_lifecycle.py").read_text(
            encoding="utf-8"
        )
        route_decorators = source.count("@router.")
        gated = source.count('require_role("analyst")')
        assert gated >= route_decorators, (
            f"{route_decorators} routes but only {gated} analyst gates"
        )

    def test_no_route_reads_an_org_id_from_the_request(self):
        """Tenancy comes from the middleware, never from the caller."""
        source = (BACKEND / "app" / "routes_opportunity_lifecycle.py").read_text(
            encoding="utf-8"
        )
        assert "get_current_org_id()" in source
        for smell in ("orgId:", "org_id:", "body.orgId", "body.org_id"):
            assert smell not in source, (
                f"{smell!r} suggests an org id is being taken from the request; "
                "one org could then write another's lifecycle"
            )


class TestTelemetryRegisteredBeforeEmission:
    def test_the_event_type_is_registered(self):
        """record_event() raises for an unregistered type.

        So the registration must exist before the first emission site — which is
        why it was added to telemetry.py in this same change.
        """
        from app.telemetry import REGISTERED_EVENT_TYPES

        assert "opportunity.lifecycle_transitioned" in REGISTERED_EVENT_TYPES

    def test_the_audit_event_type_is_registered(self):
        from app.middleware.audit import (
            AUDIT_EVENT_REGISTRY,
            OPPORTUNITY_LIFECYCLE_TRANSITIONED,
        )

        assert OPPORTUNITY_LIFECYCLE_TRANSITIONED in AUDIT_EVENT_REGISTRY

    def test_the_store_emits_the_registered_name(self):
        source = inspect.getsource(opportunity_lifecycle._audit_and_emit)
        assert '"opportunity.lifecycle_transitioned"' in source
        assert "OPPORTUNITY_LIFECYCLE_TRANSITIONED" in source
