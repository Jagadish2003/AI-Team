/**
 * T41-4 — Source Intelligence Page v1.2
 *
 * Changes from v1.1:
 *
 *   Issue 1 fix — stable connector ID as join key, not display name string.
 *     SOURCE_KEY_MAP maps connector.id (stable) to the sourceSystem string
 *     used in MappingRow.sourceSystem and PermissionRequirement.sourceSystem.
 *     deriveSourceHealth joins on connector ID, never on display name.
 *     Display name used only for rendering.
 *
 *   Issue 2 fix — "resolved" vs "reviewed" distinction.
 *     Track whether any dismiss action occurred.
 *     When all remaining cards are cleared:
 *       - If all were resolved (confirmed) → "All ambiguous fields resolved"
 *       - If any were dismissed → "All ambiguous fields reviewed"
 *     The word "resolved" appears only when the user actually confirmed
 *     a mapping. Dismiss does not imply resolution.
 *
 *   Issue 3 fix — in backend only (normalization_enrichment.py v1.2).
 *     No frontend change required for Issue 3.
 */
import React, { useMemo, useState } from "react";
import {
  Database,
  CheckCircle2,
  AlertCircle,
  ChevronRight,
  X,
  WifiOff,
  Loader2,
} from "lucide-react";
import TopNav from "../components/common/TopNav";
import { useNormalizationContext } from "../context/NormalizationContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useRunContext } from "../context/RunContext";
import { useNavigate } from "react-router-dom";
import { RunRequiredEmptyState } from "../components/common/RunRequiredEmptyState";
import MappingTable from "../components/normalization/MappingTable";
import FieldDetailsPanel from "../components/normalization/FieldDetailsPanel";
import type { MappingRow, PermissionRequirement } from "../types/normalization";
import type { Connector } from "../types/connector";

// ── Issue 1 fix: stable source key registry ───────────────────────────────────
//
// Maps connector.id (stable, defined in codebase) to the sourceSystem string
// used in MappingRow.sourceSystem and PermissionRequirement.sourceSystem.
//
// When a new connector is added, add its entry here.
// Display name for rendering is taken from connector.name — never used for joins.

const SOURCE_KEY_MAP: Record<string, string> = {
  salesforce: "Salesforce",
  servicenow: "ServiceNow",
  jira: "Jira",
  confluence: "Confluence",
  slack: "Slack",
  databricks: "Databricks",
  microsoft_365: "Microsoft 365",
  github: "GitHub",
  azure_devops: "Azure DevOps",
  gitlab: "GitLab",
  datadog: "Datadog",
  splunk: "Splunk",
  d365: "Dynamics 365",
};

function sourceKeyForConnector(connectorId: string): string {
  return SOURCE_KEY_MAP[connectorId] ?? connectorId;
}

// ── Source health derivation — Issue 1 fix ────────────────────────────────────

type PermState = "confirmed" | "warning" | "loading" | "unknown";

interface SourceHealth {
  connectorId: string;
  sourceKey: string; // canonical key used for row joins
  displayName: string; // from connector.name — display only
  signalCount: number;
  ambiguousCount: number;
  unmappedCount: number;
  confidence: "HIGH" | "MEDIUM" | "LOW" | "NONE";
  permState: PermState;
}

