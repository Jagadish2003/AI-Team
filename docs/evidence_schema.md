Track B — Sprint 1 Design

**SF-1.5: Evidence Schema**

Version v1.1 | April 13, 2026 | Status: SME Reviewed. Three precision locks applied. Awaiting Track C Sign-off

# **1\. Purpose**

Define the exact format that every evidence object must follow. Evidence objects are the traceable proof behind every opportunity the algorithm surfaces. They connect an opportunity directly to the measured data that caused the detector to fire.

Evidence is not rationale text. It is not an explanation of why the opportunity exists. It is a reference to the actual numbers the algorithm found in the org. A sentence saying 'Cases are frequently reassigned' is not evidence. A measured value of '480 owner changes across 300 cases (ratio 1.6, threshold 1.5) in the last 90 days' is evidence.

This document is the implementation contract for SF-2.7 (Evidence Builder). Once signed off, the schema does not change without Track C approval. Every Sprint 2 developer building the evidence builder uses this as their specification.

# **2\. Where Evidence Fits in the Pipeline**

DetectorResult → Scorer (SF-2.6) → Evidence Builder (SF-2.7) → OpportunityCandidate

The Evidence Builder receives one scored OpportunityCandidate at a time. It reads the raw\_evidence dict from the original DetectorResult and converts it into one or more structured evidence objects. Each evidence object gets its own unique ID. The IDs are added to the opportunity's evidenceIds array. The evidence objects are stored separately and linked to the opportunity.

The frontend (Screen 4 — Partial Results, Screen 6 — Analyst Review) reads evidence objects by their IDs and displays the snippets to the analyst. The analyst sees specific numbers, not algorithm output rationale.

# **3\. Evidence Object Schema**

Every evidence object must contain exactly these nine fields. No field may be omitted. No additional fields are permitted in v1.

| **Field** | **Type** | **Format / Allowed values** | **Notes** |
| --- | --- | --- | --- |
| **id** | string | ev_{source}_{6 chars} | source = sf, sn, jira. 6 chars = random alphanumeric. e.g. ev_sf_a3f92c |
| **tsLabel** | string | DD Mon YYYY, HH:MM (UTC only) | Always UTC. Python format string: datetime.utcnow().strftime("%d %b %Y, %H:%M"). Produces: "08 Apr 2026, 14:22". Never local time. Never ISO 8601. |
| **source** | enum | Salesforce \| ServiceNow \| Jira | The system the raw data came from. Never 'AgentIQ' or 'Algorithm'. |
| **evidenceType** | enum | Metric \| Config \| Ticket | Metric = computed score or rate. Config = metadata observation. Ticket = record-level pattern. |
| **title** | string | One sentence | What was observed. Must be specific to the detector. Must not be generic ('High volume detected'). |
| **snippet** | string | Measured value + threshold + volume | Must contain at least one specific number. Must reference the threshold exceeded. Must state the record volume it is based on. The Evidence Builder raises ValueError if snippet has no number. |
| **entities** | list[string] | List of entity IDs | System objects or processes this evidence relates to. May be empty list if no specific entity is implicated. |
| **confidence** | enum | HIGH \| MEDIUM \| LOW | Must match the confidence assigned by the Scorer (SF-2.6) for the parent opportunity. |
| **decision** | enum | UNREVIEWED \| APPROVED \| REJECTED | SF-2.x output: ALWAYS UNREVIEWED. No exceptions. APPROVED and REJECTED are Track A governance states set only by analyst action on Screen 6. The algorithm never sets these values. |

## **ID Source Abbreviation Mapping**

The source field and the id prefix are two different things that must never be confused.

-   source field value = full system name: Salesforce | ServiceNow | Jira
-   id prefix = short abbreviation: sf | sn | jira
-   In code: use the full name when setting source. Use the abbreviation only when generating the id string. Never swap them.

Mapping table (canonical — R6 validates against these prefixes):

| **id prefix** | **source field value** | **Correct vs wrong** |
| --- | --- | --- |
| **ev\_sf\_** | Salesforce | ev_sf_a3f92c ✓ ev_salesforce_a3f92c ✕ |
| **ev\_sn\_** | ServiceNow | ev_sn_7bx41d ✓ ev_servicenow_7bx41d ✕ |
| **ev\_jira\_** | Jira | ev_jira_c9m03e ✓ ev_jira_c9m03e same prefix, length varies |

R6 validation must reject any id that does not begin with ev\_sf\_, ev\_sn\_, or ev\_jira\_. An id starting with ev\_salesforce\_ or ev\_servicenow\_ fails R6.

# **4\. Enforcement Rules**

