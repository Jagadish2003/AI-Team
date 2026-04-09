## **1. Flow Activity Proxy**

**Description:**

Estimates how actively a Flow is being triggered. High-volume trigger
objects combined with simpler Flows indicate repetitive work that an AI
agent could handle better.

**Formula:**

flow_activity_score = (\
(records_created_on_trigger_object_last_90_days / 90) \# daily creation
rate\
× (1 / flow_element_count) \# simplicity factor\
× active_flow_count_on_same_object \# competition / redundancy\
)

**Data Sources (Tier A):**

-   Case / Task / custom object record counts → EntityDefinition + SOQL
    COUNT on trigger object

-   Flow details → Tooling API FlowVersionView

-   Element count → Retrieved via Metadata API on the Flow definition
    (Tier A)

**Required Queries:**

\-- 1. Daily record creation rate on trigger object\
SELECT COUNT(Id) as record_count\
FROM {TriggerObjectType}\
WHERE CreatedDate = LAST_N_DAYS:90\
\-- 2. Active flows per object (Tooling API)\
SELECT TriggerObjectType, COUNT(Id) as active_flow_count\
FROM FlowVersionView\
WHERE Status = \'Active\'\
GROUP BY TriggerObjectType

**Output Range:** ≥ 0 (higher = stronger automation candidate)

**Worked Example:**

-   Object Case created 450 records in last 90 days → daily rate = 5

-   Flow has 8 elements

-   2 active flows on Case

→ flow_activity_score = 5 × (1/8) × 2 = 1.25

## **2. Approval Delay Proxy**

**Description:**

Estimates delay caused by pending approvals by measuring the average age
of currently pending approval records.

**Formula:**

approval_delay_score = AVG(\
(TODAY () - ProcessInstance.CreatedDate)\
)\
WHERE ProcessInstance.Status = \'Pending\'\
GROUP BY ProcessNode.Name

**Data Sources (Tier A):**

-   ProcessInstance

-   ProcessInstanceStep

**Required Query:**

