/**
 * analysisPacks.ts — the non-Salesforce discovery packs a user can add to a run
 * from the Discovery Plan "Analysis pack" dropdown (R191-P1).
 *
 * The dropdown is SINGLE-select and defaults to "None": a run adds at most one
 * analysis pack on top of whatever the workspace has declared.
 *
 * A run's packs are the UNION of:
 *   • the SALESFORCE packs, fixed by the Integration Hub product declaration
 *     (Service Cloud / nCino / … — NOT offered here), and
 *   • the ANALYSIS pack below, chosen per-run in the Discovery Plan.
 *
 * Shared by StackBuilderPage (resolution) and DiscoveryPlanPage (the picker) to
 * avoid a circular import between the two page modules.
 */
export interface AnalysisPack {
  id: string;
  label: string;
  description: string;
}

export const ANALYSIS_PACKS: AnalysisPack[] = [
  { id: 'sqlserver_opsignal', label: 'SQL Server Operational Signals',
    description: 'DB ticket volume, SLA breach, and queue-depth signals' },
  { id: 'github_engineering', label: 'GitHub Engineering',
    description: 'PR review bottlenecks, commit concentration, stale branches' },
  { id: 'enterprise_ops', label: 'Enterprise Operations',
    description: 'Cross-system ServiceNow + Jira operations intelligence' },
  { id: 'cloud_ops', label: 'Cloud Ops',
    description: 'Managed cloud-operations discovery (NOC toil, recurrence)' },
  { id: 'security_ops', label: 'Security Ops',
    description: 'Security-operations discovery (SIR triage, remediation)' },
];

/** Human label for any pack id (analysis or Salesforce), else a title-cased id. */
export function analysisPackLabelFor(packId: string): string {
  const found = ANALYSIS_PACKS.find(p => p.id === packId);
  if (found) return found.label;
  if (packId === 'ncino') return 'nCino';
  if (packId === 'service_cloud') return 'Service Cloud';
  if (packId === 'strs_benefits') return 'STRS Benefits';
  return packId
    .split(/[_-]+/)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}