The Evidence Builder (SF-2.7) must enforce all of the following. Any rule violation raises a ValueError and the specific evidence object is not produced. The calling code (Runner, SF-2.8) catches the ValueError, logs it, and applies the v1 permissive failure mode: emit the opportunity with evidenceIds=\[\] and confidence downgraded to LOW.

| **Rule** | **Check** | **Error message if violated** |
| --- | --- | --- |
| **R1** | snippet contains at least one digit | ValueError: snippet must contain at least one measurable number |
| **R2** | source is one of the allowed enum values | ValueError: source '{value}' is not a valid source system |
| **R3** | evidenceType is one of the allowed enum values | ValueError: evidenceType '{value}' is not valid |
| **R4** | confidence is one of HIGH \| MEDIUM \| LOW | ValueError: confidence '{value}' is not valid |
| **R5** | title is non-empty and not a generic placeholder | ValueError: title must be a specific observation, not a generic placeholder |
| **R6** | id follows ev_{source}_{6 chars} format | ValueError: id '{value}' does not match required format ev_{{source}}_{{6chars}} |
| **R7** | decision is UNREVIEWED for algorithm-generated evidence | ValueError: decision must be UNREVIEWED for algorithm output. APPROVED/REJECTED are Track A states only. |

**Decision field — hard boundary:** Track B (SF-2.x) ALWAYS emits decision=UNREVIEWED. APPROVED and REJECTED are Track A states set only by analyst action on Screen 6. No algorithm code should ever set decision to anything other than UNREVIEWED.

**tsLabel field — format lock:** Always UTC. Always use Python format string: datetime.utcnow().strftime("%d %b %Y, %H:%M"). Output example: "08 Apr 2026, 14:22". Never use local timezone. Never use ISO 8601 format. The frontend sorts evidence using tsLabel string comparison — mixed formats break sort order silently.

Evidence failure mode (Permissive mode — v1): If evidence construction fails for a rule violation, the opportunity is still emitted but with two mandatory consequences: (1) its evidenceIds array is empty, and (2) its confidence is downgraded to LOW regardless of what the Scorer assigned. The frontend displays 'No evidence available' for that opportunity. This is valid algorithm output — it surfaces the opportunity for analyst review rather than silently dropping it. The 'every opportunity must have evidence' requirement is a Sprint 3 quality goal, not a v1 hard constraint.

# **5\. How to Choose evidenceType**

The evidenceType field is the most commonly confused. Use this guide:

| **evidenceType** | **When to use** | **Example** |
| --- | --- | --- |
| **Metric** | The evidence is a computed measurement: a rate, ratio, count, average, or score derived from querying the org data. | 'Avg 1.6 owner changes per Case across 300 cases in 90 days (threshold: 1.5)' |
| **Config** | The evidence is an observation about system metadata: a flow definition, a named credential, a permission set assignment, an approval process structure. | '4 active AutoLaunchedFlows on the Case object with average element count 6.25' |
| **Ticket** | The evidence is a pattern found in individual records: cases referencing ticket IDs, specific pending approvals, incident cross-references. | '75 of 500 Cases in 90 days contain INC- references in Subject or Description (15% echo rate)' |

# **6\. How to Write the snippet Field**

The snippet is what the analyst reads. It must be specific enough that the analyst can independently verify it by running a query in the org. Three things must always be present:

-   The measured value — the specific number produced by the detector (e.g. 1.6 owner changes per case)
-   The threshold — the value that was exceeded (e.g. threshold: 1.5)
-   The volume — how many records this is based on (e.g. across 300 cases)

Good snippet example:

"Avg 1.6 owner changes per Case (threshold: 1.5) across 300 Cases

in the last 90 days. Top categories: Access (4.0/case), Billing (3.2/case)."

Bad snippet examples (all would fail Rule R1 or produce unverifiable evidence):

"Cases are frequently reassigned between teams." ← no numbers

"High handoff friction detected in this org." ← no numbers, no volume

"Owner change rate above threshold." ← no specific values

# **7\. Five Worked Evidence Objects**

One example per firing detector from the SF-1.3 dev org run. These are the reference examples for SF-2.7 implementation. SME team: review each for domain accuracy and realistic language.

