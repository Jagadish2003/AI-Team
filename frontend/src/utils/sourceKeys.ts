/**
 * Canonical source key registry.
 *
 * Maps connector.id (stable, defined in codebase/ConnectorContext seed)
 * to the sourceSystem string used in MappingRow.sourceSystem and
 * PermissionRequirement.sourceSystem (written by backend ingestors).
 *
 * Single source of truth — all consumers (SourceIntelligencePage, NormalizationContext,
 * permissions filtering, future screens) import from here.
 *
 * Confirmed from evidence_builder.py:
 *   source_map = {"salesforce": "Salesforce", "servicenow": "ServiceNow", "jira": "Jira"}
 *
 * Sprint 5 task: create backend/app/source_keys.py with matching
 * constants so ingestors import from there — single source of truth
 * across frontend and backend.
 *
 * Adding a new connector: one entry in SOURCE_KEY_MAP. No other files change.
 * The value must exactly match what the ingestor writes into MappingRow.sourceSystem.
 */

export const SOURCE_KEY_MAP: Record<string, string> = {
  // ── Confirmed against evidence_builder.py ────────────────────────────────
  salesforce: "Salesforce",
  servicenow: "ServiceNow",

  jira: "Jira",
  confluence: "Confluence",

  // ── Sprint 6+ connectors ─────────────────────────────────────────────────
  slack: "Slack",
  databricks: "Databricks",
  microsoft_365: "Microsoft 365",
  sharepoint: "SharePoint",
  github: "GitHub",
  azure_devops: "Azure DevOps",
  gitlab: "GitLab",
  datadog: "Datadog",
  splunk: "Splunk",
  d365: "Dynamics 365",
  ncino: "nCino",
  sap: "SAP",
};

const CONNECTOR_ID_ALIASES: Record<string, keyof typeof SOURCE_KEY_MAP> = {
  // Current seed data uses a bundled delivery connector; source rows still use Jira.
  jira_confluence: "jira",
};

/** Returns the canonical sourceSystem string for a given connector.id. */
export function sourceKeyForConnector(connectorId: string): string {
  const canonicalConnectorId = CONNECTOR_ID_ALIASES[connectorId] ?? connectorId;
  return SOURCE_KEY_MAP[canonicalConnectorId] ?? connectorId;
}

/** Reverse lookup — returns the connector.id for a given sourceSystem string, or null. */
export function connectorIdForSourceKey(sourceSystem: string): string | null {
  const entry = Object.entries(SOURCE_KEY_MAP).find(
    ([, v]) => v === sourceSystem,
  );
  return entry ? entry[0] : null;
}

// ── Zero-signal sub-states ────────────────────────────────────────────────────

export const ZERO_SIGNAL_LABELS = {
  checking: { label: "Checking…", color: "muted" },
  permissionLimited: { label: "Permission-limited", color: "amber" },
  notAnalyzed: { label: "Not yet fully analyzed", color: "muted" },
  noSignals: { label: "No signals detected", color: "muted" },
  unknown: { label: "Status unknown", color: "muted" },
} as const;

export type ZeroSignalKey = keyof typeof ZERO_SIGNAL_LABELS;

/**
 * Returns the zero-signal sub-state key for a source with no mapped signals.
 * Only call this when signalCount === 0.
 */
export function zeroSignalReason(
  permState: "confirmed" | "warning" | "loading" | "unknown",
  ambiguousCount: number,
  unmappedCount: number,
): ZeroSignalKey {
  if (permState === "loading") return "checking";
  if (permState === "warning") return "permissionLimited";
  if (ambiguousCount > 0 || unmappedCount > 0) return "notAnalyzed";
  if (permState === "confirmed") return "noSignals";
  return "unknown";
}