SELECT\
ProcessNodeName,\
AVG(DAY_ONLY(CreatedDate)) as avg_age_days, \-- or use date diff in
code\
COUNT(Id) as pending_count\
FROM ProcessInstanceStep\
WHERE ProcessInstanceId IN (\
SELECT Id FROM ProcessInstance WHERE Status = \'Pending\'\
)\
AND StepStatus = \'Pending\'\
GROUP BY ProcessNodeName

**Output Range:** ≥ 0 days (higher = more delay → higher value)

**Worked Example:**

-   34 pending records in \"Legal Review\" step

-   Average age = 6.2 days

→ approval_delay_score = 6.2 for \"Legal Review\"

## **3. Case Handoff Friction Proxy**

**Description:**

Measures unnecessary owner/queue changes on Cases. Frequent handoffs
indicate manual coordination that an AI agent could automate
(auto-routing, smart escalation, etc.).

**Formula:**

handoff_score =\
COUNT(CaseHistory records WHERE Field = \'OwnerId\' OR Field =
\'Owner\')\
/ COUNT(Case WHERE CreatedDate = LAST_N_DAYS:90)

**Data Sources (Tier A):**

-   CaseHistory

-   Case

**Required Queries:**

\-- Numerator: Owner changes in last 90 days\
SELECT COUNT(Id) as handoff_count\
FROM CaseHistory\
WHERE Field IN (\'OwnerId\', \'Owner\')\
AND CreatedDate = LAST_N_DAYS:90\
\-- Denominator: Cases created in last 90 days\
SELECT COUNT(Id) as case_count\
FROM Case\
WHERE CreatedDate = LAST_N_DAYS:90

**Output Range:** ≥ 0 (higher = more friction)

**Worked Example:**

-   1,247 owner-change history records

-   847 Cases created in last 90 days

→ handoff_score = 1247 / 847 ≈ 1.47 handoffs per Case

## **4. Knowledge Gap Proxy**

**Description:**

Estimates how often closed Cases are resolved without using existing
Knowledge Articles. High gap = repetitive problem-solving from memory →
strong candidate for a knowledge-retrieval + case-resolution agent.

**Formula:**

knowledge_gap_score = 1 - (\
COUNT(Case WHERE Id IN (SELECT CaseId FROM CaseArticle))\
/ COUNT(Case WHERE IsClosed = true AND CreatedDate = LAST_N_DAYS:90)\
)

**Data Sources (Tier A):**

-   Case

-   CaseArticle

**Required Query (single efficient query recommended):**

SELECT\
COUNT(Id) as total_closed_cases,\
COUNT(CASE WHEN Id IN (SELECT CaseId FROM CaseArticle) THEN 1 END) as
cases_with_kb\
FROM Case\
WHERE IsClosed = true\
AND CreatedDate = LAST_N_DAYS:90

**Output Range:** 0.0 -- 1.0 (higher = larger knowledge gap)

**Worked Example:**

-   500 closed Cases in last 90 days

-   180 have a linked Knowledge Article

→ knowledge_gap_score = 1 - (180 / 500) = 0.64

## **5. Permission Bottleneck Proxy**

**Description:**

Identifies steps where many records are pending approval but very few
users have the required permissions. Classic human bottleneck suitable
for agentic escalation or auto-approval logic.

**Formula:**

bottleneck_score =\
COUNT(ProcessInstance WHERE Status = \'Pending\' AND ProcessNode.Name =
\[step\])\
/ COUNT(users assigned to approver role or permission set for that step)

**Data Sources (Tier A):**

-   ProcessInstance + ProcessInstanceStep

-   PermissionSet + PermissionSetAssignment + Group / GroupMember (for
    queues/roles)

**Required Queries:**

\-- Pending count per step\
SELECT ProcessNodeName, COUNT(Id) as pending_count\
FROM ProcessInstanceStep\
WHERE StepStatus = \'Pending\'\
GROUP BY ProcessNodeName\
\-- Approver count (simplified: users with relevant PermissionSet or
Group membership)\
SELECT COUNT(DISTINCT AssigneeId) as approver_count\
FROM PermissionSetAssignment\
WHERE PermissionSetId IN (/\* relevant permission sets for this approval
step \*/)

**Output Range:** ≥ 0 (higher = worse bottleneck)

**Worked Example:**

-   34 pending records in \"Legal Review\" step

-   Only 2 users have the required approval permission

→ bottleneck_score = 34 / 2 = 17 records per approver

## **6. Flow Complexity Proxy**

**Description:**

Classifies Flows by complexity based on element count. Complex Flows are
expensive to maintain and often contain brittle logic that an AI agent
could replace with more flexible reasoning.

**Formula:**

complexity_tier =\
if element_count \> 20 → \'HIGH\'\
elif 10 ≤ element_count ≤ 20 → \'MEDIUM\'\
else → \'LOW\'

**Data Source (Tier A):**

-   Flow element count retrieved via Metadata API retrieve on Flow
    metadata type (or FlowVersionView + parsing if available).

**Output Range:** HIGH \| MEDIUM \| LOW (Enum --- always uppercase)

**Worked Example:**

-   Flow \"Auto_Assign_Case\" has 28 elements → complexity_tier = HIGH

## **7. Cross-System Echo Proxy**

**Description:**

Detects manual synchronization work between Salesforce and external
systems (ServiceNow, Jira). Cases containing ticket references suggest
humans are copying data back and forth --- perfect for an integration
agent.

**Formula:**

echo_score =\
COUNT(Case WHERE Subject LIKE \'%INC-%\'\
OR Subject LIKE \'%JIRA-%\'\
OR Description LIKE \'%INC-%\'\
OR Description LIKE \'%JIRA-%\')\
/ COUNT(Case WHERE CreatedDate = LAST_N_DAYS:90)

**Data Source (Tier A):**

-   Case object (standard SOQL text search)

**Required Query:**

SELECT\
COUNT(Id) as total_cases,\
COUNT(CASE WHEN\
Subject LIKE \'%INC-%\' OR Subject LIKE \'%JIRA-%\' OR\
Description LIKE \'%INC-%\' OR Description LIKE \'%JIRA-%\'\
THEN 1 END) as echo_cases\
FROM Case\
WHERE CreatedDate = LAST_N_DAYS:90

**Output Range:** 0.0 -- 1.0 (higher = more cross-system duplication)

**Worked Example:**

-   847 Cases created in last 90 days

-   198 contain external ticket patterns (INC-, JIRA-)

→ echo_score = 198 / 847 ≈ 0.23
