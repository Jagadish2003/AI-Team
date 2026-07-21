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
