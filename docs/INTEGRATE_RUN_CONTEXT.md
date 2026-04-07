A-Task-7 Integration Instructions (Frontend)

Goal
- Add RunContext so all run-scoped screens share a single runId.
- Persist runId in URL (?runId=...) and localStorage (fallback).
- Show a clear empty state when runId is missing.

Files in this pack
- frontend/src/context/RunContext.tsx
- frontend/src/components/RunRequiredEmptyState.tsx
- frontend/src/api/runApi.ts
- frontend/src/types/discoveryRun.ts  (NEW — required for compilation)

1) Provider placement (important)
RunProvider must wrap ALL run-scoped providers and routes.

Recommended order:
<ToastProvider>
  <ConnectorProvider>
    <RunProvider>
      <SourceIntakeProvider>
        <DiscoveryRunProvider>
          <PartialResultsProvider>
            <NormalizationProvider>
              <AnalystReviewProvider>
                <Routes />
              </AnalystReviewProvider>
            </NormalizationProvider>
          </PartialResultsProvider>
        </DiscoveryRunProvider>
      </SourceIntakeProvider>
    </RunProvider>
  </ConnectorProvider>
</ToastProvider>

2) Where to call startRun()
- This call belongs in DiscoveryRunPage.tsx (or equivalent)
- After startRun(inputs) succeeds:
  - setRunId(res.runId)
  - navigate to the next screen in your flow

3) Empty runId behavior (all run-scoped screens except Discovery Run)
- If runId is null:
  - render <RunRequiredEmptyState onStart={() => navigate('/discovery-run')} />

4) URL is the share mechanism
- Copy/paste a URL with ?runId=run_001 and open in a new tab:
  - the app should load the same run automatically.
