**Track B — Sprint 1 Design**

**SF-1.4: Scoring Rubric**

**Version v1.0 (Final)** | **April 13, 2026** | **Status: Ready for SME Review & Track C Sign-off**

### **1\. Purpose**

This document defines **exactly** how the algorithm converts a DetectorResult into four scored outputs: **Impact (1–10)**, **Effort (1–10)**, **Confidence (HIGH / MEDIUM / LOW)**, and **Tier (Quick Win / Strategic / Complex)**.

This is the implementation contract for **SF-2.6 (Scorer)**.

SF-1.4 does **not** define what fires a detector (that is SF-1.3) and does **not** define the evidence schema (that is SF-1.5). It only defines how to turn a fired detector’s output into scores. The same input must always produce the same output with zero ambiguity.

**Scope Note**: Fields such as recommendedConnected and totalConnected belong to the Track A Executive Report and are **not** part of SF-1.4.

### **2\. Where Scoring Fits in the Pipeline**

**DetectorResult → Scorer (SF-2.6) → OpportunityCandidate**

The Scorer is a **pure function** — no database, no network calls, no side effects. It reads detector\_id, metric\_value, threshold, and raw\_evidence.

### **3\. Impact Scoring (Scale: 1–10)**

**Formula**:

Impact = (Volume × 0.30) + (Delay/Friction × 0.25) + (Customer-facing × 0.20) + (Revenue/Compliance × 0.15) + (External systems × 0.10)

**Rounding & Clamping Rule**: After calculating the weighted sum, clamp the result to the range \[1, 10\], then round to the nearest integer. Impact and Effort are always whole numbers (hard requirement for deterministic unit tests).

#### **Impact Factor Derivation Table (Non-ambiguous)**

| **Detector** | **Volume Source (÷13 = weekly)** | **Delay/Friction Mapping (pts)** | **Customer-facing Default** | **Revenue/Compliance Default** | **Ext. Systems Default** |
| --- | --- | --- | --- | --- | --- |
| D1 REPETITIVE_AUTOMATION | records_90d | element_count: &lt;10=2, 10-20=5, &gt;20=8 | trigger_object=Case → 3, else 0 | 0 | 1 |
| D2 HANDOFF_FRICTION | total_cases_90d | handoff_score: &lt;1.5=2, 1.5-2.5=5, &gt;2.5=8 | Case → 3 | 0 | 1 |
| D3 APPROVAL_BOTTLENECK | pending_count | avg_delay_days: &lt;1=2, 1-3=5, &gt;3=8 | 0 (internal) | 2 | 1 |
| D4 KNOWLEDGE_GAP | closed_cases_90d | knowledge_gap_score: &lt;0.4=2, 0.4-0.6=5, &gt;0.6=8 | Case → 3 | 0 | 1 |
| D5 INTEGRATION_CONCENTRATION | flow_reference_count × avg trigger volume | 2 pts (fixed – latency risk) | 0 | 0 | 3 (2+ systems) |
| D6 PERMISSION_BOTTLENECK | pending_count | bottleneck_score: ≤10=2, 10-20=5, &gt;20=8 | 0 (internal) | 2 | 1 |
| D7 CROSS_SYSTEM_ECHO | sf_total_cases | MAX(sf_echo_score, sn_echo_score): &lt;0.15=2, 0.15-0.30=5, &gt;0.30=8 | 0 | 0 | 3 (SF+SN) |

**Required raw\_evidence keys** (must be present):

-   D1: flow\_id, flow\_label, trigger\_object, records\_90d, element\_count, active\_flow\_count\_on\_object, flow\_activity\_score
-   D2: owner\_changes\_90d, total\_cases\_90d, handoff\_score, top\_categories
-   D3: process\_name, pending\_count, avg\_delay\_days, approver\_count, bottleneck\_score
-   D4: closed\_cases\_90d, cases\_with\_kb\_link, knowledge\_gap\_score
-   D5: credential\_name, credential\_developer\_name, flow\_reference\_count, referencing\_flow\_ids, match\_type
-   D6: process\_name, pending\_count, approver\_count, bottleneck\_score
-   D7: sf\_echo\_count, sf\_total\_cases, sf\_echo\_score, sn\_match\_count, sn\_total\_incidents, sn\_echo\_score, matched\_patterns

### **4\. Effort Scoring (Scale: 1–10)**

**Formula**:

Effort = (Data availability × 0.30) + (Permission scope × 0.25) + (System boundary count × 0.25) + (Process complexity × 0.20)

**Effort Factor Table**

