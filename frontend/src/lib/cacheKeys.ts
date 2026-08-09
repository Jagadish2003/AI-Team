/**
 * Centralized cache-key builders for the shared data cache (see dataCache.tsx).
 *
 * Keys are hierarchical, "/"-delimited strings so that prefix invalidation is
 * meaningful — e.g. `invalidate(cacheKeys.runScope(runId))` refreshes that run's
 * opportunities + audit + roadmap + blueprint together, and
 * `invalidate(cacheKeys.connectorsScope)` refreshes the tile list + the product
 * declaration + every credential-status card at once.
 *
 * Use these builders rather than stringly-typing keys at call sites, so a rename
 * is a single edit and prefixes stay consistent.
 */
export const cacheKeys = {
  // ── Connectors (Integration Hub) ─────────────────────────────────────────
  /** Prefix covering all connector-scoped keys below. */
  connectorsScope: 'connectors',
  connectors: 'connectors',
  // Integration Hub scope pickers (which channels / projects / spaces / sites /
  // repos / schemas AgentIQ may read). All under the `connectors` prefix, so a
  // connect/disconnect refreshes them along with the tile list — and because each
  // picker renders its skeleton only while it holds NO data, that refresh is
  // silent: the current options stay on screen until the new ones arrive.
  connectorProducts: 'connectors/salesforce/products',
  connectorSpaces: 'connectors/confluence/spaces',
  connectorSites: 'connectors/sharepoint/sites',
  connectorRepos: 'connectors/github/repos',
  connectorSlackChannels: 'connectors/slack/channels',
  connectorTeamsChannels: 'connectors/teams/channels',
  connectorJiraProjects: 'connectors/jira/projects',
  connectorSqlServerScope: 'connectors/sqlserver/scope',
  connectorOracleScope: 'connectors/oracle/scope',
  connectorPostgresScope: 'connectors/postgresql/scope',
  connectorCredentialStatus: (id: string) => `connectors/${id}/credential-status`,
  connectorJwtStatus: (id: string) => `connectors/${id}/jwt-credentials`,
  connectorTokenStatus: (id: string) => `connectors/${id}/token-status`,
  workspaceCatalog: 'integration-hub/workspace-catalog',
  networkProfile: 'network-profile',
  /** Stack Builder's industry + template registry (one key — one fetch pair). */
  stackBuilderRegistry: 'stack-builder/registry',

  // ── Runs (run-scoped resources) ──────────────────────────────────────────
  runs: 'runs',
  /** Prefix covering everything under a single run. */
  runScope: (runId: string) => `runs/${runId}`,
  run: (runId: string) => `runs/${runId}`,
  runOpportunities: (runId: string) => `runs/${runId}/opportunities`,
  runAudit: (runId: string) => `runs/${runId}/audit`,
  runRoadmap: (runId: string) => `runs/${runId}/roadmap`,
  runEvidence: (runId: string) => `runs/${runId}/evidence`,
  runEntities: (runId: string) => `runs/${runId}/entities`,
  runNormalization: (runId: string) => `runs/${runId}/normalization`,
  runStatus: (runId: string) => `runs/${runId}/status`,
  runBlueprint: (runId: string, oppId: string) => `runs/${runId}/blueprint/${oppId}`,
  runEnrichment: (runId: string) => `runs/${runId}/enrichment`,
  runOppEnrichment: (runId: string, oppId: string) =>
    `runs/${runId}/opportunities/${oppId}/enrichment`,
  runTraceGraph: (runId: string, oppId: string) =>
    `runs/${runId}/opportunities/${oppId}/trace-graph`,
  runExecutiveReport: (runId: string) => `runs/${runId}/executive-report`,
  outcomesScope: 'outcomes',
  outcomePortfolio: 'outcomes/portfolio',
  opportunityOutcome: (opportunityIdentity: string) =>
    `outcomes/opportunity/${opportunityIdentity}`,
  opportunityLifecycle: (opportunityIdentity: string) =>
    `outcomes/lifecycle/${opportunityIdentity}`,
  learningScope: 'learning',
  learningSignals: 'learning/signals',

  // ── Run Health dashboard ─────────────────────────────────────────────────
  /** Prefix covering every run-health panel — invalidate to refresh them all. */
  runHealthScope: 'run-health',
  runHealthConnectors: 'run-health/connectors',
  runHealthRuns: 'run-health/runs',
  runHealthContent: 'run-health/content',
  runHealthPacks: 'run-health/packs',
  runHealthAttention: 'run-health/attention',

  // ── Workspace / account ──────────────────────────────────────────────────
  license: 'license',
  workspaceMembers: 'workspace/members',
  uploads: 'uploads',
  permissions: 'permissions',
} as const;
