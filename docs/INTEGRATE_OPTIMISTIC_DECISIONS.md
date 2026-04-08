A-Task-8 v1.1 Integration Instructions

Goal
- Decisions and overrides update the UI immediately (optimistic update).
- API persistence happens in the background.
- No list refetch after every write.
- On API failure, UI rolls back and the context returns an error.

Integrate (required)
1) Analyst Review (S6 / S7)
- Replace your existing file with:
  frontend/src/context/AnalystReviewContext.tsx
- Add:
  frontend/src/api/analystReviewApi.ts
  frontend/src/types/common.ts
  frontend/src/types/analystReview.ts (updated imports)
- Ensure RunProvider (A-Task-7) wraps AnalystReviewProvider so runId is available.

Integrate (optional)
2) Evidence decisions (S4)
- This pack includes a clean Evidence provider without any task suffix naming:
  frontend/src/context/EvidenceContext.tsx
  frontend/src/api/evidenceApi.ts
  frontend/src/types/evidence.ts (updated imports)
- If you already have an Evidence/PartialResults context, port the same optimistic pattern:
  - update local list immediately
  - POST /api/runs/{runId}/evidence/{evidenceId}/decision
  - rollback on failure

Expected behavior
- Approve/Reject changes badges immediately.
- Saving Override changes the displayed rationale immediately.
- A refresh shows persisted state (backend is run-scoped by runId).
- Audit entries created optimistically use the same timestamp style as the server: "DD Mon YYYY, HH:MM".
