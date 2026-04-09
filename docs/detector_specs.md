### **Detector 1 — Repetition Without Decision**

**Business Problem:**

Some automated processes run repeatedly on high volumes of records but contain no meaningful decision logic — they always do the same thing regardless of context. These are the easiest and highest-confidence candidates for an AI agent because the agent replaces a mechanical process, not a judgement call.

**Input:**

-   flow\_activity\_score (from Task 1.2)
-   complexity\_tier (from Task 1.2)
-   Flow type from FlowVersionView

**Fires when:**

flow\_activity\_score > 0.6 **AND** complexity\_tier == "LOW" **AND** Flow ProcessType indicates Record-Triggered (e.g., "AutoLaunchedFlow" with TriggerType = "Record")

**Output Label:**

REPETITIVE\_AUTOMATION

**Example that fires:**

A Record-Triggered Flow on the Case object that automatically sets Status = 'In Progress' on every new Case.

-   450 Cases created in 90 days → daily rate = 5
-   Flow has 8 elements → complexity\_tier = LOW
-   flow\_activity\_score = 1.25

→ Detector fires.

**Example that does not fire:**

A Screen Flow with 24 elements that guides users through a multi-step data entry process.

-   flow\_activity\_score = 0.3
-   complexity\_tier = HIGH

→ Detector does not fire.

### **Detector 2 — Handoff Friction**

**Business Problem:**

When Cases or records are reassigned multiple times before resolution, it means the initial routing was wrong, humans are spending time figuring out who should own the work, and resolution time increases with every handoff. An agent that routes correctly on first assignment eliminates this friction entirely.

**Input:**

-   handoff\_score (from Task 1.2), broken down by Case.Type (category)

**Fires when:**

handoff\_score > 1.5 for any Case category that has volume > 50 Cases in last 90 days.

**Output Label:**

HANDOFF\_FRICTION

**Example that fires:**

Technical Support Cases:

-   Average 2.8 owner changes per Case
-   847 Cases created in 90 days

→ Detector fires.

**Example that does not fire:**

Billing Cases:

-   Average 0.9 owner changes per Case
-   120 Cases created in 90 days

→ Detector does not fire.

### **Detector 3 — Approval Chain Delay**

**Business Problem:**

Approval processes create predictable bottlenecks when a small number of approvers are responsible for a large volume of records, or when the approval criteria are simple enough that they could be automated. Records sitting idle for days waiting for a human signature represent direct revenue or operational delay.

**Input:**

-   approval\_delay\_score (from Task 1.2)
-   bottleneck\_score (from Task 1.2)

**Fires when:**

approval\_delay\_score > 3.0 (average pending age exceeds 3 days) for any approval step.

**Output Label:**

APPROVAL\_BOTTLENECK

**Example that fires:**

Discount approval step:

-   47 pending records
-   Average age = 6.2 days
-   2 approvers

→ Detector fires.

**Example that does not fire:**

Refund approval step:

-   8 pending records
-   Average age = 1.1 days
-   5 approvers

→ Detector does not fire.

### **Detector 4 — Knowledge Gap**

**Business Problem:**

When agents resolve Cases without using or creating Knowledge Articles, they are solving the same problem from scratch every time. An AI agent that surfaces the right Knowledge Article at the moment a Case is opened can reduce resolution time significantly and improve consistency across the support team.

**Input:**

-   knowledge\_gap\_score (from Task 1.2), broken down by Case.Type (category)

**Fires when:**

knowledge\_gap\_score > 0.4 for any category with volume > 30 closed Cases in last 90 days.

**Output Label:**

KNOWLEDGE\_GAP

**Example that fires:**

Billing category:

-   500 closed Cases in 90 days
-   180 have KB article attached
-   knowledge\_gap\_score = 0.64

→ Detector fires.

**Example that does not fire:**

Product Setup category:

-   300 closed Cases in 90 days
-   280 have KB article attached
-   knowledge\_gap\_score = 0.07

→ Detector does not fire.

### **Detector 5 — Integration Callout Concentration**

**Business Problem:**

When many different Flows or processes all connect to the same external system independently, it means there is no coordinated integration layer. Each connection handles its own errors, retries, and data mapping differently. An agent that owns the connection to an external system and is called by other processes creates consistency and resilience.

**Input:**

-   List of Named Credentials (from Metadata API)
-   Count of distinct active Flows referencing each Named Credential (via Tooling API scan of Flow metadata)

**Fires when:**

A single Named Credential is referenced by **3 or more** distinct active Flows.

**Output Label:**

INTEGRATION\_CONCENTRATION

**Example that fires:**

The SAP-ERP Named Credential is referenced by 6 different active order management Flows.

→ Detector fires.

**Example that does not fire:**

The DocuSign Named Credential is referenced by only 1 contract signing Flow.

→ Detector does not fire.

### **Detector 6 — Permission Bottleneck**

**Business Problem:**

When a specific action — approving a contract, escalating a case, accessing sensitive records — requires a permission that only a small number of users hold, a human queue forms. Records wait. Deadlines are missed. An agent can either perform the action autonomously if the criteria are clear, or intelligently escalate to the right approver with the right context to reduce the wait.

**Input:**

-   bottleneck\_score (from Task 1.2) per approval step

**Fires when:**

bottleneck\_score > 10 for any approval step (more than 10 pending records per available approver).

**Output Label:**

PERMISSION\_BOTTLENECK

**Example that fires:**

Legal Review step:

-   34 pending records
-   2 approvers assigned
-   bottleneck\_score = 17

→ Detector fires.

**Example that does not fire:**

Manager Approval step:

-   12 pending records
-   8 managers assigned
-   bottleneck\_score = 1.5

→ Detector does not fire.

### **Detector 7 — Cross-System Echo**

**Business Problem:**

In organisations that use both Salesforce and external systems like ServiceNow or Jira, it is common for humans to manually create a Salesforce Case because they saw an incident in ServiceNow, or to update a Jira ticket because a Salesforce Case changed status. This manual synchronisation is error-prone and time-consuming. An agent can own the synchronisation logic and eliminate the human step.

**Input:**

-   echo\_score (from Task 1.2)

**Fires when:**

echo\_score > 0.15 (more than 15% of recently created Cases contain references to external system ticket IDs).

**Output Label:**

CROSS\_SYSTEM\_ECHO

**Example that fires:**

-   847 Cases created in last 90 days
-   198 contain INC- or JIRA- references
-   echo\_score = 0.23

→ Detector fires.

**Example that does not fire:**

-   340 Cases created in last 90 days
-   9 contain external references
-   echo\_score = 0.026

→ Detector does not fire.