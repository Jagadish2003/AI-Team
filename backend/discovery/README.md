# AgentIQ Discovery Algorithm — backend/discovery

Sprint 2 implementation of the Track B discovery algorithm.

## Quick Start (Offline Mode — No Credentials Required)

```bash
# From repo root
pip install -r backend/requirements.txt --break-system-packages

# Run the algorithm against fixture data
python -m backend.discovery.runner --mode offline

# Run all tests
pytest backend/discovery/tests/ -v
```

Expected output at SF-2.1 (stubs): `Opportunities produced: 0`
Expected output at SF-2.8 (complete): all 7 detectors fire with real scores.

---

## Environment Variables

| Variable | Required for | Description |
|---|---|---|
| `INGEST_MODE` | All | `offline` (default) or `live` |
| `SF_INSTANCE_URL` | Live mode | Salesforce org URL, e.g. `https://myorg.my.salesforce.com` |
| `SF_ACCESS_TOKEN` | Live mode | OAuth access token for Salesforce |
| `SERVICENOW_URL` | Live mode | ServiceNow instance URL |
| `SERVICENOW_TOKEN` | Live mode | ServiceNow REST API token |
| `JIRA_URL` | Live mode | Jira instance URL |
| `JIRA_TOKEN` | Live mode | Jira API token |

---

## Offline Mode (default)

All three ingestion modules read from fixture files:
- `ingest/fixtures/salesforce_sample.json` — crafted to fire all 7 detectors
- `ingest/fixtures/servicenow_sample.json` — D7 ServiceNow echo signal
- `ingest/fixtures/jira_sample.json` — D7 Jira cross-reference signal

Fixture data is internally consistent:
- Salesforce Case IDs `CS-1001` through `CS-1300` are referenced in ServiceNow incidents and Jira issues
- Echo scores: SF=0.25, SN=0.16, Jira=0.22 — all above D7 threshold of 0.15

---

## Project Structure

```
backend/discovery/
    models.py               — DetectorResult dataclass (SF-2.1)
    runner.py               — CLI orchestrator (SF-2.8)
    scorer.py               — Impact/Effort/Confidence/Tier scorer (SF-2.6)
    evidence_builder.py     — Evidence object builder (SF-2.7)
    ingest/
        __init__.py         — INGEST_MODE switch
        salesforce.py       — Salesforce ingestion (SF-2.2)
        servicenow.py       — ServiceNow ingestion (SF-2.3)
        jira.py             — Jira ingestion (SF-2.4)
        fixtures/
            salesforce_sample.json
            servicenow_sample.json
            jira_sample.json
    detectors/
        __init__.py
        repetition.py               — D1 (SF-2.5)
        handoff_friction.py         — D2 (SF-2.5)
        approval_delay.py           — D3 (SF-2.5)
        knowledge_gap.py            — D4 (SF-2.5)
        integration_concentration.py— D5 (SF-2.5)
        permission_bottleneck.py    — D6 (SF-2.5)
        cross_system_echo.py        — D7 (SF-2.5)
    tests/
        test_models.py              — DetectorResult dataclass tests
        test_ingest_offline.py      — Fixture loading + cross-system consistency
        test_detectors_stub.py      — All 7 detectors callable (stub)
        test_runner_offline.py      — End-to-end runner smoke test

```

---

## Fixture Design — Detectors That Fire in Offline Mode

| Detector | Signal | Value | Threshold | Fires? |
|---|---|---|---|---|
| D1 REPETITIVE_AUTOMATION | flow_activity_score | 2.128 | > 0.6 | ✅ |
| D2 HANDOFF_FRICTION | handoff_score | 1.6 | > 1.5 | ✅ |
| D3 APPROVAL_BOTTLENECK | avg_delay_days | 5.0 | > 3 | ✅ |
| D4 KNOWLEDGE_GAP | knowledge_gap_score | 0.5 | > 0.40 | ✅ |
| D5 INTEGRATION_CONCENTRATION | flow_reference_count | 3 | >= 3 | ✅ |
| D6 PERMISSION_BOTTLENECK | bottleneck_score | 30.0 | > 10 | ✅ |
| D7 CROSS_SYSTEM_ECHO | MAX(sf=0.25, sn=0.16) | 0.25 | > 0.15 | ✅ |

---

## Live Mode (SF-3.1 onwards)

Set `INGEST_MODE=live` and provide all credentials via environment variables.
ServiceNow and Jira are optional — if credentials are missing, those modules
log a warning and return empty data gracefully. The algorithm continues with
Salesforce-only signals.

```bash
export INGEST_MODE=live
export SF_INSTANCE_URL=https://myorg.my.salesforce.com
export SF_ACCESS_TOKEN=<token>
python -m backend.discovery.runner --mode live --output opportunities.json
```