| **Factor** | **Weight** | **Low effort (2 pts)** | **Mid effort (5 pts)** | **High effort (8 pts)** |
| --- | --- | --- | --- | --- |
| Data availability tier | 30% | All Tier A signals | Some Tier B required | Requires Tier C |
| Permission scope count | 25% | &lt; 3 permission scopes | 3–6 scopes | &gt; 6 scopes |
| System boundary count | 25% | 1 system only | 2 systems | 3+ systems |
| Flow or process complexity | 20% | LOW (&lt;10 elements or simple) | MEDIUM (10–20 elements) | HIGH (&gt;20 or multi-step) |

All Track B detectors use only **Tier A** signals in v1 → Data availability defaults to **2 pts**.

### **5\. Confidence Assignment (HIGH / MEDIUM / LOW)**

Assigned after Impact and Effort. First matching rule wins.

| **Level** | **Criteria (all must be true)** | **Plain English** |
| --- | --- | --- |
| HIGH | Tier A data **AND** proxy score > 2× threshold **AND** record volume > 100 | Strong signal, large volume, well above threshold |
| MEDIUM | Tier A data **AND** proxy score ≥ 1× threshold **AND** record volume ≥ 20 | Clear signal with sufficient volume |
| LOW | Anything weaker (volume &lt; 20, score &lt; 1× threshold, proxy inference only, or cross-system without live data) | Weak/sparse/inferred – needs manual validation |

**Note**: LOW confidence does **not** suppress the opportunity. It only downgrades the tier by one level (see Section 6).

### **6\. Tier Assignment Rules (Deterministic)**

Rules applied in strict order. First match wins.

| **Step** | **Condition** | **Resulting Tier** | **Roadmap Stage** |
| --- | --- | --- | --- |
| 1 | Effort ≤ 4 | Quick Win | NEXT_30 |
| 2 | Effort ≥ 7 | Complex | NEXT_90 |
| 3 | Effort 5–6 | Strategic | NEXT_60 |
| 4 | If Confidence = LOW | Downgrade 1 level | Quick Win → Strategic (NEXT\_30 → NEXT\_60)   Strategic → Complex (NEXT\_60 → NEXT\_90)   Complex stays Complex |

**Important Note**: Tier is driven **only by Effort** (and LOW confidence downgrade). Impact does **not** influence tier. This rule supersedes any earlier draft that referenced Impact thresholds for tier assignment. Impact is preserved separately for the 2x2 matrix on Screen 7.

### **7\. Five Worked Examples (Official Unit Tests for SF-2.6)**

**Example 1 — D2: HANDOFF\_FRICTION**

| **Field** | **Value / Calculation** |
| --- | --- |
| Detector | D2 HANDOFF_FRICTION |
| metric_value | 1.6 (handoff_score) |
| raw_evidence | {owner_changes_90d: 480, total_cases_90d: 300, handoff_score: 1.6} |

**Impact Calculation**

| **Factor** | **Value** | **Points** | **Weighted** |
| --- | --- | --- | --- |
| Volume | 23/wk | 2 | 0.60 |
| Delay/Friction | 1.6 | 5 | 1.25 |
| Customer-facing | Case | 3 | 0.60 |
| Revenue/Compliance | - | 0 | 0.00 |
| External systems | 0 | 1 | 0.10 |
| **Total** | - | - | **2.55 → 3** |

**Effort Calculation** → **Effort = 2** (All Tier A, 1 scope, 1 system, LOW complexity)

**Confidence**: Tier A + proxy score 1.07× + volume 300 ≥ 20 → **MEDIUM**

**Tier**: Effort = 2 ≤ 4 → **Quick Win** (no downgrade)

**Final Result**: Impact = 3, Effort = 2, Confidence = MEDIUM, Tier = Quick Win

**Example 2 — D4: KNOWLEDGE\_GAP**

| **Field** | **Value / Calculation** |
| --- | --- |
| Detector | D4 KNOWLEDGE_GAP |
| metric_value | 0.5 |
| raw_evidence | {closed_cases_90d: 60, cases_with_kb: 30, knowledge_gap_score: 0.5} |

**Impact**: **3** (same logic as Example 1)

**Effort**: **2**

**Confidence**: Tier A + 1.25× + volume 60 → **MEDIUM**

**Tier**: **Quick Win**

**Final Result**: Impact = 3, Effort = 2, Confidence = MEDIUM, Tier = Quick Win

**Example 3 — D6: PERMISSION\_BOTTLENECK**

