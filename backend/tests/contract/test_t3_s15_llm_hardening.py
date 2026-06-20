"""ENT-3 / T3-S15-A — LLM Enrichment Enterprise Hardening contract tests.

Covers AC1–AC11 of the ENT-3 story:

  AC1  deterministic first pass (same input → same prompt)
  AC2  rule-based rewrite pattern handlers
  AC3  rule rewrite fires hallucination_guard.rewritten, no second LLM call
  AC4  LLM rewrite timeout → bullet dropped, hallucination_guard.removed
       (reason=dropped_timeout)
  AC5  generic bullet dropped without LLM rewrite, reason=dropped_generic
  AC6  preliminary=True when run_count < 10
  AC7  preliminary=True when any entity is unresolved
  AC8  preliminary=False only when all three gates pass
  AC9  (frontend — covered by Vitest T3_S15_EnrichmentEvidenceTrace.test.tsx)
  AC10 sparse graph (< 3 entities) → llm_grounded=False, no guard, no error
  AC11 graph_truncated + entity counts populated from the ENT-4 graph

The LLM boundary is mocked so the deterministic logic (rule rewrites, gates,
fallback, grounding) runs for real. Tests are hermetic — they use the temp DB
from conftest and never require a live API key.
"""
import logging

import pytest

from app import hallucination_guard as hg
from app.enrichment_quality import (
    MIN_AVG_CONFIDENCE,
    MIN_BASELINE_RUNS,
    evaluate_preliminary_status,
)
from app.graph_context import (
    MAX_GRAPH_ENTITIES,
    SPARSE_GRAPH_THRESHOLD,
    GraphContext,
    build_graph_context,
)
from app.llm_enrichment import build_grounded_opp_prompt


def _entity(name, status="resolved", conf=0.9, etype="person", source="jira", eid=None):
    return {
        "entity_id": eid or f"ent_{name.lower().replace(' ', '_')}",
        "entity_type": etype,
        "display_name": name,
        "source_system": source,
        "resolution_confidence": conf,
        "resolution_status": status,
        "run_count": 5,
    }


# ───────────────────────────── AC2 — rule-based rewrite ─────────────────────

class TestRuleBasedRewrite:
    def test_owns_pattern(self):
        out = hg.rule_based_rewrite("Acme Corp owns the billing queue", ["Acme Corp"])
        assert out == "the billing queue remain unresolved"

    def test_member_pattern_keeps_team(self):
        out = hg.rule_based_rewrite(
            "John Smith is a member of Billing Team", ["John Smith"]
        )
        assert out == "A team member is assigned to Billing Team"

    def test_and_own_pattern(self):
        out = hg.rule_based_rewrite(
            "John Smith and Jane Doe own the queue items", ["John Smith", "Jane Doe"]
        )
        assert out == "Team members own the queue items"

    def test_fallback_replaces_name_with_team_member(self):
        out = hg.rule_based_rewrite(
            "Ghost Person reviewed the escalation last Tuesday afternoon",
            ["Ghost Person"],
        )
        assert "Ghost Person" not in out
        assert "a team member" in out

    def test_no_hallucinated_name_survives_any_pattern(self):
        for bullet, names in [
            ("Acme Corp owns the queue backlog", ["Acme Corp"]),
            ("John Smith is a member of Billing Team", ["John Smith"]),
            ("John Smith and Jane Doe own the items", ["John Smith", "Jane Doe"]),
        ]:
            out = hg.rule_based_rewrite(bullet, names)
            for n in names:
                assert n not in out


# ───────────────────────── validate_and_recover paths ──────────────────────

