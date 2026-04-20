# SMOKE_DEMO_SF33.md — S1→S10 Manual Walkthrough
## SF-3.3 Demo Integration Verification
### Version v1.0 | April 18, 2026

---

## Purpose

This is the manual sign-off walkthrough that completes SF-3.3.
Automated contract tests confirm schema compatibility.
This document confirms every screen *renders correctly* with Track B data.

Niranjan must complete this walkthrough and sign off before Sprint 3 is marked done.

---

## Prerequisites — Run These First

```bash
# Step 1: Export Track B seed files (offline, no credentials)
python -m backend.discovery.offline_export

# Step 2: Run automated contract verifier
python -m backend.discovery.integration_verifier

# Step 3: Run all contract tests
pytest backend/discovery/tests/test_sf33_integration_verifier.py -v

# All three must pass before the manual walkthrough begins.
```

If the verifier fails, fix the issue and re-run before starting the walkthrough.

---

## Track A Backend Setup

```bash
# In Track A repo — load Track B seed data
cp /path/to/track_b/backend/seed/opportunities.json backend/seed/opportunities.json
cp /path/to/track_b/backend/seed/evidence.json backend/seed/evidence.json

# Load into Track A database
python backend/seed_loader.py

# Start Track A backend
uvicorn backend.app.main:app --reload --port 8000
```

In a second terminal:
```bash
# Start Track A frontend
npm install && npm run dev
```

Confirm: `http://localhost:5173` loads and backend responds at `http://localhost:8000/api/health`.

---

## Scope Boundary (Important)

SF-3.3 verifies: **Export → Seed → UI walkthrough**

It does NOT verify: Track A triggering Track B computation per run.
That is Sprint 4 scope. If the team cannot trigger discovery from the UI,
that is expected at this stage — use the seeded data.

---

## S1→S10 Walkthrough — 8 Minutes Target

### S1 — Integration Hub
**What to confirm:**
- [ ] Page loads without blank panels
- [ ] Salesforce connector shows connected status
- [ ] ServiceNow and Jira connectors visible

**Pass if:** Page renders with connector cards. No "undefined" text.

---

### S2 — Source Intake
**What to confirm:**
- [ ] Upload panel renders
- [ ] No runtime errors in browser console

**Pass if:** Page renders cleanly.

---

### S3 — Discovery Run
**What to confirm:**
- [ ] "Start Discovery" button visible
- [ ] Run status shows (even if mocked at this stage)
- [ ] runId is present in network requests or URL

**Pass if:** Run lifecycle UI renders. Note: live Track B trigger is Sprint 4.

---

### S4 — Partial Results (Evidence Cards)
**What to confirm:**
- [ ] At least 7 evidence cards render
- [ ] Each card shows: title, snippet with at least one number, source label, timestamp (tsLabel)
- [ ] evidenceType badges visible (Metric, Log, etc.)
- [ ] No card shows "undefined" or blank snippet

**Time target:** 30 seconds

**Pass if:** All 7 cards visible with real data, no blank panels.

---

### S4 → S6 — Evidence Linkage (Critical Check)
**What to confirm:**
- [ ] From S6, open any opportunity (e.g. "Reduce case routing friction")
- [ ] The opportunity shows evidenceIds in the details
- [ ] Click/navigate to the referenced evidence
- [ ] The S4 card for that evidenceId renders with entities populated

**This is the most common integration break.**

**Pass if:** evidenceId from S6 resolves to a visible S4 card with entity tags.

---

### S6 — Analyst Review
**What to confirm:**
- [ ] All 7 opportunities listed
- [ ] Each shows: title, category, tier badge, confidence badge (colour-coded)
- [ ] Confidence badges: HIGH = green, MEDIUM = amber, LOW = red
- [ ] decision = UNREVIEWED on all items (not pre-approved)
- [ ] aiRationale paragraph visible and populated for each item
- [ ] Override panel available (isLocked checkbox, rationaleOverride field, overrideReason)

**Time target:** 1 minute

**Pass if:** All items show correct confidence colours, UNREVIEWED state, and populated rationale.

---

### S7 — Opportunity Map
**What to confirm:**
- [ ] All 7 opportunities placed on the impact/effort quadrant
- [ ] No item sitting at origin (0,0)
- [ ] Quick Win items appear in the low-effort / high-impact quadrant region
- [ ] Confidence badge colours match S6 (HIGH=green, MEDIUM=amber, LOW=red)
- [ ] Clicking an item opens the detail panel showing aiRationale
- [ ] Detail panel shows override rationale if one was saved in S6

**Time target:** 1 minute

**Pass if:** Quadrant populated, no blank panels, colours consistent with S6.

---

### S9 — Pilot Roadmap
**What to confirm:**
- [ ] NEXT_30 column contains Quick Win opportunities
- [ ] NEXT_60 column contains Strategic opportunities (may be empty on fixture data)
- [ ] NEXT_90 column contains Complex opportunities (may be empty on fixture data)
- [ ] Tier bucketing matches what S6 showed

**Note:** Fixture data produces all Quick Win tier due to low volume (expected — impact bias confirmed in SF-3.2). NEXT_60/NEXT_90 may be empty. This is documented behaviour, not a failure.

**Pass if:** NEXT_30 populated with Quick Wins. No items in wrong column.

---

### S10 — Executive Report
**What to confirm:**
- [ ] sourcesAnalyzed shows correct connected source count (Salesforce + SN + Jira)
- [ ] topQuickWins section populated with at least 3 opportunities
- [ ] Opportunity titles match what S6/S7 showed
- [ ] No "undefined" or placeholder text
- [ ] snapshotBubbles or summary chart renders

**Time target:** 30 seconds

**Pass if:** Executive report shows real data — not mock placeholders.

---

## Sign-Off Checklist

| Screen | Confirmed | Notes |
|---|---|---|
| S1 — Integration Hub | | |
| S2 — Source Intake | | |
| S3 — Discovery Run | | |
| S4 — Evidence Cards | | |
| S4→S6 — Evidence Linkage | | |
| S6 — Analyst Review | | |
| S7 — Opportunity Map | | |
| S9 — Pilot Roadmap | | |
| S10 — Executive Report | | |

**Signed off by:** _______________________ **Date:** _____________

**SF-3.3 is complete when:**
1. `pytest backend/discovery/tests/test_sf33_integration_verifier.py` — 46 tests pass
2. `python -m backend.discovery.integration_verifier` — sf33_passed = true
3. This sign-off checklist is completed with no blank panels, no undefined fields, no runtime errors

---

## Fallback Plan (If Live Track A Is Not Available)

Run the verifier only (no UI walkthrough needed for contract testing):

```bash
python -m backend.discovery.offline_export
python -m backend.discovery.integration_verifier --report-path runs/sf33_report.json
pytest backend/discovery/tests/test_sf33_integration_verifier.py -v
```

Contract tests confirm schema compatibility end-to-end.
Full S1→S10 walkthrough can be done at the first World Tour rehearsal.

---

*SF-3.3 scope: Export → Seed → UI walkthrough. Not Track A triggering Track B (Sprint 4).*
