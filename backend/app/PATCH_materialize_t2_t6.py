"""
Sprint 4 T6 — Patch for materialize_t2.py
==========================================

This file shows EXACTLY what to add to materialize_t2.py to wire
T6 LLM enrichment into the materialisation pipeline.

The T6 enrichment must run AFTER opps and evidence are persisted (hard rule).

PATCH INSTRUCTIONS
------------------
In backend/app/materialize_t2.py, inside run_trackb_and_persist(),
find the executive_report try/except block that ends with:

    db.run_kv_set("executive_report", run_id, er)

ADD the following block immediately AFTER that try/except block
(before the status/audit finalisation):

    # T6 — LLM enrichment (post-processing, non-blocking)
    # Runs after all deterministic artifacts are persisted.
    # Failure does not fail the run.
    try:
        from .llm_enrichment import run_llm_enrichment, KV_LLM_ENRICHMENT
        exec_report = db.run_kv_get("executive_report", run_id, {})
        sources_analyzed = exec_report.get("sourcesAnalyzed", {})
        enrichment = run_llm_enrichment(
            run_id=run_id,
            opps=opps,
            evidence=ev,
            sources_analyzed=sources_analyzed,
        )
        db.run_kv_set(KV_LLM_ENRICHMENT, run_id, enrichment)

        # Patch aiExecutiveSummary into the stored executive report
        if enrichment.get("executiveSummary"):
            exec_report["aiExecutiveSummary"] = enrichment["executiveSummary"]
            db.run_kv_set("executive_report", run_id, exec_report)
    except Exception as e:
        errors["llm_enrichment"] = str(e)

That is the complete change to materialize_t2.py.

WHY THIS PLACEMENT
------------------
- opps and ev are persisted (hard rule: LLM runs after persistence)
- roadmap and executive_report are persisted
- try/except ensures LLM failure never fails the run
- errors["llm_enrichment"] surfaces the error in /status

VERIFICATION
------------
After applying, start a run and check:
    GET /api/runs/{runId}/llm-enrichment   → available: true
    GET /api/runs/{runId}/opportunities/{oppId}/enrichment  → aiSummary present

If ANTHROPIC_API_KEY is not set, llmGenerated will be false and
aiSummary will contain the existing aiRationale template text.
"""

# Reference: what the patched context looks like in materialize_t2.py
PATCH_CONTEXT = """
        try:
            from .executive_report_engine import build_executive_report
            roadmap = db.run_kv_get("roadmap", run_id, {})
            er = build_executive_report(run_id=run_id, opps=opps, roadmap=roadmap)
            sa = er.get("sourcesAnalyzed", {})
            sa["totalConnected"] = len(run_inputs.get("connectedSources", []))
            sa["uploadedFiles"] = len(run_inputs.get("uploadedFiles", []))
            sa["sampleWorkspaceEnabled"] = bool(run_inputs.get("sampleWorkspaceEnabled", False))
            er["sourcesAnalyzed"] = sa
            db.run_kv_set("executive_report", run_id, er)
        except Exception as e:
            errors["exec_report"] = str(e)
            db.run_kv_set("executive_report", run_id, { ... fallback ... })

        # ← ADD T6 BLOCK HERE ↓

        # T6 — LLM enrichment (post-processing, non-blocking)
        try:
            from .llm_enrichment import run_llm_enrichment, KV_LLM_ENRICHMENT
            exec_report = db.run_kv_get("executive_report", run_id, {})
            sources_analyzed = exec_report.get("sourcesAnalyzed", {})
            enrichment = run_llm_enrichment(
                run_id=run_id,
                opps=opps,
                evidence=ev,
                sources_analyzed=sources_analyzed,
            )
            db.run_kv_set(KV_LLM_ENRICHMENT, run_id, enrichment)
            if enrichment.get("executiveSummary"):
                exec_report["aiExecutiveSummary"] = enrichment["executiveSummary"]
                db.run_kv_set("executive_report", run_id, exec_report)
        except Exception as e:
            errors["llm_enrichment"] = str(e)

        # partial if at least one requested system failed; complete if all succeeded
        status = "complete" if len(succeeded) == len(systems) else "partial"
"""