| **Field** | **Value / Calculation** |
| --- | --- |
| Detector | D6 PERMISSION_BOTTLENECK |
| metric_value | 30.0 |
| raw_evidence | {process_name: 'Discount Approval', pending_count: 60, approver_count: 2, bottleneck_score: 30.0} |

**Impact Calculation**

| **Factor** | **Value** | **Points** | **Weighted** |
| --- | --- | --- | --- |
| Volume | 4.6/wk | 2 | 0.60 |
| Delay/Friction | 30.0 | 8 | 2.00 |
| Customer-facing | Internal | 0 | 0.00 |
| Revenue/Compliance | Both | 3 | 0.45 |
| External systems | 0 | 1 | 0.10 |
| **Total** | - | - | **3.15 → 3** |

**Effort**: **3**

**Confidence**: Tier A + 3× + volume 60 → **MEDIUM** (volume not >100)

**Tier**: **Quick Win**

**Final Result**: Impact = 3, Effort = 3, Confidence = MEDIUM, Tier = Quick Win

**Example 4 — D3: APPROVAL\_BOTTLENECK**

| **Field** | **Value / Calculation** |
| --- | --- |
| Detector | D3 APPROVAL_BOTTLENECK |
| metric_value | 5.0 |
| raw_evidence | {process_name: 'Discount Approval', pending_count: 60, avg_delay_days: 5.0, approver_count: 2, bottleneck_score: 30.0} |

**Impact**: **3**

**Effort**: **3**

**Confidence**: Tier A + 1.67× + volume 60 → **MEDIUM**

**Tier**: **Quick Win**

**Note**: v1 scoring is per DetectorResult. De-duplication or merging of overlapping opportunities (D3 & D6) is handled downstream in SF-2.x, **not** in the Scorer.

**Final Result**: Impact = 3, Effort = 3, Confidence = MEDIUM, Tier = Quick Win

**Example 5 — D1: REPETITIVE\_AUTOMATION (HIGH confidence)**

| **Field** | **Value / Calculation** |
| --- | --- |
| Detector | D1 REPETITIVE_AUTOMATION |
| metric_value | 2.128 |
| raw_evidence | {flow_count: 4, avg_element_count: 6.25, records_90d: 300, trigger_object: 'Case', flow_activity_score: 2.128} |

**Impact Calculation**

| **Factor** | **Value** | **Points** | **Weighted** |
| --- | --- | --- | --- |
| Volume | 23/wk | 2 | 0.60 |
| Delay/Friction | High rep | 5 | 1.25 |
| Customer-facing | Case | 3 | 0.60 |
| Revenue/Compliance | Compliance | 2 | 0.30 |
| External systems | 0 | 1 | 0.10 |
| **Total** | - | - | **2.85 → 3** |

**Effort**: **2**

**Confidence**: Tier A + 3.55× + volume 300 > 100 → **HIGH**

**Tier**: **Quick Win**

**Final Result**: Impact = 3, Effort = 2, Confidence = HIGH, Tier = Quick Win

### **8\. Worked Examples Summary**

| **Detector** | **Title** | **Impact** | **Effort** | **Confidence** | **Tier** |
| --- | --- | --- | --- | --- | --- |
| D2 | Handoff Friction | 3 | 2 | MEDIUM | Quick Win |
| D4 | Knowledge Gap | 3 | 2 | MEDIUM | Quick Win |
| D6 | Permission Bottleneck | 3 | 3 | MEDIUM | Quick Win |
| D3 | Approval Bottleneck | 3 | 3 | MEDIUM | Quick Win |
| D1 | Repetitive Automation | 3 | 2 | HIGH | Quick Win |

**SME Note**: Impact scores are low in the dev org due to sandbox-level volumes. In production orgs with high weekly volumes, Impact will naturally rise to 6–8+ range.

### **9\. Definition of Done**

-   ✅ docs/scoring\_rubric.md is complete with all tables, rules, and five worked examples.
-   ✅ Every factor table entry reviewed and confirmed realistic for enterprise Salesforce orgs.
-   ✅ All five worked examples produce deterministic outputs.
-   ✅ The five worked examples are specific enough to become unit tests for SF-2.6.
-   ✅ Tier assignment rules confirmed against AgentIQ roadmap mapping.
-   Track C sign-off pending before SF-1.5 or SF-2.6 begins.

### **10\. What Comes After SF-1.4**

-   SF-1.5 (Evidence Schema) can start in parallel.
-   SF-2.6 (Scorer) implementation can begin. The five worked examples above are the official unit tests.