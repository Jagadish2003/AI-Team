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

// ── Cloud-events → Cloud Ops default ─────────────────────────────────────────
//
// Selecting a cloud event connector on Step 2 defaults the analysis pack to
// `cloud_ops` on Step 4, because the two are not independent choices: the
// discovery runner only polls the AWS/Azure event connectors when a cloud_ops
// pack is selected (`if _any_cloud_ops and "aws_events" in _systems` —
// discovery/runner.py). With the dropdown left at its old "None" default, a
// connected AWS/Azure Events source was never read and the run produced no
// cloud findings at all, silently.
//
// The default is a STARTING VALUE, not a lock: `SetupState.analysisPackTouched`
// records that the user has used the dropdown, after which their choice —
// including "None" — always wins.
//
// The canonical connector ids are `aws_events` / `azure_events` (MSP-B13); the
// `*_event_source` ids are the legacy template-suggestion ids, kept because
// they are still selectable on Step 2 (see YourSystemsPage SYSTEM_DISPLAY).
export const CLOUD_EVENT_SYSTEM_IDS: readonly string[] = [
  'aws_events',
  'azure_events',
  'aws_event_source',
  'azure_event_source',
];

/** The pack a cloud event connector implies. */
export const CLOUD_EVENTS_DEFAULT_PACK_ID = 'cloud_ops';

/**
 * Whether the Cloud Ops analysis-pack default applies to this selection —
 * true when any cloud event connector is among the selected systems.
 */
export function cloudOpsDefaultApplies(
  selectedSystemIds: string[] | null | undefined,
): boolean {
  if (!selectedSystemIds || selectedSystemIds.length === 0) return false;
  return selectedSystemIds.some(id => CLOUD_EVENT_SYSTEM_IDS.includes(id));
}

/** The offerable analysis-pack ids, for filtering a mixed pack list. */
export const ANALYSIS_PACK_IDS: ReadonlySet<string> = new Set(
  ANALYSIS_PACKS.map(pack => pack.id),
);

/**
 * The analysis pack a run will activate: the user's explicit choice when there
 * is one, else the Cloud Ops default when a cloud event connector is selected,
 * else '' (None).
 *
 * The SINGLE source of truth for the analysis slot, shared by the Step 4
 * dropdown (what the user sees) and `resolvePackIds` (what actually launches) —
 * so the menu can never show a pack the run does not run, or vice versa.
 *
 * Takes primitives rather than SetupState to keep this module dependency-free.
 */
export function resolveAnalysisPackId(
  packIds: string[] | null | undefined,
  selectedSystemIds: string[] | null | undefined,
  analysisPackTouched?: boolean,
): string {
  const chosen = (packIds ?? []).find(id => ANALYSIS_PACK_IDS.has(id));
  if (chosen) return chosen;
  // '' is a deliberate "None" once the user has used the dropdown, so the
  // default must not re-apply over it.
  if (analysisPackTouched) return '';
  return cloudOpsDefaultApplies(selectedSystemIds)
    ? CLOUD_EVENTS_DEFAULT_PACK_ID
    : '';
}

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
