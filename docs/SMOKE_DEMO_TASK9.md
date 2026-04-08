A-Task-9 smoke demo (manual)

1) Screen 6 (Analyst Review)
   - Pick an UNREVIEWED opportunity
   - Enter Override Rationale + Override Reason
   - Save Override

2) Screen 7 (Opportunity Map)
   - Select the same opportunity in the Opportunity List
   - Verify details panel shows the override rationale (not the AI rationale)

3) Evidence decision (any screen that supports it)
   - Mark one evidence item APPROVED or REJECTED
   - Verify an EVIDENCE_* audit entry appears at the top immediately after action

4) Click "Go to Review"
   - Verify Screen 6 still shows the override (round-trip preserved)

5) Refresh
   - Verify Audit Trail shows newest events at the top (newest-first).
