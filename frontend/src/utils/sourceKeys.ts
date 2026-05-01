/**
 * Canonical source key registry.
 *
 * Maps connector.id (stable, defined in codebase) to the sourceSystem string
 * used in MappingRow.sourceSystem and PermissionRequirement.sourceSystem.
 *
 * Single source of truth — all consumers (SourceIntelligencePage, NormalizationContext,
 * permissions filtering, future screens) import from here.
 * Adding a new connector: one entry in SOURCE_KEY_MAP. No other files change.
 */

export const SOURCE_KEY_MAP: Record<string, string> = {
  salesforce: 'Salesforce',
  servicenow: 'ServiceNow',
  jira: 'Jira',
  confluence: 'Confluence',
  slack: 'Slack',
  databricks: 'Databricks',
  microsoft_365: 'Microsoft 365',
  github: 'GitHub',
  azure_devops: 'Azure DevOps',
  gitlab: 'GitLab',
  datadog: 'Datadog',
  splunk: 'Splunk',
  d365: 'Dynamics 365',
  ncino: 'nCino',
};

/** Returns the canonical sourceSystem string for a given connector.id. */
export function sourceKeyForConnector(connectorId: string): string {
  return SOURCE_KEY_MAP[connectorId] ?? connectorId;
}

/** Reverse lookup — returns the connector.id for a given sourceSystem string, or null. */
export function connectorIdForSourceKey(sourceSystem: string): string | null {
  const entry = Object.entries(SOURCE_KEY_MAP).find(([, v]) => v === sourceSystem);
  return entry ? entry[0] : null;
}

// ── Zero-signal sub-states ────────────────────────────────────────────────────

export const ZERO_SIGNAL_LABELS = {
  checking:          { label: 'Checking…',              color: 'muted'  },
  permissionLimited: { label: 'Permission-limited',        color: 'amber'  },
  notAnalyzed:       { label: 'Not yet fully analyzed',    color: 'muted'  },
  noSignals:         { label: 'No signals detected',       color: 'muted'  },
  unknown:           { label: 'Status unknown',            color: 'muted'  },
} as const;

export type ZeroSignalKey = keyof typeof ZERO_SIGNAL_LABELS;

/**
 * Returns the zero-signal sub-state key for a source with no mapped signals.
 * Only call this when signalCount === 0.
 */
export function zeroSignalReason(
  permState: 'confirmed' | 'warning' | 'loading' | 'unknown',
  ambiguousCount: number,
  unmappedCount: number,
): ZeroSignalKey {
  if (permState === 'loading') return 'checking';
  if (permState === 'warning') return 'permissionLimited';
  if (ambiguousCount > 0 || unmappedCount > 0) return 'notAnalyzed';
  if (permState === 'confirmed') return 'noSignals';
  return 'unknown';
}