class TestValidateAndRecover:
    def test_clean_bullet_returned_unchanged(self):
        bullet = "The billing queue grew by forty percent over the last week"
        assert hg.validate_and_recover(bullet, set(), "org", "run") == bullet

    def test_rule_rewrite_returns_coherent_bullet_no_name(self):
        out = hg.validate_and_recover(
            "Acme Corp owns the billing escalation backlog items", set(), "org", "run"
        )
        assert out == "the billing escalation backlog items remain unresolved"
        assert "Acme Corp" not in out

    def test_ac3_rule_rewrite_fires_rewritten_telemetry_no_llm_call(self, caplog, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(hg, "_invoke_rewrite_llm", lambda p: called.__setitem__("n", called["n"] + 1))
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            out = hg.validate_and_recover(
                "Acme Corp owns the billing escalation backlog items",
                set(),
                "org",
                "run",
            )
        assert out == "the billing escalation backlog items remain unresolved"
        assert called["n"] == 0, "rule rewrite must not call the LLM"
        assert "hallucination_guard.rewritten" in caplog.text

    def test_ac5_generic_bullet_dropped_without_llm(self, caplog, monkeypatch):
        called = {"n": 0}
        monkeypatch.setattr(hg, "_invoke_rewrite_llm", lambda p: called.__setitem__("n", called["n"] + 1))
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            out = hg.validate_and_recover("Ghost Person owns it", set(), "org", "run")
        assert out is None
        assert called["n"] == 0, "generic drop must not call the LLM"
        assert "hallucination_guard.removed" in caplog.text
        assert "dropped_generic" in caplog.text

    def test_ac4_llm_rewrite_timeout_drops_bullet(self, caplog, monkeypatch):
        import time

        # Force the rule rewrite to look incoherent so we reach the LLM path,
        # and make the bullet worth saving (references the resolved entity).
        monkeypatch.setattr(hg, "is_coherent", lambda text: False)
        monkeypatch.setattr(hg, "_invoke_rewrite_llm", lambda p: time.sleep(1.0))
        resolved = {"billing queue"}
        bullet = "Ghost Person manages the Billing Queue every single morning"
        with caplog.at_level(logging.INFO, logger="app.telemetry"):
            out = hg.validate_and_recover(bullet, resolved, "org", "run")
        assert out is None
        assert "hallucination_guard.removed" in caplog.text
        assert "dropped_timeout" in caplog.text

    def test_llm_rewrite_success_returns_clean_bullet(self, monkeypatch):
        monkeypatch.setattr(hg, "is_coherent", lambda text: False)
        monkeypatch.setattr(
            hg, "_invoke_rewrite_llm", lambda p: "The Billing Queue has unassigned items piling up"
        )
        resolved = {"billing queue"}
        stats = hg.GuardStats()
        out = hg.validate_and_recover(
            "Ghost Person manages the Billing Queue every morning",
            resolved,
            "org",
            "run",
            stats=stats,
        )
        assert out == "The Billing Queue has unassigned items piling up"
        assert stats.llm_rewrites == 1

    def test_llm_rewrite_leaving_name_intact_is_dropped(self, monkeypatch):
        # A rewrite that still contains the hallucinated name must be rejected.
        monkeypatch.setattr(hg, "is_coherent", lambda text: False)
        monkeypatch.setattr(hg, "_invoke_rewrite_llm", lambda p: "Ghost Person still here in the Billing Queue")
        out = hg.validate_and_recover(
            "Ghost Person manages the Billing Queue every morning",
            {"billing queue"},
            "org",
            "run",
        )
        assert out is None


# ──────────────────── rewrite concurrency cap (ENT-4 review #1) ──────────────

class TestRewriteConcurrencyCap:
    def test_semaphore_bounded_to_max_concurrency(self):
        # The cap is a BoundedSemaphore sized to REWRITE_MAX_CONCURRENCY so
        # abandoned (timed-out) rewrite threads can never accumulate without
        # bound under load.
        assert hg.REWRITE_MAX_CONCURRENCY == 5
        assert isinstance(hg._rewrite_semaphore, type(__import__("threading").BoundedSemaphore()))

    def test_saturated_pool_drops_without_spawning_thread(self, monkeypatch):
        # When every slot is held, a rewrite request fails fast (TimeoutError →
        # caller drops the bullet) rather than starting an (N+1)-th thread. Use a
        # fresh capacity-1 semaphore so the test is isolated from any rewrite
        # thread still in flight from an earlier test.
        import threading

        monkeypatch.setattr(hg, "_rewrite_semaphore", threading.BoundedSemaphore(1))
        spawned = {"n": 0}
        monkeypatch.setattr(
            hg, "_invoke_rewrite_llm", lambda p: spawned.__setitem__("n", spawned["n"] + 1)
        )
        assert hg._rewrite_semaphore.acquire(blocking=False)  # saturate the pool
        with pytest.raises(TimeoutError):
            hg.llm_rewrite_bullet(
                "Ghost Person manages the Billing Queue",
                ["Ghost Person"],
                {"billing queue"},
                timeout_ms=50,
            )
        assert spawned["n"] == 0, "must not invoke the LLM when pool is saturated"

    def test_slot_released_after_normal_rewrite(self, monkeypatch):
        # A completed rewrite returns its slot so steady-state load is unaffected.
        import threading

        sem = threading.BoundedSemaphore(1)
        monkeypatch.setattr(hg, "_rewrite_semaphore", sem)
        monkeypatch.setattr(
            hg, "_invoke_rewrite_llm",
            lambda p: "The Billing Queue has unassigned items piling up steadily",
        )
        hg.llm_rewrite_bullet(
            "Ghost Person manages the Billing Queue",
            ["Ghost Person"],
            {"billing queue"},
            timeout_ms=500,
        )
        # The single slot is free again (released by the worker), and a second
        # acquire then fails — proving exactly one slot was returned, not leaked.
        assert sem.acquire(blocking=False) is True
        assert sem.acquire(blocking=False) is False
        sem.release()


# ────────────────────────── is_coherent / is_worth_saving ───────────────────

class TestCoherenceChecks:
    def test_short_bullet_not_coherent(self):
        assert hg.is_coherent("it remain unresolved") is False

    def test_long_clean_bullet_coherent(self):
        assert hg.is_coherent("the billing queue backlog has grown sharply this week") is True

    def test_dangling_trailing_not_coherent(self):
        assert hg.is_coherent("the billing queue backlog has grown because of") is False

    def test_worth_saving_requires_resolved_reference(self):
        assert hg.is_worth_saving("Ghost Person owns the Billing Queue", {"billing queue"}) is True
        assert hg.is_worth_saving("Ghost Person owns something", set()) is False


# ─────────────────────────── proper-noun extraction ─────────────────────────

class TestProperNouns:
    def test_extracts_multiword_name(self):
        nouns = hg.extract_proper_nouns("Jane Doe escalated the case")
        assert "Jane Doe" in nouns

    def test_ignores_common_sentence_opener(self):
        nouns = hg.extract_proper_nouns("Tickets are piling up in the queue")
        assert "Tickets" not in nouns

    def test_strips_leading_observation_tag(self):
        nouns = hg.extract_proper_nouns("[OBSERVED] Jane Doe owns the queue")
        assert "OBSERVED" not in nouns
        assert "Jane Doe" in nouns


# ─────────────────────── AC6 / AC7 / AC8 — preliminary gate ─────────────────

class TestPreliminaryGate:
    def test_ac6_run_count_below_threshold(self):
        prelim, reason = evaluate_preliminary_status(
            {"entities": [_entity("Jane Doe")]}, 3, "org"
        )
        assert prelim is True
        assert "3 of 10" in reason

    def test_ac7_unresolved_entity(self):
        prelim, reason = evaluate_preliminary_status(
            {"entities": [_entity("Jane Doe", status="ambiguous")]},
            MIN_BASELINE_RUNS + 2,
            "org",
        )
        assert prelim is True
        assert "require resolution" in reason

    def test_low_confidence_gate(self):
        prelim, reason = evaluate_preliminary_status(
            {"entities": [_entity("Jane Doe", conf=0.5)]}, MIN_BASELINE_RUNS, "org"
        )
        assert prelim is True
        assert "Entity confidence is 0.50" in reason

    def test_ac8_all_gates_pass(self):
        prelim, reason = evaluate_preliminary_status(
            {"entities": [_entity("Jane Doe", conf=0.9), _entity("Bob Lee", conf=0.95)]},
            MIN_BASELINE_RUNS,
            "org",
        )
        assert prelim is False
        assert reason is None

    def test_empty_entities_gate_not_applicable(self):
        # Packs that do not extract entities (sqlserver_opsignal,
        # github_engineering) carry an empty entity list. The entity-resolution
        # gates (2 & 3) do not apply, so — once baseline maturity (Gate 1) is met
        # — the finding is NOT flagged preliminary on entity grounds (review #3).
        prelim, reason = evaluate_preliminary_status({"entities": []}, MIN_BASELINE_RUNS, "org")
        assert prelim is False
        assert reason is None

    def test_empty_entities_still_subject_to_baseline_gate(self):
        # Gate 1 is independent of entities: an immature baseline is still
        # preliminary even when there are no entities to evaluate.
        prelim, reason = evaluate_preliminary_status({"entities": []}, 2, "org")
        assert prelim is True
        assert "Baseline context is still accumulating" in reason

    def test_gate_ordering_baseline_first(self):
        # run_count<10 AND unresolved → baseline reason surfaces (checked first).
        prelim, reason = evaluate_preliminary_status(
            {"entities": [_entity("Jane Doe", status="ambiguous")]}, 2, "org"
        )
        assert prelim is True
        assert "Baseline context is still accumulating" in reason

    def test_threshold_constants(self):
        assert MIN_BASELINE_RUNS == 10
        assert MIN_AVG_CONFIDENCE == 0.8


# ───────────────────── AC10 / AC11 — graph context build ────────────────────

class TestGraphContext:
    def test_ac10_sparse_below_threshold(self):
        gc = build_graph_context("org", "run", entities=[_entity("Jane Doe")], relationships=[])
        assert gc.is_sparse is True
        assert gc.entity_count == 1

    def test_not_sparse_at_threshold(self):
        ents = [_entity(f"Person {i}", eid=str(i)) for i in range(SPARSE_GRAPH_THRESHOLD)]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert gc.is_sparse is False

    def test_ac11_truncates_at_15_with_counts(self):
        ents = [_entity(f"Person {i:02d}", eid=str(i)) for i in range(20)]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert gc.truncated is True
        assert gc.entity_count == 20
        assert gc.entity_count_shown == MAX_GRAPH_ENTITIES == 15
        assert gc.truncation_note  # non-empty note when truncated

    def test_not_truncated_under_cap(self):
        ents = [_entity(f"Person {i}", eid=str(i)) for i in range(5)]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert gc.truncated is False
        assert gc.truncation_note == ""
        assert gc.entity_count_shown == 5

    def test_resolved_names_only_includes_resolved(self):
        ents = [
            _entity("Jane Doe", status="resolved"),
            _entity("Maybe Person", status="ambiguous"),
            _entity("Bob Lee", status="resolved"),
        ]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        assert "jane doe" in gc.resolved_names
        assert "bob lee" in gc.resolved_names
        assert "maybe person" not in gc.resolved_names

    def test_build_is_deterministic(self):
        ents = [_entity(f"Person {i}", eid=str(i)) for i in range(6)]
        a = build_graph_context("org", "run", entities=list(ents), relationships=[])
        b = build_graph_context("org", "run", entities=list(reversed(ents)), relationships=[])
        # Ordering of the input list must not change the rendered summary.
        assert a.observed_summary == b.observed_summary
        assert a.resolved_names == b.resolved_names


# ───────────────────── AC1 — deterministic grounded prompt ──────────────────

class TestGroundedPrompt:
    def _ctx(self):
        ents = [_entity(f"Person {i}", eid=str(i)) for i in range(4)]
        gc = build_graph_context("org", "run", entities=ents, relationships=[])
        signal = {
            "detector_display_name": "SLA breach rate elevated",
            "metric_value": 0.42,
            "threshold": 0.2,
            "trend_direction": "rising",
            "baseline_context": "up from 0.15 baseline",
            "corroboration_label": "Corroborated across Jira and ServiceNow",
        }
        return signal, gc

    def test_four_sections_present(self):
        signal, gc = self._ctx()
        prompt = build_grounded_opp_prompt(signal, gc, "pack domain context", "Acme Org")
        for section in (
            "=== SIGNAL CONTEXT ===",
            "=== DIRECTLY OBSERVED ENTITIES AND RELATIONSHIPS ===",
            "=== DOMAIN CONTEXT ===",
            "=== OUTPUT INSTRUCTIONS ===",
        ):
            assert section in prompt

    def test_forbids_invented_names_and_requires_tags(self):
        signal, gc = self._ctx()
        prompt = build_grounded_opp_prompt(signal, gc, "", "Acme Org")
        assert "Do not invent names" in prompt
        assert "[OBSERVED]" in prompt
        assert "[INFERRED" in prompt

    def test_ac1_same_input_same_output(self):
        signal, gc = self._ctx()
        p1 = build_grounded_opp_prompt(signal, gc, "domain", "Acme Org")
        p2 = build_grounded_opp_prompt(signal, gc, "domain", "Acme Org")
        assert p1 == p2

    def test_signal_context_values_rendered(self):
        signal, gc = self._ctx()
        prompt = build_grounded_opp_prompt(signal, gc, "", "Acme Org")
        assert "SLA breach rate elevated" in prompt
        assert "Corroborated across Jira and ServiceNow" in prompt


# ───────────── KV entity-load fallback to entities table (review #5) ─────────

class TestGraphContextKvFallback:
    def test_resolved_names_recovered_from_entities_table_when_kv_empty(self):
        import sqlite3
        import uuid
        from datetime import datetime, timezone

        from app import db
        from app.graph_context import build_graph_context

        org = f"org_fb_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()
        # Prior run is created first (lower rowid = earlier), current run after.
        prior_run = f"run_prior_{uuid.uuid4().hex[:6]}"
        cur_run = f"run_cur_{uuid.uuid4().hex[:6]}"
        db.upsert_run(prior_run, {"id": prior_run, "org_id": org, "status": "done", "startedAt": now})
        db.upsert_run(cur_run, {"id": cur_run, "org_id": org, "status": "done", "startedAt": now})

        # Entity first seen in the prior run — and deliberately NOT written into
        # the current run's KV blob (the PR #113 gap this fallback closes).
        con = db.connect()
        try:
            con.execute(
                """
                INSERT INTO entities (
                    id, org_id, entity_type, canonical_name, display_name,
                    source_system, source_record_id, resolution_confidence,
                    resolution_status, first_seen_run_id, last_seen_run_id,
                    run_count, metadata, created_at, updated_at
                ) VALUES (%s, %s, 'person', 'alice smith', 'Alice Smith', 'jira', NULL,
                          0.95, 'resolved', %s, %s, 7, NULL, %s, %s)
                """,
                (str(uuid.uuid4()), org, prior_run, prior_run, now, now),
            )
            con.commit()
        finally:
            con.close()

        # Current run's KV has no entities → fallback to the accumulated set.
        db.run_kv_set("entities", cur_run, [])
        ctx = build_graph_context(org, cur_run)
        assert "alice smith" in ctx.resolved_names

    def test_kv_entities_take_precedence_over_table(self):
        import uuid

        from app import db
        from app.graph_context import build_graph_context

        org = f"org_kv_{uuid.uuid4().hex[:8]}"
        run_id = f"run_kv_{uuid.uuid4().hex[:6]}"
        db.run_kv_set("entities", run_id, [_entity("Kv Person")])
        ctx = build_graph_context(org, run_id)
        # KV present → used directly, no table query needed.
        assert "kv person" in ctx.resolved_names


# ──────────────── tag enforcement after guard rewrite (ENT-4 review #2) ──────

class TestTagEnforcement:
    def _grounded_ctx(self):
        return GraphContext(
            entity_count=5,
            entity_count_shown=5,
            truncated=False,
            is_sparse=False,
            resolved_names=["jane doe"],
            observed_summary="Entities:\n- Jane Doe (person, jira)",
            truncation_note="",
            relationship_count=0,
        )

    def test_untagged_bullet_gets_unverified_tag(self, monkeypatch):
        import json as _json

        from app import llm_enrichment as le

        # First-pass LLM emits one correctly-tagged bullet and one with NO tag
        # (common non-compliance). Neither references a hallucinated name, so the
        # guard returns both unchanged — exercising only the tag-enforcement step.
        payload = {
            "aiSummary": "Queue backlog is rising for this organisation.",
            "aiWhyBullets": [
                "[OBSERVED] Jane Doe owns the escalation backlog items",
                "The backlog grew sharply over the last several weeks",
            ],
            "aiRisks": ["SLA breaches will continue"],
            "aiSuggestedNextSteps": ["Rebalance the queue"],
        }
        monkeypatch.setattr(le, "_call_claude", lambda prompt, max_tokens: _json.dumps(payload))

        out = le._enrich_opportunity_grounded(
            opp={"id": "opp_tag", "title": "Backlog"},
            graph_context=self._grounded_ctx(),
            pack_id=None,
            org_id="org-tag",
            run_id="run-tag",
            corroboration_label=None,
        )

        bullets = out["aiWhyBullets"]
        assert len(bullets) == 2
        # Every bullet reaching the frontend carries a recognised provenance tag.
        for b in bullets:
            assert b.startswith("[OBSERVED]") or b.startswith("[INFERRED") or b.startswith("[UNVERIFIED]")
        # The originally-untagged bullet is now explicitly UNVERIFIED, never bare.
        assert any(b.startswith("[UNVERIFIED] The backlog grew") for b in bullets)


# ──────────────────── T7 — telemetry event registration ─────────────────────

class TestTelemetryRegistration:
    def test_new_event_types_registered(self):
        from app.telemetry import REGISTERED_EVENT_TYPES

        for name in (
            "hallucination_guard.removed",
            "hallucination_guard.rewritten",
            "llm.enrichment_grounded",
        ):
            assert name in REGISTERED_EVENT_TYPES

    def test_existing_event_types_intact(self):
        from app.telemetry import REGISTERED_EVENT_TYPES

        for name in ("run.started", "run.completed", "entity.extraction_completed"):
            assert name in REGISTERED_EVENT_TYPES


# ─────────────── AC10 / AC11 — pipeline integration via run_llm_enrichment ───

class TestEnrichmentPipeline:
    def _opp(self):
        return {
            "id": "opp_001",
            "title": "SLA breaches rising",
            "aiRationale": "Deterministic rationale.",
            "_debug": {"detector_id": "sla_breach_rate", "metric_value": 0.4},
        }

    def test_ac10_sparse_graph_falls_back_no_grounding(self):
        from app import db
        from app.llm_enrichment import run_llm_enrichment

        run_id = "run_sparse_ac10"
        db.run_kv_set("entities", run_id, [_entity("Solo Person")])  # 1 entity < 3
        result = run_llm_enrichment(
            run_id=run_id, opps=[self._opp()], evidence=[], org_id="org-x"
        )
        per = result["perOpportunity"]["opp_001"]
        assert per["llm_grounded"] is False
        assert per["hallucination_rewrites"] == 0
        assert per["hallucination_llm_rewrites"] == 0
        assert per["hallucination_removals"] == []

    def test_no_org_id_falls_back(self):
        from app import db
        from app.llm_enrichment import run_llm_enrichment

        run_id = "run_no_org"
        db.run_kv_set("entities", run_id, [_entity(f"P{i}", eid=str(i)) for i in range(5)])
        result = run_llm_enrichment(run_id=run_id, opps=[self._opp()], evidence=[])
        per = result["perOpportunity"]["opp_001"]
        assert per["llm_grounded"] is False

    def test_ac11_grounded_populates_graph_fields(self):
        from app import db
        from app.llm_enrichment import run_llm_enrichment

        run_id = "run_grounded_ac11"
        ents = [_entity(f"Person {i:02d}", eid=str(i)) for i in range(20)]
        db.run_kv_set("entities", run_id, ents)
        result = run_llm_enrichment(
            run_id=run_id, opps=[self._opp()], evidence=[], org_id="org-y"
        )
        per = result["perOpportunity"]["opp_001"]
        assert per["llm_grounded"] is True
        assert per["graph_entity_count"] == 20
        assert per["graph_entity_count_shown"] == 15
        assert per["graph_truncated"] is True
        # preliminary is present and a bool (gate evaluated before storing)
        assert isinstance(per["preliminary"], bool)

    def test_pipeline_output_is_deterministic_without_llm(self):
        from app import db
        from app.llm_enrichment import run_llm_enrichment

        run_id = "run_determinism"
        ents = [_entity(f"Person {i}", eid=str(i)) for i in range(5)]
        db.run_kv_set("entities", run_id, ents)

        def _per():
            r = run_llm_enrichment(run_id=run_id, opps=[self._opp()], evidence=[], org_id="org-z")
            p = dict(r["perOpportunity"]["opp_001"])
            return p

        assert _per() == _per()