| **Example 1 — D2: HANDOFF\_FRICTION (evidenceType: Metric)**   {   "id": "ev\_sf\_hf8x2a",   "tsLabel": "08 Apr 2026, 14:22",   "source": "Salesforce",   "evidenceType": "Metric",   "title": "Elevated case owner reassignment rate across all categories",   "snippet": "480 owner changes recorded across 300 Cases in the last 90 days.   Avg 1.6 owner changes per Case (threshold: 1.5).   Top friction categories: Access (avg 4.0/case),   Integration (avg 4.0/case), Technical Support (avg 4.0/case).",   "entities": \["ent\_case\_all\_categories", "ent\_casehistory\_owner\_changes"\],   "confidence": "MEDIUM",   "decision": "UNREVIEWED"   }   _⚠️ SME Note: Confirm: does 'avg 4.0 owner changes per category' align with what Salesforce Service Cloud teams would consider high friction? Is 'Technical Support' the right category label or does the org use a different picklist value?_ |
| --- |

| **Example 2 — D6: PERMISSION\_BOTTLENECK (evidenceType: Metric)**   {   "id": "ev\_sf\_pb3k7r",   "tsLabel": "08 Apr 2026, 14:23",   "source": "Salesforce",   "evidenceType": "Metric",   "title": "Approval queue overloaded relative to available approvers",   "snippet": "60 ProcessInstance records pending for 'Discount Approval'.   2 active approvers identified via ProcessInstanceWorkitem.   Bottleneck score: 30.0 pending per approver (threshold: 10).   Avg queue age: 5.0 days.",   "entities": \["ent\_approval\_discount\_approval", "ent\_user\_approvers"\],   "confidence": "MEDIUM",   "decision": "UNREVIEWED"   }   _⚠️ SME Note: Confirm: is 'Discount Approval' the actual ProcessDefinition.Name in this org or was it renamed? Is 5-day average delay realistic for a discount approval process in enterprise sales orgs?_ |
| --- |

| **Example 3 — D3: APPROVAL\_BOTTLENECK (evidenceType: Metric)**   {   "id": "ev\_sf\_ab9m1c",   "tsLabel": "08 Apr 2026, 14:23",   "source": "Salesforce",   "evidenceType": "Metric",   "title": "Approval records aged beyond threshold with insufficient approver capacity",   "snippet": "60 pending 'Discount Approval' records with avg delay 5.0 days   (threshold: 3 days). Bottleneck score 30.0 (threshold: 10).   2 approvers responsible for all 60 pending items.",   "entities": \["ent\_approval\_discount\_approval"\],   "confidence": "MEDIUM",   "decision": "UNREVIEWED"   }   _⚠️ SME Note: Note: D3 and D6 produce separate evidence objects for the same approval process. This is correct — D3 fires on delay, D6 fires on concentration. Both will appear in the opportunity's evidenceIds array. SME: is it clear to an analyst why two evidence items reference the same process?_ |
| --- |

| **Example 4 — D4: KNOWLEDGE\_GAP (evidenceType: Metric)**   {   "id": "ev\_sf\_kg5p0w",   "tsLabel": "08 Apr 2026, 14:24",   "source": "Salesforce",   "evidenceType": "Metric",   "title": "Majority of closed cases resolved without Knowledge Article linkage",   "snippet": "30 of 60 closed Cases in the last 90 days have a linked Knowledge Article.   Knowledge gap score: 0.50 (threshold: 0.40).   50% of cases closed without KB reuse.",   "entities": \["ent\_case\_closed", "ent\_knowledge\_articles"\],   "confidence": "MEDIUM",   "decision": "UNREVIEWED"   }   _⚠️ SME Note: Confirm: is 'Knowledge Article linkage' the term Salesforce Service Cloud professionals use, or is it 'KB article' or 'article attachment'? The terminology in the snippet should match what analysts see in the Salesforce UI._ |
| --- |

| **Example 5 — D1: REPETITIVE\_AUTOMATION (evidenceType: Config)**   {   "id": "ev\_sf\_ra2n6d",   "tsLabel": "08 Apr 2026, 14:25",   "source": "Salesforce",   "evidenceType": "Config",   "title": "Multiple low-complexity AutoLaunchedFlows running on high-volume Case object",   "snippet": "4 active AutoLaunchedFlows on the Case trigger object.   Avg element count: 6.25 (LOW complexity).   300 Cases created in last 90 days (23/week).   Flow activity score: 2.128 (threshold: 0.6).   Flows: Case-Notify, Update-case-rec, Named-credential-flow,   Case-and-OPP-Flow.",   "entities": \["ent\_flow\_case\_notify", "ent\_flow\_update\_case\_rec",   "ent\_flow\_named\_cred", "ent\_flow\_case\_opp"\],   "confidence": "HIGH",   "decision": "UNREVIEWED"   }   _⚠️ SME Note: Confirm: are these the actual MasterLabel values from the org? The flow names in the snippet must match exactly what SF-1.1 Check 5 showed (Case and OPP Flow, Case-Notify, update case rec, Internal Testing, Simple\_Case\_Scheduled\_Flow). evidenceType is Config here because the evidence is a metadata observation about flow configuration, not a computed metric from runtime data._ |
| --- |

