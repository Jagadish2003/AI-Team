# MSP-B5 runbook-match lifecycle

The B6 documented/repeated/manual finding consumes one of five runbook states:

| State | Meaning in the composite | User-facing label |
|---|---|---|
| `observed` | An incident explicitly cited the resolved runbook. The documented leg is satisfied and receives the strongest treatment. | Observed runbook match |
| `proposed` | Retrieval found a candidate. It may contribute provisionally, but is not fact. | Proposed match, pending confirmation |
| `confirmed` | An analyst accepted a proposal. The documented leg is satisfied and receives the strongest treatment. | Confirmed runbook match |
| `absent` | Matching completed and no active match remains, including a dismissed proposal. | No runbook match |
| `unavailable` | Matching could not run. This is not treated as a documentation gap. | Runbook matching unavailable |

Analysts use `POST /api/runbook-matches/{recurrence_id}/decision` with `accept`,
`dismiss`, or `defer`. The route requires an authenticated Analyst or Owner and
derives the organization from the signed request context. `accept` changes the
active match to confirmed; `dismiss` removes it from active consideration;
`defer` keeps it proposed. Repeating the current action is a no-op.

Current state is stored in `runbook_matches`. Every real transition is appended
to `runbook_match_decision_history`; earlier decisions are never overwritten.
Accept and dismiss also append privacy-safe labels to `runbook_match_feedback`.
Those feedback records include stable match provenance and confidence only—no
incident notes or runbook text.

`runbook_composite.present_runbook_match` is the presentation source of truth.
Finding, executive-report, and demonstration shaping all use it, preventing a
proposal from being displayed as observed or confirmed.

## Documentation-gap finding

`runbook_documentation_gap.evaluate_documentation_gap` emits the inverse finding
only for a high-volume recurrence after both checks complete successfully:

1. explicit runbook citations resolve to no observed match; and
2. runbook-scoped retrieval and deterministic scoring produce no proposal.

A citation-library or retrieval failure returns `unavailable` with no finding. A
low-volume recurrence returns `not_eligible`. The emitted finding carries the
recurrence's incident evidence pointers and a structured record of both search
outcomes, so an analyst can verify why the gap was raised.

Sensitivity is configuration, not detector code:

| Setting | Default | Purpose |
|---|---:|---|
| `MSP_B5_DOCUMENTATION_GAP_FLOOR` | `5` | Minimum recurrence count required before missing documentation is raised as an organizational risk. |
| `MSP_B5_DOCUMENTATION_GAP_CONFIDENCE_CAP` | `0.65` | Maximum confidence assigned to the absence-based finding. |

Titles and explanations use only the structured incident category and CI class.
They name the repeated loop, recurrence volume, and missing documentation; they do
not copy assignees, resolvers, or any other individual identity.