function deriveSourceHealth(
  connectors: Connector[],
  rows: MappingRow[],
  permissions: PermissionRequirement[],
  permissionsLoading: boolean,
  permissionsError: string | null,
): SourceHealth[] {
  const connected = connectors.filter((c) => c.status === "connected");
  const totalMapped = rows.filter((r) => r.status === "MAPPED").length;

  return connected
    .map((c) => {
      // Issue 1 fix: join on canonical sourceKey, not display name
      const sourceKey = sourceKeyForConnector(c.id);

      const sourceRows = rows.filter((r) => r.sourceSystem === sourceKey);
      const signalCount = sourceRows.filter(
        (r) => r.status === "MAPPED",
      ).length;
      const ambiguousCount = sourceRows.filter(
        (r) => r.status === "AMBIGUOUS",
      ).length;
      const unmappedCount = sourceRows.filter(
        (r) => r.status === "UNMAPPED",
      ).length;

      // Derive confidence from MAPPED rows only
      let confidence: SourceHealth["confidence"] = "NONE";
      if (signalCount > 0) {
        const mappedRows = sourceRows.filter((r) => r.status === "MAPPED");
        const highCount = mappedRows.filter(
          (r) => r.confidence === "HIGH",
        ).length;
        const medCount = mappedRows.filter(
          (r) => r.confidence === "MEDIUM",
        ).length;
        if (highCount > mappedRows.length / 2) confidence = "HIGH";
        else if (highCount + medCount >= mappedRows.length / 2)
          confidence = "MEDIUM";
        else confidence = "LOW";
      }

      // Derive permission state from real context — Issue 1 + 3 fix
      let permState: PermState;
      if (permissionsLoading) {
        permState = "loading";
      } else if (permissionsError) {
        permState = "unknown";
      } else {
        // Join permissions on sourceKey, not display name
        const sourcePerms = permissions.filter(
          (p) => p.sourceSystem === sourceKey,
        );
        if (sourcePerms.length === 0) {
          permState = "unknown";
        } else {
          const hasUnsatisfied = sourcePerms.some(
            (p) => p.required && !p.satisfied,
          );
          permState = hasUnsatisfied ? "warning" : "confirmed";
        }
      }

      return {
        connectorId: c.id,
        sourceKey,
        displayName: c.name, // display only — never used for joins
        signalCount,
        ambiguousCount,
        unmappedCount,
        confidence,
        permState,
      };
    })
    .sort((a, b) => b.signalCount - a.signalCount);
}

// ── UI helpers ────────────────────────────────────────────────────────────────