# **8\. Entity ID Conventions**

The entities array links evidence to named system objects. Entity IDs follow a prefix convention so the frontend can display them meaningfully. Entity IDs are produced by the ingestion modules (SF-2.2, SF-2.3, SF-2.4) and referenced here.

| **Prefix pattern** | **Represents** | **Example** |
| --- | --- | --- |
| ent_case_{category} | Salesforce Case object or category | ent_case_technical_support |
| ent_approval_{process} | Approval process definition | ent_approval_discount_approval |
| ent_flow_{label} | Salesforce Flow | ent_flow_case_notify |
| ent_knowledge_{type} | Knowledge article collection | ent_knowledge_articles |
| ent_user_{role} | User or user group | ent_user_approvers |
| ent_credential_{name} | Named Credential | ent_credential_servicenow |
| ent_incident_{system} | ServiceNow incident category | ent_incident_servicenow_access |

Labels in entity IDs use lowercase with underscores. Spaces and special characters are replaced. The ingestion modules produce entity IDs as part of their output. The Evidence Builder uses the entity IDs from the DetectorResult's raw\_evidence dict.

# **9\. What SF-2.7 (Evidence Builder) Must Implement**

The Evidence Builder is a pure Python function. It receives a DetectorResult and a scored OpportunityCandidate and returns a list of evidence objects. Given the same inputs it always returns the same outputs.

def build\_evidence(

detector\_result: DetectorResult,

opportunity: OpportunityCandidate,

id\_factory=None, # optional: callable() -> str, for deterministic test IDs

) -> List\[EvidenceObject\]:

"""

Convert raw\_evidence from DetectorResult into structured EvidenceObject list.

Raises ValueError if any enforcement rule (R1-R7) is violated.

Returns \[\] if evidence cannot be constructed (caller must then downgrade

opportunity confidence to LOW and set evidenceIds=\[\]).

id\_factory: if None, uses secrets.token\_hex(3) for random IDs.

Pass a seeded callable in tests for snapshot-stable IDs.

"""

Rules for the implementation:

-   One DetectorResult may produce one or more evidence objects. Most detectors produce one. D3 + D6 share the same source data but produce separate objects.
-   The snippet must be generated from the raw\_evidence dict fields — not from the metric\_value alone. The full context (volume, threshold, measured value) must come from raw\_evidence.
-   If raw\_evidence is missing a required key for the snippet, raise ValueError: 'Cannot construct evidence: missing key {key}'.
-   The id must be generated fresh each run using secrets.token\_hex(3) or equivalent. Do not reuse IDs between runs. For deterministic unit tests, pass an id\_factory callable that returns a stable sequence (e.g. itertools.count-based factory). This keeps production IDs random while making tests snapshot-testable.
-   The confidence must be passed in from the Scorer output — the Evidence Builder does not compute confidence independently.

# **10\. Definition of Done**

-   docs/evidence\_schema.md is complete with: all nine field definitions, all seven enforcement rules, the evidenceType guide, the snippet writing rules, and five worked examples.
-   SME team has reviewed all five worked examples for domain accuracy and confirmed the terminology matches what Salesforce professionals use.
-   Entity ID prefix conventions are agreed and documented.
-   The five worked examples are specific enough to become unit tests for SF-2.7 — same input raw\_evidence must produce the same output evidence object.
-   Schema is locked. Any post-sign-off change requires Track C approval and a version bump.
-   Track C has signed off before SF-2.7 coding begins.

# **11\. What Comes After SF-1.5**

SF-1.5 is the final Sprint 1 design document. Once it is signed off, Sprint 2 implementation begins in parallel across five tracks:

-   SF-2.1 — Project structure and offline/live framework (Python team, starts immediately)
-   SF-2.2 — Salesforce ingestion module (SME + Python, after SF-2.1)
-   SF-2.5 — Seven detector implementations (Python team, uses SF-1.3 as spec)
-   SF-2.6 — Scorer (Python team, uses SF-1.4 as spec)
-   SF-2.7 — Evidence Builder (Python team, uses SF-1.5 as spec)
-   SF-2.8 — Runner CLI — orchestrates everything end-to-end

The five worked examples in this document become the unit tests for SF-2.7. The Python team should be able to write all five tests before writing a single line of production Evidence Builder code.

SF-1.5 v1.1 | April 13, 2026 | Track B Sprint 1 Final | v1.1 locks: decision boundary, tsLabel format, source vs id prefix. SF-2.7 can begin after Track C sign-off.