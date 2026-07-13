"""
R18-C1 T4 — template-driven lending terminology across serve surfaces.

When the Commercial Lending template is active on a run, the user-facing WORDING
served for findings, roadmap, blueprint, and the executive report must speak
lending language (customer→borrower, account→facility, obligation→covenant,
rationale→credit memo, approval→approval gate). Detector logic and stored data
are unchanged — only the served string values are adapted. A run with no
template stays generic (safe no-op), and technical/identifier fields are never
rewritten.

These tests inject a single deterministic opportunity so the assertions do not
depend on pipeline wording, then exercise each serve endpoint with and without
the template active.
"""
import os


def _auth_headers():
    token = os.getenv("DEV_JWT", "dev-token-change-me")
    return {"Authorization": f"Bearer {token}"}


def _start_run(client):
    r = client.post(
        "/api/runs/start",
        headers=_auth_headers(),
        json={
            "connectedSources": ["ServiceNow", "Jira"],
            "uploadedFiles": [],
            "sampleWorkspaceEnabled": False,
            "mode": "offline",
        },
    )
    assert r.status_code == 200
    return r.json()["runId"]


# A finding whose generic wording contains every lending mapping, plus technical
# fields that must survive verbatim.
_OPP = {
    "id": "opp_term_1",
    "title": "Customer approval backlog",
    "category": "Approval bottleneck",
    "description": (
        "Accounts awaiting approval from the customer, with unmet obligations "
        "and no rationale recorded."
    ),
    "aiRationale": (
        "The customer approval process stalls on open accounts and obligations."
    ),
    "detector_id": "APPROVAL_BOTTLENECK",
    "_debug": {"detector_id": "APPROVAL_BOTTLENECK"},
    "tier": "Quick Win",
    "impact": 5,
    "effort": 3,
    "confidence": "High",
    "decision": "UNREVIEWED",
    "evidenceIds": [],
    "requiredPermissions": [],
}


def _seed_opp(run_id):
    from app import db

    db.run_kv_set("opps", run_id, [dict(_OPP)])
    # The run started via /api/runs/start already persisted roadmap /
    # executive_report / llm_enrichment from the real offline pipeline. Clear
    # them so the roadmap, executive-report, and enrichment endpoints rebuild
    # deterministically from our single injected opportunity (the build-from-opps
    # and enrichment-fallback code paths), instead of serving the pipeline's own
    # (non-lending) findings.
    db.run_kv_set("roadmap", run_id, None)
    db.run_kv_set("executive_report", run_id, None)
    db.run_kv_set("llm_enrichment", run_id, None)


def _activate_lending(run_id):
    from app import db

    run = db.get_run(run_id) or {}
    run["templateId"] = "commercial_lending"
    db.upsert_run(run_id, run)


# ── Findings / opportunities ──────────────────────────────────────────────────

def test_opportunities_use_lending_terms_when_template_active(client):
    run_id = _start_run(client)
    _seed_opp(run_id)
    _activate_lending(run_id)

    opps = client.get(
        f"/api/runs/{run_id}/opportunities", headers=_auth_headers()
    ).json()
    assert isinstance(opps, list) and opps
    opp = opps[0]

    # customer -> borrower, approval -> approval gate (title-cased preserved)
    assert "Borrower" in opp["title"]
    assert "approval gate" in opp["title"].lower()

    desc = opp["description"].lower()
    assert "facilities" in desc          # accounts -> facilities (y->ies)
    assert "borrower" in desc            # customer -> borrower
    assert "covenant" in desc            # obligations -> covenants
    assert "credit memo" in desc         # rationale -> credit memo

    # Technical / enum / identifier fields are NEVER rewritten.
    assert opp["detector_id"] == "APPROVAL_BOTTLENECK"
    assert opp["decision"] == "UNREVIEWED"
    assert opp["tier"] == "Quick Win"
    assert opp["_debug"]["detector_id"] == "APPROVAL_BOTTLENECK"


def test_opportunities_stay_generic_without_template(client):
    run_id = _start_run(client)
    _seed_opp(run_id)
    # No template activated → no-op transform, generic wording preserved.

    opps = client.get(
        f"/api/runs/{run_id}/opportunities", headers=_auth_headers()
    ).json()
    opp = opps[0]
    assert "Customer" in opp["title"]
    assert "Borrower" not in opp["title"]
    assert "customer" in opp["description"].lower()


def test_decision_response_uses_lending_terms(client):
    run_id = _start_run(client)
    _seed_opp(run_id)
    _activate_lending(run_id)

    r = client.post(
        f"/api/runs/{run_id}/opportunities/{_OPP['id']}/decision",
        headers=_auth_headers(),
        json={"decision": "APPROVED"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "Borrower" in body["title"]
    assert body["decision"] == "APPROVED"  # enum untouched


# ── Roadmap ────────────────────────────────────────────────────────────────────

def test_roadmap_embedded_findings_use_lending_terms(client):
    run_id = _start_run(client)
    _seed_opp(run_id)
    _activate_lending(run_id)

    roadmap = client.get(
        f"/api/runs/{run_id}/roadmap", headers=_auth_headers()
    ).json()
    blob = str(roadmap)
    assert "Borrower" in blob
    assert "Customer approval backlog" not in blob


# ── Executive report ───────────────────────────────────────────────────────────

def test_executive_report_uses_lending_terms(client):
    run_id = _start_run(client)
    _seed_opp(run_id)
    _activate_lending(run_id)

    report = client.get(
        f"/api/runs/{run_id}/executive-report", headers=_auth_headers()
    ).json()
    quick_wins = report.get("topQuickWins") or []
    assert quick_wins, "seeded Quick Win should appear in the executive report"
    assert "Borrower" in quick_wins[0]["title"]
    assert "approval gate" in quick_wins[0]["title"].lower()


# ── Blueprint / agent recommendation ────────────────────────────────────────────

def test_blueprint_uses_lending_terms(client):
    run_id = _start_run(client)
    _seed_opp(run_id)
    _activate_lending(run_id)

    bp = client.get(
        f"/api/runs/{run_id}/opportunities/{_OPP['id']}/blueprint",
        headers=_auth_headers(),
    ).json()
    # agentTopic derives from aiRationale (our controlled copy) → lending wording.
    topic = (bp.get("agentTopic") or "").lower()
    assert "borrower" in topic
    assert "approval gate" in topic
    # detectorId remains verbatim; Salesforce object API names (…__c) are outside
    # the terminology allowlist, so any that appear are served unchanged.
    assert bp["detectorId"] == "APPROVAL_BOTTLENECK"
    for action in bp.get("suggestedActions") or []:
        obj = action["object"]
        if obj.endswith("__c"):
            # An API name must never be pluralised/rewritten into prose.
            assert " " not in obj


# ── Enrichment (finding detail panel) ────────────────────────────────────────────

def test_enrichment_fallback_summary_uses_lending_terms(client):
    run_id = _start_run(client)
    _seed_opp(run_id)
    _activate_lending(run_id)

    # No llm_enrichment KV seeded → fallback serves aiRationale as aiSummary.
    enr = client.get(
        f"/api/runs/{run_id}/opportunities/{_OPP['id']}/enrichment",
        headers=_auth_headers(),
    ).json()
    summary = (enr.get("aiSummary") or "").lower()
    assert "borrower" in summary
    assert "approval gate" in summary
    assert "facilities" in summary