function ConfBadge({ level }: { level: SourceHealth["confidence"] }) {
  if (level === "NONE") {
    return (
      <span className="rounded-full border border-border px-2 py-0.5 text-[10px] font-semibold text-muted">
        NO SIGNALS
      </span>
    );
  }
  const cls =
    level === "HIGH"
      ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
      : level === "MEDIUM"
        ? "border-amber-500/50 bg-amber-500/10 text-amber-300"
        : "border-red-500/50 bg-red-500/10 text-red-300";
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide ${cls}`}
    >
      {level}
    </span>
  );
}

function PermIndicator({ state }: { state: PermState }) {
  switch (state) {
    case "confirmed":
      return (
        <span className="flex items-center gap-1 text-xs text-emerald-400">
          <CheckCircle2 size={12} className="shrink-0" />
          Confirmed
        </span>
      );
    case "warning":
      return (
        <span className="flex items-center gap-1 text-xs text-amber-400">
          <AlertCircle size={12} className="shrink-0" />
          Check permissions
        </span>
      );
    case "loading":
      return (
        <span className="flex items-center gap-1 text-xs text-muted">
          <Loader2 size={12} className="animate-spin shrink-0" />
          Loading…
        </span>
      );
    default:
      return (
        <span className="flex items-center gap-1 text-xs text-muted">
          <WifiOff size={12} className="shrink-0" />
          Not assessed
        </span>
      );
  }
}

function StatCard({
  label,
  value,
  sub,
  accent = false,
  warn = false,
}: {
  label: string;
  value: number | string;
  sub?: string;
  accent?: boolean;
  warn?: boolean;
}) {
  const ring = accent
    ? "border-accent/40 bg-accent/5"
    : warn
      ? "border-amber-500/40 bg-amber-500/5"
      : "border-border bg-panel";
  const val = accent ? "text-accent" : warn ? "text-amber-300" : "text-text";
  return (
    <div
      className={`rounded-xl border ${ring} px-5 py-4`}
      data-testid="stat-card"
    >
      <div className={`text-3xl font-bold ${val}`}>{value}</div>
      <div className="mt-1 text-sm font-medium text-text">{label}</div>
      {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </div>
  );
}

// ── Ambiguous decision card ───────────────────────────────────────────────────

const ENTITY_OPTIONS = [
  "Application",
  "Service",
  "Workflow",
  "DataObject",
  "User",
  "Other",
];

function AmbiguousCard({
  row,
  onConfirm,
  onDismiss,
}: {
  row: MappingRow;
  onConfirm: (id: string, entity: string) => void;
  onDismiss: (id: string) => void;
}) {
  const [selected, setSelected] = useState("");
  return (
    <div
      className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4"
      data-testid={`ambiguous-card-${row.id}`}
    >
      <div className="flex items-start justify-between gap-3 mb-2">
        <div>
          <div className="text-xs font-semibold text-text">
            {row.sourceSystem}.{row.sourceField}
          </div>
          <div className="text-xs text-muted mt-0.5">
            {row.sourceSystem} · {row.sourceType}
          </div>
        </div>
        <button
          onClick={() => onDismiss(row.id)}
          className="text-muted hover:text-text transition-colors shrink-0"
          data-testid={`ambiguous-dismiss-${row.id}`}
          aria-label="Dismiss without resolving"
        >
          <X size={14} />
        </button>
      </div>
      {row.sampleValues && row.sampleValues.length > 0 && (
        <div className="text-xs text-muted mb-3">
          Sample values: {row.sampleValues.slice(0, 3).join(", ")}
        </div>
      )}
      <div className="text-xs text-muted mb-2">What is this field about?</div>
      <div className="flex flex-wrap gap-2 mb-3">
        {ENTITY_OPTIONS.map((opt) => (
          <button
            key={opt}
            onClick={() => setSelected(opt)}
            className={[
              "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
              selected === opt
                ? "border-accent bg-accent/20 text-accent"
                : "border-border text-muted hover:border-accent/40 hover:text-text",
            ].join(" ")}
            data-testid={`ambiguous-option-${row.id}-${opt}`}
          >
            {opt}
          </button>
        ))}
      </div>
      <button
        onClick={() => selected && onConfirm(row.id, selected)}
        disabled={!selected}
        className="w-full rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent hover:bg-accent/20 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        data-testid={`ambiguous-confirm-${row.id}`}
      >
        Confirm mapping
      </button>
    </div>
  );
}

// ── Issue 2 fix: resolved vs reviewed state ───────────────────────────────────

type ReviewedState = "resolved" | "reviewed" | null;

function resolvedState(
  totalAmbiguous: number,
  confirmedCount: number,
  dismissedCount: number,
  remainingCount: number,
): ReviewedState {
  if (remainingCount > 0) return null; // still cards to action
  if (totalAmbiguous === 0) return null; // nothing was ambiguous
  if (dismissedCount === 0 && confirmedCount > 0) return "resolved"; // all confirmed
  return "reviewed"; // some dismissed
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function SourceIntelligencePage() {
  const { rows, counts, permissions, permissionsLoading, permissionsError } =
    useNormalizationContext();

  const { all: connectors } = useConnectorContext();
  const { runId } = useRunContext();
  const nav = useNavigate();

  const [showDetail, setShowDetail] = useState(false);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [confirmed, setConfirmed] = useState<Map<string, string>>(new Map());

  // Issue 1 fix: uses connector ID for joins
  const sourceHealth = useMemo(
    () =>
      deriveSourceHealth(
        connectors,
        rows,
        permissions,
        permissionsLoading,
        permissionsError,
      ),
    [connectors, rows, permissions, permissionsLoading, permissionsError],
  );

  const ambiguousRows = rows
    .filter((r) => r.status === "AMBIGUOUS")
    .filter((r) => !dismissed.has(r.id))
    .filter((r) => !confirmed.has(r.id));

  const totalAmbiguous = counts.AMBIGUOUS;
  const needsReviewCount = ambiguousRows.length;
  const highMappedCount = rows.filter(
    (r) => r.status === "MAPPED" && r.confidence === "HIGH",
  ).length;

  // Issue 2 fix: track dismiss vs confirm actions separately
  const reviewState = resolvedState(
    totalAmbiguous,
    confirmed.size,
    dismissed.size,
    needsReviewCount,
  );

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          <RunRequiredEmptyState onStart={() => nav("/discovery-run")} />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen text-text">
      <TopNav />
      <div className="w-full px-8 py-6 pb-10">
        {/* Header */}
        <div className="mb-6 flex items-start justify-between">
          <div>
            <div
              className="text-2xl font-semibold text-text"
              data-testid="page-title"
            >
              Source Intelligence
            </div>
            <div className="mt-1 text-sm text-muted">
              How AgentIQ understood your connected sources — and what needs
              your attention.
            </div>
          </div>
          <button
            onClick={() => setShowDetail((v) => !v)}
            className="flex items-center gap-1.5 text-xs text-muted hover:text-text transition-colors"
            data-testid="toggle-detail"
          >
            {showDetail ? "Hide" : "View"} field mapping detail
            <ChevronRight
              size={12}
              className={`transition-transform ${showDetail ? "rotate-90" : ""}`}
            />
          </button>
        </div>

        {/* Section 1 — Stat cards */}
        <div className="grid grid-cols-3 gap-4 mb-6" data-testid="stat-cards">
          <StatCard
            label="Connected sources"
            value={sourceHealth.length}
            sub={`${rows.filter((r) => r.status === "MAPPED").length} fields mapped total`}
            accent
          />
          <StatCard
            label="Fields mapped with HIGH confidence"
            value={highMappedCount}
            sub="Used directly in detection"
            accent
          />
          <StatCard
            label="Fields needing your review"
            value={needsReviewCount}
            sub={
              needsReviewCount === 0
                ? "Nothing pending"
                : "One-click resolution below"
            }
            warn={needsReviewCount > 0}
          />
        </div>

        {/* Section 2 — Source health */}
        <div
          className="rounded-xl border border-border bg-panel mb-6"
          data-testid="source-health-table"
        >
          <div className="px-5 py-3 border-b border-border flex items-center gap-2">
            <Database size={14} className="text-muted" />
            <span className="text-sm font-semibold text-text">
              Source Health
            </span>
            <span className="text-xs text-muted ml-1">
              — all connected sources
            </span>
          </div>

          {sourceHealth.length === 0 ? (
            <div className="px-5 py-8 text-sm text-muted text-center">
              No sources connected yet. Connect sources on the Integration Hub.
            </div>
          ) : (
            <div className="divide-y divide-border">
              {sourceHealth.map((s) => (
                <div
                  key={s.connectorId}
                  className="flex items-center gap-4 px-5 py-3"
                  data-testid={`source-row-${s.connectorId}`}
                >
                  {/* Display name — rendering only, not used for joins */}
                  <div className="w-36 shrink-0">
                    <div className="text-sm font-semibold text-text">
                      {s.displayName}
                    </div>
                    {s.signalCount === 0 && (
                      <div className="text-[10px] text-amber-400 mt-0.5">
                        No signals ingested
                      </div>
                    )}
                  </div>

                  {/* Signal bar */}
                  <div className="flex-1">
                    {s.signalCount > 0 ? (
                      <div className="flex items-center gap-2">
                        <div className="flex-1 h-1.5 rounded-full bg-border overflow-hidden">
                          <div
                            className="h-full bg-accent rounded-full"
                            style={{
                              width: `${Math.min(
                                100,
                                (s.signalCount /
                                  Math.max(
                                    1,
                                    rows.filter((r) => r.status === "MAPPED")
                                      .length,
                                  )) *
                                  100,
                              )}%`,
                            }}
                          />
                        </div>
                        <span className="text-xs text-muted w-24 shrink-0">
                          {s.signalCount} signal{s.signalCount !== 1 ? "s" : ""}
                          {s.ambiguousCount > 0 &&
                            `, ${s.ambiguousCount} ambiguous`}
                        </span>
                      </div>
                    ) : (
                      <div className="text-xs text-muted">
                        {s.ambiguousCount > 0
                          ? `${s.ambiguousCount} field${s.ambiguousCount !== 1 ? "s" : ""} found but could not be mapped`
                          : s.unmappedCount > 0
                            ? `${s.unmappedCount} field${s.unmappedCount !== 1 ? "s" : ""} could not be mapped`
                            : "Connected — awaiting discovery run"}
                      </div>
                    )}
                  </div>

                  {/* Confidence */}
                  <div className="w-24 shrink-0">
                    <ConfBadge level={s.confidence} />
                  </div>

                  {/* Permission state */}
                  <div className="w-40 shrink-0">
                    <PermIndicator state={s.permState} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Section 3 — Ambiguous review */}
        {needsReviewCount > 0 && (
          <div className="mb-6">
            <div className="text-sm font-semibold text-text mb-3 flex items-center gap-2">
              <AlertCircle size={14} className="text-amber-400" />
              {needsReviewCount} field{needsReviewCount !== 1 ? "s" : ""} need
              {needsReviewCount === 1 ? "s" : ""} your review
              <span className="text-xs text-muted font-normal">
                — one click to resolve, or dismiss to skip
              </span>
            </div>
            <div
              className="grid grid-cols-2 gap-3"
              data-testid="ambiguous-cards"
            >
              {ambiguousRows.map((row) => (
                <AmbiguousCard
                  key={row.id}
                  row={row}
                  onConfirm={(id, entity) =>
                    setConfirmed((prev) => new Map(prev).set(id, entity))
                  }
                  onDismiss={(id) =>
                    setDismissed((prev) => new Set(prev).add(id))
                  }
                />
              ))}
            </div>
          </div>
        )}

        {/* Issue 2 fix: resolved vs reviewed terminal state */}
        {reviewState === "resolved" && (
          <div
            className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 px-5 py-4 mb-6 flex items-center gap-3"
            data-testid="state-resolved"
          >
            <CheckCircle2 size={16} className="text-emerald-400 shrink-0" />
            <div>
              <div className="text-sm font-semibold text-emerald-300">
                All ambiguous fields resolved
              </div>
              <div className="text-xs text-muted mt-0.5">
                {confirmed.size} field{confirmed.size !== 1 ? "s" : ""}{" "}
                confirmed by you.
              </div>
            </div>
          </div>
        )}

        {reviewState === "reviewed" && (
          <div
            className="rounded-xl border border-border bg-panel px-5 py-4 mb-6 flex items-center gap-3"
            data-testid="state-reviewed"
          >
            <CheckCircle2 size={16} className="text-muted shrink-0" />
            <div>
              <div className="text-sm font-semibold text-text">
                All ambiguous fields reviewed
              </div>
              <div className="text-xs text-muted mt-0.5">
                {confirmed.size > 0 && `${confirmed.size} confirmed. `}
                {dismissed.size > 0 &&
                  `${dismissed.size} dismissed without resolution.`}
              </div>
            </div>
          </div>
        )}

        {/* Developer detail panel */}
        {showDetail && (
          <div
            className="rounded-xl border border-border bg-panel p-4 mb-4"
            data-testid="detail-panel"
          >
            <div className="text-sm font-semibold text-text mb-4 flex items-center justify-between">
              Field Mapping Detail
              <button
                onClick={() => setShowDetail(false)}
                className="text-muted hover:text-text transition-colors"
              >
                <X size={14} />
              </button>
            </div>
            <div className="flex gap-4" style={{ height: "600px" }}>
              <div className="flex-1 overflow-hidden">
                <MappingTable />
              </div>
              <div className="w-72 shrink-0 overflow-hidden">
                <FieldDetailsPanel />
              </div>
            </div>
          </div>
        )}

        {/* Counts footer */}
        <div className="flex items-center gap-8 rounded-xl border border-border bg-panel px-6 py-3 text-sm">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-sm bg-accent" />
            <span className="font-semibold text-text">Mapped</span>
            <span className="font-bold text-text">{counts.MAPPED}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="font-semibold text-muted">{counts.UNMAPPED}</span>
            <span className="text-muted">Unmapped</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="font-semibold text-muted">{counts.AMBIGUOUS}</span>
            <span className="text-muted">Ambiguous</span>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <span className="text-muted">Total fields</span>
            <span className="font-bold text-text">
              {counts.MAPPED + counts.AMBIGUOUS + counts.UNMAPPED}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
