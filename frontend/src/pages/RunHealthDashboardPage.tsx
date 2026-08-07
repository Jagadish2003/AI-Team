import { useCallback, useEffect, useMemo, useState } from "react";
import PackCertificationBadge from "../components/common/PackCertificationBadge";
import {
  PackDeprecationBadge,
  PackDeprecationDetail,
} from "../components/common/PackDeprecationNotice";
import { Link, useSearchParams } from "react-router-dom";
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  Boxes,
  CheckCircle2,
  Clock3,
  Database,
  Loader2,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
} from "lucide-react";

import {
  fetchAttentionHealth,
  fetchConnectorHealth,
  fetchContentHealth,
  fetchPackHealth,
  fetchRunHealth,
} from "../api/runHealthApi";
import { resetIngestionCheckpoint } from "../api/ingestionApi";
import { ApiError } from "../lib/apiClient";
import { useResource } from "../lib/dataCache";
import { cacheKeys } from "../lib/cacheKeys";
import ConfirmDialog from "../components/common/ConfirmDialog";
import PageShell from "../components/common/PageShell";
import { Skeleton } from "../components/common/Skeleton";
import { useAuth } from "../context/AuthContext";
import type {
  AttentionHealthResponse,
  ConnectorHealthItem,
  ConnectorHealthResponse,
  ContentHealthResponse,
  ExcludedPackItem,
  HealthPanelId,
  PackHealthItem,
  PackHealthResponse,
  RunHealthItem,
  RunHealthResponse,
} from "../types/runHealth";

type ResourceState<T> =
  | { status: "loading"; data: null; error: null }
  | { status: "success"; data: T; error: null }
  | { status: "error"; data: null; error: string };

const INITIAL_STATE: ResourceState<never> = {
  status: "loading",
  data: null,
  error: null,
};

// Panels rendered on the dashboard grid, and therefore the only ?panel= targets a
// deep link can scroll to. "content" and "packs" are intentionally absent: their
// panels are hidden, so scrolling to them would be a no-op on a missing element.
const PANEL_IDS: HealthPanelId[] = ["connectors", "runs"];

// Panels with more than this many cards get an internal scrollbar so the panel
// stays compact instead of growing with the list. The height fits ~3 cards.
const CARD_LIST_SCROLL_THRESHOLD = 3;

/**
 * Classes for a card list that scrolls internally once it exceeds the threshold.
 * The extra right padding keeps the scrollbar from overlapping the card borders.
 */
function cardListClasses(count: number): string {
  const base = "space-y-3";
  return count > CARD_LIST_SCROLL_THRESHOLD
    ? `${base} max-h-[30rem] overflow-y-auto pr-1`
    : base;
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}

/**
 * A health panel's data, on the SHARED cache.
 *
 * The cache lives at the app root, so navigating away and back re-renders each
 * panel from its cached value with NO refetch and no skeleton — previously every
 * visit refetched all five panels from zero. Freshness is handled for us:
 * background revalidation (tab focus / the org change stream) updates panels
 * silently, with the current data still on screen.
 *
 * The returned shape is deliberately unchanged (the loading/success/error union
 * plus `refresh`), so every panel below renders exactly as before.
 */
function useHealthResource<T>(cacheKey: string, loader: () => Promise<T>, label: string) {
  const { data, error, refetch } = useResource<T>(cacheKey, loader);


  const state: ResourceState<T> = useMemo(() => {
    // Data wins over a later error: if a BACKGROUND revalidation fails we keep
    // showing the last good panel rather than replacing it with an error.
    if (data !== undefined) return { status: "success", data, error: null };
    if (error) {
      return {
        status: "error",
        data: null,
        error: errorMessage(error, `${label} could not be loaded.`),
      };
    }
    return INITIAL_STATE as ResourceState<T>;
  }, [data, error, label]);

  return { state, refresh: refetch };
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "Not available";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

/**
 * Why a connector has no checkpoint age, stated instead of a bare "Not available".
 *
 * A checkpoint age can be genuinely absent for reasons an operator should be able
 * to tell apart, and none of them can be shown as a number without inventing data:
 *  - the connector is not connected, so nothing has ingested;
 *  - it has ingested but does not keep a resumable cursor (it is not on the
 *    change-based ingestion path, e.g. Salesforce/Jira today);
 *  - its checkpoint was reset, so it will re-read from the start on the next run.
 * Reporting the reason keeps the panel honest while making the blank cell useful.
 */
function checkpointAbsenceReason(item: ConnectorHealthItem): string {
  if (["disconnected", "needs_auth", "refresh_failed", "error"].includes(item.connection_state)) {
    return "Not connected";
  }
  if (item.last_successful_ingestion) return "No resumable cursor";
  return "Awaiting first ingestion";
}

function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "Not available";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} sec`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} hr`;
  return `${Math.round(seconds / 86400)} days`;
}

function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "Not available";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} sec`;
  return `${(ms / 60000).toFixed(1)} min`;
}

function labelize(value: string): string {
  return value.replaceAll("_", " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function detectorLabel(value: string): string {
  return labelize(value.split(".").at(-1) ?? value);
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseJson(value: string): unknown | null {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function looksLikeDate(value: string): boolean {
  if (!/[0-9]{4}[-/][0-9]{1,2}[-/][0-9]{1,2}/.test(value)) return false;
  return !Number.isNaN(new Date(value).getTime());
}

function checkpointReadTime(values: unknown[]): string | null {
  const usable = values
    .map((value) => (typeof value === "string" || typeof value === "number" ? String(value).trim() : ""))
    .filter(Boolean);
  if (usable.length === 0) return null;

  const dated = usable
    .filter(looksLikeDate)
    .sort((a, b) => new Date(b).getTime() - new Date(a).getTime());
  if (dated.length > 0) return formatDate(dated[0]);

  return null;
}

/**
 * The ONE place a raw checkpoint position is decoded.
 *
 * A checkpoint value is opaque to the frontend by design — it is a plain ISO
 * string for a single-cursor connector and a nested per-scope map for the cloud
 * ones. Decoding it in exactly one function means a serialisation change (a key
 * rename, a new nesting level) has a single site to update; when two callers
 * each parsed it independently, one could be updated and the other would
 * silently render nothing.
 */
function parseCheckpointPosition(raw: string | null | undefined): unknown | null {
  if (!raw || !raw.trim()) return null;
  // A bare ISO timestamp is not JSON, so a parse failure is expected, not an
  // error: fall back to the raw string and let the caller read a time off it.
  return parseJson(raw) ?? raw;
}

function checkpointProgressRows(item: ConnectorHealthItem): Array<{ label: string; value: string; readTime?: string }> {
  const source = parseCheckpointPosition(item.checkpoint_position);
  if (source === null) return [];

  if (isPlainRecord(source)) {
    return Object.entries(source).flatMap(([key, value]) => {
      const label = labelize(key);
      if (isPlainRecord(value)) {
        const readTime = checkpointReadTime(Object.values(value));
        return readTime ? [{ label, value: `Continues after ${readTime}`, readTime }] : [];
      }
      if (Array.isArray(value)) {
        const readTime = checkpointReadTime(value);
        return readTime ? [{ label, value: `Continues after ${readTime}`, readTime }] : [];
      }
      const readTime = checkpointReadTime([value]);
      return readTime ? [{ label, value: `Continues after ${readTime}`, readTime }] : [];
    });
  }

  const readTime = checkpointReadTime([source]);
  return readTime ? [{ label: "Data checkpoint", value: `Continues after ${readTime}`, readTime }] : [];
}

/**
 * The supporting-details view of the same rows, minus the readTime the progress
 * view uses for ordering. Derived from `checkpointProgressRows` rather than
 * re-parsing the position, so the two views cannot disagree about what a
 * checkpoint says.
 */
function checkpointSupportingRows(item: ConnectorHealthItem): Array<{ label: string; value: string }> {
  return checkpointProgressRows(item).map(({ label, value }) => ({ label, value }));
}

function sentenceCase(value: string): string {
  const trimmed = compactText(value).replace(/[.!?]+$/, "");
  if (!trimmed) return "";
  return `${trimmed.charAt(0).toUpperCase()}${trimmed.slice(1)}.`;
}

function messageFromErrorJson(value: unknown): string | null {
  const first = Array.isArray(value) ? value[0] : value;
  if (!isPlainRecord(first)) return null;
  const message = first.message ?? first.error ?? first.detail ?? first.error_description;
  return typeof message === "string" && message.trim() ? compactText(message) : null;
}

function extractErrorJson(text: string): string | null {
  const withoutQuery = text.replace(/\s+Query:\s*[\s\S]*$/i, "");
  const jsonMatch = withoutQuery.match(/(\[[\s\S]*\]|\{[\s\S]*\})/);
  if (!jsonMatch) return null;
  const parsed = parseJson(jsonMatch[1]);
  return parsed === null ? null : messageFromErrorJson(parsed);
}

function customerStageIssue(stage: string | null | undefined, reason: string | null | undefined) {
  const stageLabel = labelize(stage || "Stage");
  const raw = compactText(reason || "");
  const jsonMessage = raw ? extractErrorJson(raw) : null;
  const searchable = `${stageLabel} ${raw}`;

  if (/salesforce/i.test(searchable) && /(invalid_session_id|session expired|http 401|unauthori[sz]ed)/i.test(searchable)) {
    return {
      stage: stageLabel,
      issue: "Salesforce session expired or is no longer valid.",
      action: "Reconnect Salesforce, then rerun discovery.",
    };
  }
  if (/(http 401|unauthori[sz]ed|authentication|needs_auth|invalid session)/i.test(searchable)) {
    return {
      stage: stageLabel,
      issue: `${stageLabel} authentication failed.`,
      action: `Reconnect ${stageLabel}, then rerun discovery.`,
    };
  }
  if (/(http 403|forbidden|permission)/i.test(searchable)) {
    return {
      stage: stageLabel,
      issue: `${stageLabel} does not have permission to read the requested data.`,
      action: `Update ${stageLabel} permissions, then rerun discovery.`,
    };
  }
  if (/(http 429|rate.?limit|too many requests)/i.test(searchable)) {
    return {
      stage: stageLabel,
      issue: `${stageLabel} rate limit was reached.`,
      action: "Wait for the source limit to reset, then rerun discovery.",
    };
  }
  if (/(timed out|timeout)/i.test(searchable)) {
    return {
      stage: stageLabel,
      issue: sentenceCase(jsonMessage ?? raw),
      action: "Rerun discovery after the source is responding normally.",
    };
  }

  const withoutQuery = raw.replace(/\s+Query:\s*[\s\S]*$/i, "");
  const withoutJson = withoutQuery.replace(/(\[[\s\S]*\]|\{[\s\S]*\})/, jsonMessage ?? "The source returned an error response");
  return {
    stage: stageLabel,
    issue: sentenceCase(jsonMessage ?? (withoutJson || "This stage reported an issue")),
    action: null,
  };
}

function stageLevelTone(level: string | null | undefined): "warn" | "bad" | "info" | "neutral" {
  const normalized = String(level || "").toUpperCase();
  if (normalized === "ERROR" || normalized === "AI_ERROR") return "bad";
  if (normalized === "WARNING") return "warn";
  if (normalized === "INFO") return "info";
  return "neutral";
}

function stageOutcomeNeedsAttention(level: string | null | undefined): boolean {
  return ["WARNING", "ERROR", "AI_ERROR"].includes(String(level || "").toUpperCase());
}

function StatusPill({ label, tone }: { label: string; tone: "good" | "warn" | "bad" | "info" | "neutral" }) {
  const styles = {
    good: "border-emerald-500/30 bg-emerald-500/15 text-emerald-300",
    warn: "border-amber-500/30 bg-amber-500/15 text-amber-300",
    bad: "border-red-500/30 bg-red-500/15 text-red-300",
    info: "border-blue-500/30 bg-blue-500/15 text-blue-300",
    neutral: "border-border bg-bg/40 text-muted",
  };
  return (
    <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-semibold ${styles[tone]}`}>
      {label}
    </span>
  );
}

function Metric({ label, value, detail }: { label: string; value: string | number; detail?: string }) {
  return (
    <div className="rounded-xl border border-border bg-bg/40 p-3">
      <div className="text-xs font-medium uppercase tracking-wide text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-text">{value}</div>
      {detail ? <div className="mt-1 text-xs text-muted">{detail}</div> : null}
    </div>
  );
}

function PanelStateIndicator({ state }: { state: string }) {
  if (state === "loading") return <StatusPill label="Checking" tone="info" />;
  if (state === "error") return <StatusPill label="Unavailable" tone="bad" />;
  if (state === "empty") return <StatusPill label="No data" tone="neutral" />;
  if (state === "degraded") return <StatusPill label="Needs attention" tone="warn" />;
  if (state === "partial") return <StatusPill label="Partial data" tone="warn" />;
  if (state === "in-progress") return <StatusPill label="In progress" tone="info" />;
  if (state === "healthy") return <StatusPill label="Healthy" tone="good" />;
  return null;
}

function PanelFrame({
  id,
  title,
  description,
  icon,
  state,
  highlighted,
  children,
}: {
  id: string;
  title: string;
  description: string;
  icon: React.ReactNode;
  state: string;
  highlighted: boolean;
  children: React.ReactNode;
}) {
  return (
    <section
      id={id}
      data-testid={id}
      data-state={state}
      className={`min-w-0 overflow-hidden scroll-mt-24 rounded-2xl border bg-panel p-5 shadow-sm transition ${
        highlighted ? "border-accent ring-4 ring-accent/30" : "border-border"
      }`}
    >
      <div className="mb-4 flex items-start gap-3">
        <div className="shrink-0 rounded-xl bg-bg/40 p-2 text-muted">{icon}</div>
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-text">{title}</h2>
          <p className="mt-1 text-sm text-muted">{description}</p>
        </div>
        <div className="shrink-0">
          <PanelStateIndicator state={state} />
        </div>
      </div>
      {children}
    </section>
  );
}

// Skeleton rows shaped like the panel's real list content, so a panel fills its
// reserved space when the data lands instead of swapping a spinner for a block
// of rows (which shifted everything below it).
function PanelLoading({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-busy="true"
      aria-label={`Loading ${label.toLowerCase()}`}
      className="rounded-xl border border-border bg-bg/40 p-5"
    >
      <div className="space-y-3">
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-12 w-full rounded-lg" />
        ))}
      </div>
    </div>
  );
}

function PanelError({ label, message, onRetry }: { label: string; message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="rounded-xl border border-red-500/30 bg-red-500/10 p-4 text-red-200">
      <div className="flex gap-3">
        <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
        <div className="min-w-0">
          <div className="font-semibold">{label} unavailable</div>
          <p className="mt-1 break-words text-sm text-red-200/90">{message}</p>
          <p className="mt-1 text-xs text-red-200/70">No healthy or zero-value state is inferred while this read is unavailable.</p>
          <button
            type="button"
            onClick={onRetry}
            className="mt-3 rounded-lg border border-red-500/40 bg-panel px-3 py-1.5 text-sm font-semibold text-text hover:bg-red-500/10"
          >
            Retry {label.toLowerCase()}
          </button>
        </div>
      </div>
    </div>
  );
}

function EmptyState({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="rounded-xl border border-dashed border-border bg-bg/40 p-5 text-center">
      <div className="font-semibold text-text">{title}</div>
      <p className="mt-1 text-sm text-muted">{detail}</p>
    </div>
  );
}

function connectorTone(item: ConnectorHealthItem): "good" | "warn" | "bad" | "neutral" {
  if (["error", "disconnected", "needs_auth", "refresh_failed"].includes(item.connection_state)) return "bad";
  if (item.last_error || !["connected", "live"].includes(item.connection_state)) return "warn";
  return "good";
}

// Display order within the Connectors panel: healthy connectors first, then ones
// with a warning, then disconnected/failed ones last. Sinking the broken ones to
// the bottom is deliberate — the panel scrolls after three cards, so the states
// an operator acts on sit together at the end of a predictable list rather than
// being scattered through it by the backend's arbitrary order.
const CONNECTOR_TONE_ORDER: Record<"good" | "warn" | "neutral" | "bad", number> = {
  good: 0,
  warn: 1,
  neutral: 2,
  bad: 3,
};

function sortConnectorsByHealth(items: ConnectorHealthItem[]): ConnectorHealthItem[] {
  // Copy first: the array belongs to the shared data cache and must not be sorted
  // in place. Ties keep the backend's order (stable sort) and break on name so the
  // list never reshuffles between two renders of the same data.
  return [...items].sort((a, b) => {
    const delta = CONNECTOR_TONE_ORDER[connectorTone(a)] - CONNECTOR_TONE_ORDER[connectorTone(b)];
    if (delta !== 0) return delta;
    return a.name.localeCompare(b.name);
  });
}

// ── In-context actions (R18-C2 T5) ────────────────────────────────────────────
// The dashboard surfaces the EXISTING reconnect and checkpoint-reset operations
// where they are relevant. No new operational controls are introduced.

// Connection states that mean authentication needs attention → reconnect. This
// mirrors the backend's _AUTH_ACTION_STATES (health_aggregation.py) so the
// dashboard offers the reconnect action for exactly the states the health
// service flags. "disconnected" is included because a disconnected source is
// also re-established via the Integration Hub connect flow.
const AUTH_ACTION_STATES = new Set(["needs_auth", "refresh_failed", "error", "disconnected"]);

// Mirrors backend STALLED_CHECKPOINT_SECONDS (health_aggregation.py, 24h): a
// checkpoint that exists but has not advanced for at least this long is stalled.
const STALLED_CHECKPOINT_SECONDS = 24 * 60 * 60;

// Integration Hub groups connectors by category and supports a ?category=
// deep-link (IntegrationHubPage). Map a connector id to its category so the
// reconnect link lands on the group containing that connector.
const CONNECTOR_CATEGORY: Record<string, string> = {
  salesforce: "primary_platforms",
  sap: "primary_platforms",
  oracle_ebs: "primary_platforms",
  workday: "primary_platforms",
  dynamics365: "primary_platforms",
  jira: "operational_systems",
  jira_confluence: "operational_systems",
  servicenow: "operational_systems",
  azure_devops: "operational_systems",
  linear: "operational_systems",
  zendesk: "operational_systems",
  slack: "comms_knowledge",
  teams: "comms_knowledge",
  m365: "comms_knowledge",
  confluence: "comms_knowledge",
  sharepoint: "comms_knowledge",
  notion: "comms_knowledge",
  github: "data_engineering",
  gitlab: "data_engineering",
  bitbucket: "data_engineering",
  azure_repos: "data_engineering",
  postgresql: "data_engineering",
  sql_server: "data_engineering",
  oracle_db: "data_engineering",
  databricks: "data_engineering",
  snowflake: "data_engineering",
  dbt: "data_engineering",
};

function reconnectHref(connectorId: string): string {
  const category = CONNECTOR_CATEGORY[connectorId];
  return category ? `/integration-hub?category=${category}` : "/integration-hub";
}

function authNeedsAttention(item: ConnectorHealthItem): boolean {
  return AUTH_ACTION_STATES.has(item.connection_state);
}

function checkpointExists(item: ConnectorHealthItem): boolean {
  // A checkpoint exists when the backend reported a position or capture time.
  return item.checkpoint_position != null || item.checkpoint_captured_at != null;
}

function checkpointStalled(item: ConnectorHealthItem): boolean {
  return (
    checkpointExists(item) &&
    item.checkpoint_age_seconds != null &&
    item.checkpoint_age_seconds >= STALLED_CHECKPOINT_SECONDS
  );
}

type ResetOutcome =
  | { kind: "cleared" }
  | { kind: "nothing" }
  | { kind: "error"; message: string };

function ConnectorActions({
  item,
  role,
  outcome,
  onOutcome,
  onResetSuccess,
}: {
  item: ConnectorHealthItem;
  role: "owner" | "analyst";
  // Reset outcome is held by the panel (keyed by connector) so the "existed and
  // cleared" message survives the post-success refresh, which briefly re-renders
  // the connector list.
  outcome: ResetOutcome | null;
  onOutcome: (connectorId: string, outcome: ResetOutcome | null) => void;
  // Called after a successful reset so the panel can refresh and show the
  // updated state. Not called on failure — known health must not change.
  onResetSuccess: () => void;
}) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [resetting, setResetting] = useState(false);

  const showReconnect = authNeedsAttention(item);
  // Reset is an owner-only backend operation, surfaced only when a checkpoint
  // exists and is stalled. Analysts inspect but cannot reset.
  const showReset = role === "owner" && checkpointStalled(item);

  if (!showReconnect && !showReset) return null;

  async function runReset() {
    setResetting(true);
    onOutcome(item.connector_id, null);
    try {
      const result = await resetIngestionCheckpoint(item.connector_id);
      setConfirmOpen(false);
      onOutcome(item.connector_id, { kind: result.cleared ? "cleared" : "nothing" });
      // Refresh the panel so it reflects the reset (post-success only).
      onResetSuccess();
    } catch (error) {
      // Failure surfaces a clear error and leaves the previously known health
      // untouched — never a false "resolved". The dialog stays open so the
      // owner can retry or cancel.
      const message =
        error instanceof ApiError && error.message.trim()
          ? error.message
          : "Checkpoint reset failed. The connector's health is unchanged.";
      onOutcome(item.connector_id, { kind: "error", message });
    } finally {
      setResetting(false);
    }
  }

  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2">
        {showReconnect ? (
          <Link
            to={reconnectHref(item.connector_id)}
            data-testid={`reconnect-${item.connector_id}`}
            className="inline-flex items-center gap-1.5 rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-sm font-semibold text-accent hover:bg-accent/20"
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
            Reconnect in Integration Hub
          </Link>
        ) : null}
        {showReset ? (
          <button
            type="button"
            data-testid={`reset-checkpoint-${item.connector_id}`}
            onClick={() => {
              onOutcome(item.connector_id, null);
              setConfirmOpen(true);
            }}
            className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-sm font-semibold text-amber-200 hover:bg-amber-500/20"
          >
            <RotateCcw className="h-4 w-4" aria-hidden="true" />
            Reset checkpoint
          </button>
        ) : null}
      </div>

      {outcome && outcome.kind !== "error" ? (
        <div
          role="status"
          data-testid={`reset-result-${item.connector_id}`}
          className="mt-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-300"
        >
          {outcome.kind === "cleared"
            ? "Checkpoint cleared. The next ingestion for this connector will re-read from the start."
            : "No checkpoint existed to clear — this connector was already set to re-read from the start."}
        </div>
      ) : null}

      {outcome && outcome.kind === "error" ? (
        <div
          role="alert"
          data-testid={`reset-error-${item.connector_id}`}
          className="mt-2 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300"
        >
          {outcome.message}
        </div>
      ) : null}

      {showReset ? (
        <ConfirmDialog
          open={confirmOpen}
          title={`Reset ${item.name} checkpoint?`}
          confirmLabel="Reset checkpoint"
          busyLabel="Resetting…"
          busy={resetting}
          message={
            <span>
              This clears the ingestion checkpoint for <span className="font-semibold text-text">{item.name}</span>.
              The next discovery run will perform a <span className="font-semibold text-text">full re-read</span> of
              this source from the beginning instead of an incremental update, which can take longer. The result
              will confirm whether a checkpoint actually existed and was cleared. This cannot be undone.
            </span>
          }
          onConfirm={() => {
            void runReset();
          }}
          onCancel={() => {
            if (!resetting) setConfirmOpen(false);
          }}
        />
      ) : null}
    </div>
  );
}

function ConnectorsPanel({
  resource,
  retry,
  refresh,
  role,
  highlighted,
}: {
  resource: ResourceState<ConnectorHealthResponse>;
  retry: () => void;
  refresh: () => void;
  role: "owner" | "analyst";
  highlighted: boolean;
}) {
  // Reset outcomes are held here (keyed by connector) so a success message
  // survives the post-reset refresh, which momentarily re-renders the list.
  const [resetOutcomes, setResetOutcomes] = useState<Record<string, ResetOutcome | null>>({});
  const setOutcome = useCallback((connectorId: string, outcome: ResetOutcome | null) => {
    setResetOutcomes((prev) => ({ ...prev, [connectorId]: outcome }));
  }, []);

  // Healthy first, disconnected last. Sorting here (not in the panel's state
  // calculation below) keeps the derived state independent of display order.
  const orderedConnectors = useMemo(
    () => (resource.status === "success" ? sortConnectorsByHealth(resource.data.connectors) : []),
    [resource],
  );

  let state: string = resource.status;
  if (resource.status === "success") {
    const hasIssues = resource.data.connectors.some((item) => connectorTone(item) !== "good");
    const hasMissing = resource.data.connectors.some(
      (item) =>
        !item.last_successful_ingestion ||
        item.checkpoint_age_seconds === null ||
        item.checkpoint_age_seconds === undefined ||
        !item.auth_mode,
    );
    state = resource.data.connectors.length === 0 ? "empty" : hasIssues ? "degraded" : hasMissing ? "partial" : "healthy";
  }

  return (
    <PanelFrame
      id="panel-connectors"
      title="Connectors"
      description="See whether each connected system is authenticated, ingesting data, and advancing its checkpoint."
      icon={<Activity className="h-5 w-5" aria-hidden="true" />}
      state={state}
      highlighted={highlighted}
    >
      {resource.status === "loading" ? <PanelLoading label="Connector health" /> : null}
      {resource.status === "error" ? (
        <PanelError label="Connector health" message={resource.error} onRetry={retry} />
      ) : null}
      {resource.status === "success" && resource.data.connectors.length === 0 ? (
        <EmptyState title="No connectors configured" detail="Connector health will appear after a system is connected for this organization." />
      ) : null}
      {resource.status === "success" && resource.data.connectors.length > 0 ? (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Metric label="Configured" value={resource.data.connectors.length} />
            <Metric label="Data flowing" value={resource.data.connectors.filter((item) => connectorTone(item) === "good").length} />
            <Metric label="Need attention" value={resource.data.connectors.filter((item) => connectorTone(item) !== "good").length} />
          </div>
          <div className={cardListClasses(orderedConnectors.length)}>
            {orderedConnectors.map((item) => (
              <article key={item.connector_id} className="rounded-xl border border-border p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="font-semibold text-text">{item.name}</h3>
                    <p className="text-sm text-muted">{item.tier ? `${labelize(item.tier)} connector` : labelize(item.connector_id)}</p>
                  </div>
                  <StatusPill label={labelize(item.connection_state)} tone={connectorTone(item)} />
                </div>
                <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
                  <div><dt className="text-muted">Last ingestion</dt><dd className="mt-0.5 font-medium text-text">{formatDate(item.last_successful_ingestion)}</dd></div>
                  <div>
                    <dt className="text-muted">Checkpoint age</dt>
                    <dd className="mt-0.5 font-medium text-text">
                      {item.checkpoint_age_seconds === null || item.checkpoint_age_seconds === undefined ? (
                        // No age exists — say WHY rather than showing a bare blank or
                        // a fabricated zero.
                        <span className="text-muted">{checkpointAbsenceReason(item)}</span>
                      ) : (
                        <>
                          {formatAge(item.checkpoint_age_seconds)}
                          {/* A multi-stream connector's age is that of its newest
                              stream, so say how many streams it stands for rather
                              than implying a single cursor. */}
                          {item.checkpoint_streams && item.checkpoint_streams > 1 ? (
                            <span className="ml-1 font-normal text-muted">
                              (newest of {item.checkpoint_streams} streams)
                            </span>
                          ) : null}
                        </>
                      )}
                    </dd>
                  </div>
                  <div><dt className="text-muted">Authentication</dt><dd className="mt-0.5 font-medium text-text">{item.auth_mode ? labelize(item.auth_mode) : "Not available"}</dd></div>
                </dl>
                {item.last_error ? (
                  <div className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
                    <span className="font-semibold">Latest issue:</span> {item.last_error}
                  </div>
                ) : null}
                {checkpointSupportingRows(item).length > 0 ? (
                  <details className="mt-3 text-sm">
                    <summary className="cursor-pointer font-medium text-accent">Supporting details</summary>
                    <dl className="mt-2 grid gap-2 rounded-lg bg-bg/40 p-3 sm:grid-cols-2">
                      {checkpointSupportingRows(item).map((row) => (
                        <div key={`${item.connector_id}-${row.label}`}>
                          <dt className="text-muted">{row.label}</dt>
                          <dd className="font-medium text-text">{row.value}</dd>
                        </div>
                      ))}
                    </dl>
                  </details>
                ) : null}
                <ConnectorActions
                  item={item}
                  role={role}
                  outcome={resetOutcomes[item.connector_id] ?? null}
                  onOutcome={setOutcome}
                  onResetSuccess={refresh}
                />
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </PanelFrame>
  );
}

function runTone(status: string | null | undefined): "good" | "warn" | "bad" | "info" | "neutral" {
  if (["healthy", "complete", "completed", "done"].includes(status ?? "")) return "good";
  if (status === "degraded") return "warn";
  if (status === "failed") return "bad";
  if (["running", "created", "queued", "pending"].includes(status ?? "")) return "info";
  return "neutral";
}

function RunDetails({ run }: { run: RunHealthItem }) {
  const stageIssues = (run.degraded_stages ?? []).map((stage) => customerStageIssue(stage.stage, stage.reason));
  const additionalIssues = (run.stage_outcomes ?? [])
    .filter((stage) => stageOutcomeNeedsAttention(stage.level))
    .filter((stage) =>
      !stageIssues.some(
        (issue) => issue.stage === labelize(stage.stage || "Stage") && issue.issue === customerStageIssue(stage.stage, stage.message).issue,
      ),
    );
  const hasIssues = stageIssues.length > 0 || additionalIssues.length > 0;

  return (
    <details className="mt-3 text-sm">
      <summary className="cursor-pointer font-medium text-accent">Stage and detector details</summary>
      <div className="mt-2 space-y-3 rounded-lg bg-bg/40 p-3">
        {stageIssues.length > 0 ? (
          <div>
            <div className="font-semibold text-text">Stages needing attention</div>
            <dl className="mt-2 space-y-2">
              {stageIssues.map((issue, index) => (
                <div key={`${issue.stage}-${index}`} className="rounded-lg border border-border/70 bg-panel/50 p-3">
                  <dt className="font-medium text-text">{issue.stage}</dt>
                  <dd className="mt-1 min-w-0 space-y-1 text-muted">
                    <div className="break-words leading-relaxed">{issue.issue}</div>
                    {issue.action ? <div className="break-words leading-relaxed text-text">{issue.action}</div> : null}
                  </dd>
                </div>
              ))}
            </dl>
          </div>
        ) : null}
        {additionalIssues.length > 0 ? (
          <div>
            <div className="font-semibold text-text">Additional stage issues</div>
            <div className="mt-2 flex flex-wrap gap-2">
              {additionalIssues.map((stage, index) => (
                <StatusPill
                  key={`${stage.stage}-${index}`}
                  label={`${labelize(stage.stage ?? "Unknown stage")}: ${labelize(stage.level ?? "Needs review")}`}
                  tone={stageLevelTone(stage.level)}
                />
              ))}
            </div>
          </div>
        ) : null}
        {!hasIssues ? (
          <div className="text-muted">
            No stage issues were recorded.
          </div>
        ) : null}
        {(run.detectors_evaluated !== null && run.detectors_evaluated !== undefined) || (run.detectors_fired !== null && run.detectors_fired !== undefined) ? (
          <div className="grid gap-2 border-t border-border pt-3 text-sm sm:grid-cols-2">
            {run.detectors_evaluated !== null && run.detectors_evaluated !== undefined ? (
              <div>
                <div className="text-muted">Detectors checked</div>
                <div className="font-medium text-text">{run.detectors_evaluated}</div>
              </div>
            ) : null}
            {run.detectors_fired !== null && run.detectors_fired !== undefined ? (
              <div>
                <div className="text-muted">Findings raised</div>
                <div className="font-medium text-text">{run.detectors_fired}</div>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </details>
  );
}

function RunsPanel({
  resource,
  retry,
}: {
  resource: ResourceState<RunHealthResponse>;
  retry: () => void;
}) {
  const hasIncompleteSummary = resource.status === "success" && resource.data.runs.some(
    (run) =>
      !["running", "created", "queued", "pending"].includes(run.health_status) &&
      (
        run.duration_seconds === null ||
        run.duration_seconds === undefined ||
        run.system_count === null ||
        run.system_count === undefined ||
        run.detectors_evaluated === null ||
        run.detectors_evaluated === undefined ||
        run.detectors_fired === null ||
        run.detectors_fired === undefined ||
        run.opportunities === null ||
        run.opportunities === undefined
      ),
  );
  const state = resource.status === "success"
    ? resource.data.runs.length === 0
      ? "empty"
      : resource.data.runs.some((run) => ["failed", "degraded"].includes(run.health_status))
        ? "degraded"
        : resource.data.runs.some((run) => ["running", "created", "queued", "pending"].includes(run.health_status))
          ? "in-progress"
          : resource.data.runs.some((run) => run.health_status !== "healthy") || hasIncompleteSummary
            ? "partial"
            : "healthy"
    : resource.status;
  return (
    <PanelFrame
      id="panel-runs"
      title="Runs"
      description="Understand which recent discovery runs succeeded, degraded, failed, or are still in progress."
      icon={<Clock3 className="h-5 w-5" aria-hidden="true" />}
      state={state}
      highlighted={false}
    >
      {resource.status === "loading" ? <PanelLoading label="Run health" /> : null}
      {resource.status === "error" ? <PanelError label="Run health" message={resource.error} onRetry={retry} /> : null}
      {resource.status === "success" && resource.data.runs.length === 0 ? (
        <EmptyState title="No discovery runs yet" detail="Run outcomes and stage health will appear after the first discovery run starts." />
      ) : null}
      {resource.status === "success" && resource.data.runs.length > 0 ? (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="Successful" value={resource.data.runs.filter((run) => run.health_status === "healthy").length} />
            <Metric label="Degraded" value={resource.data.runs.filter((run) => run.health_status === "degraded").length} />
            <Metric label="Failed" value={resource.data.runs.filter((run) => run.health_status === "failed").length} />
            <Metric label="In progress" value={resource.data.runs.filter((run) => ["running", "created", "queued", "pending"].includes(run.health_status)).length} />
          </div>
          <div className={cardListClasses(resource.data.runs.length)}>
            {resource.data.runs.map((run) => (
              <article key={run.run_id} className="rounded-xl border border-border p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    {/* Full run id, never truncated: it is the identifier an operator
                        copies to correlate with logs and run-scoped API calls, so a
                        shortened prefix is not actionable. */}
                    <h3 className="break-all font-semibold text-text">Run {run.run_id}</h3>
                    <p className="text-sm text-muted">{formatDate(run.started_at)}</p>
                  </div>
                  <StatusPill label={labelize(run.health_status)} tone={runTone(run.health_status)} />
                </div>
                <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-5">
                  <div><dt className="text-muted">Systems</dt><dd className="font-semibold text-text">{run.system_count ?? "Not available"}</dd></div>
                  <div><dt className="text-muted">Detectors</dt><dd className="font-semibold text-text">{run.detectors_evaluated ?? "Not available"} evaluated</dd></div>
                  <div><dt className="text-muted">Findings</dt><dd className="font-semibold text-text">{run.detectors_fired ?? "Not available"} fired</dd></div>
                  <div><dt className="text-muted">Opportunities</dt><dd className="font-semibold text-text">{run.opportunities ?? "Not available"}</dd></div>
                  <div><dt className="text-muted">Duration</dt><dd className="font-semibold text-text">{run.duration_seconds === null || run.duration_seconds === undefined ? "Not available" : formatDuration(run.duration_seconds * 1000)}</dd></div>
                </dl>
                {run.pack_id ? (
                  <p className="mt-3 text-sm text-muted">
                    Pack: <span className="font-medium text-text">{run.pack_id}</span>
                  </p>
                ) : null}
                <RunDetails run={run} />
              </article>
            ))}
          </div>
        </div>
      ) : null}
    </PanelFrame>
  );
}

function ContentPanel({
  resource,
  retry,
  highlighted,
}: {
  resource: ResourceState<ContentHealthResponse>;
  retry: () => void;
  highlighted: boolean;
}) {
  const hasContentSignals = resource.status === "success" && (
    resource.data.chunks_total > 0 ||
    resource.data.indexed_by_source.length > 0 ||
    resource.data.pending_embeddings > 0 ||
    resource.data.stale_chunks > 0 ||
    resource.data.pending_change_events > 0 ||
    resource.data.failed_refreshes > 0 ||
    resource.data.redaction_count > 0 ||
    resource.data.skipped.length > 0 ||
    (resource.data.backfill.awaiting_backfill ?? 0) > 0
  );
  const state = resource.status === "success"
    ? !hasContentSignals
      ? "empty"
      : resource.data.pending_embeddings > 0 || resource.data.stale_chunks > 0 || resource.data.failed_refreshes > 0
        ? "degraded"
        : resource.data.chunks_total > 0 && resource.data.indexed_by_source.length === 0
          ? "partial"
          : "healthy"
    : resource.status;
  const progress = resource.status === "success" && resource.data.backfill.progress !== null && resource.data.backfill.progress !== undefined
    ? Math.max(0, Math.min(100, Math.round(resource.data.backfill.progress * 100)))
    : null;

  return (
    <PanelFrame
      id="panel-content"
      title="Content and Freshness"
      description="Track indexed volume, embedding work, stale content, refresh progress, skips, and redactions."
      icon={<Database className="h-5 w-5" aria-hidden="true" />}
      state={state}
      highlighted={highlighted}
    >
      {resource.status === "loading" ? <PanelLoading label="Content health" /> : null}
      {resource.status === "error" ? <PanelError label="Content health" message={resource.error} onRetry={retry} /> : null}
      {resource.status === "success" && !hasContentSignals ? (
        <EmptyState title="No indexed content yet" detail="Content and freshness metrics will appear when connector ingestion begins." />
      ) : null}
      {resource.status === "success" && hasContentSignals ? (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Metric label="Indexed chunks" value={resource.data.chunks_embedded} detail={`${resource.data.chunks_total} discovered`} />
            <Metric label="Embedding backlog" value={resource.data.pending_embeddings} />
            <Metric label="Stale content" value={resource.data.stale_chunks} />
            <Metric label="Failed refreshes" value={resource.data.failed_refreshes} />
            <Metric label="Redactions" value={resource.data.redaction_count} />
            <Metric label="Pending changes" value={resource.data.pending_change_events} />
          </div>
          <div className="rounded-xl border border-border p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="font-semibold text-text">Refresh progress</h3>
              <StatusPill label={resource.data.backfill.complete ? "Complete" : resource.data.backfill.awaiting_backfill ? "In progress" : "No active refresh"} tone={resource.data.backfill.complete ? "good" : resource.data.backfill.awaiting_backfill ? "info" : "neutral"} />
            </div>
            {progress !== null ? (
              <div className="mt-3">
                <div className="h-2 overflow-hidden rounded-full bg-border" aria-label={`Refresh ${progress}% complete`}>
                  <div className="h-full rounded-full bg-accent" style={{ width: `${progress}%` }} />
                </div>
                <p className="mt-1 text-xs text-muted">{progress}% complete</p>
              </div>
            ) : (
              <p className="mt-2 text-sm text-muted">No active refresh progress is available.</p>
            )}
          </div>
          <details className="min-w-0 overflow-hidden rounded-xl border border-border p-4" open={resource.data.skipped.length > 0}>
            <summary className="cursor-pointer font-semibold text-text">Supporting source details</summary>
            <div className="mt-3 max-w-full overflow-x-auto">
              <table className="w-full table-fixed text-left text-sm">
                <thead className="text-xs uppercase text-muted"><tr><th className="w-[46%] break-words pb-2 pr-3">Source</th><th className="w-[28%] break-words pb-2 pr-3">Discovered</th><th className="w-[26%] break-words pb-2">Embedded</th></tr></thead>
                <tbody className="divide-y divide-border">
                  {resource.data.indexed_by_source.map((source) => (
                    <tr key={source.source_system}><td className="break-words py-2 pr-3 font-medium text-text">{labelize(source.source_system)}</td><td className="break-words py-2 pr-3 tabular-nums">{source.chunk_count}</td><td className="break-words py-2 tabular-nums">{source.embedded_count}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            {resource.data.skipped.length > 0 ? (
              <div className="mt-4">
                <div className="text-sm font-semibold text-text">Skipped items</div>
                <ul className="mt-2 space-y-1 text-sm text-muted">
                  {resource.data.skipped.map((item) => <li key={item.reason} className="break-words">{labelize(item.reason)}: {item.count}</li>)}
                </ul>
              </div>
            ) : null}
          </details>
        </div>
      ) : null}
    </PanelFrame>
  );
}

/**
 * 2.0-C1 T5 (AT-830): the pack's lifecycle position, as one short phrase.
 *
 * Deliberately separates the two orthogonal facts a reader needs:
 *  - `state`   — is the pack still running at all? (AT-827)
 *  - `version` — which version produced this run, and was that a rollback? (AT-828)
 *
 * "Rolled back" is NOT a state — a pack can be rolled back AND disabled at once, so
 * the two are shown as separate pills rather than collapsed into one word.
 */
export function packLifecycleLabel(pack: PackHealthItem): {
  stateLabel: string;
  stateTone: "good" | "warn" | "info" | "neutral";
  versionLabel: string;
  versionTone: "warn" | "info";
  rolledBack: boolean;
} {
  const disabled = pack.pack_state === "disabled";
  // `rolled_back` is authoritative; fall back to the presence of a pin so an older
  // response that carries only `pinned_version` still reads correctly.
  const rolledBack = pack.rolled_back === true || Boolean(pack.pinned_version);
  return {
    stateLabel: disabled ? "Disabled" : "Active",
    // Disabled is a deliberate customer choice, not a fault — informational, never
    // an error tone. It still needs to stand out from Active.
    stateTone: disabled ? "warn" : "good",
    versionLabel: pack.pack_version
      ? rolledBack
        ? `Rolled back to ${pack.pack_version}`
        : `Version ${pack.pack_version}`
      : "Version unavailable",
    versionTone: pack.pack_version ? "info" : "warn",
    rolledBack,
  };
}

/**
 * 2.0-C4 T4 (AT-845): why a selected pack did not run, in two words.
 *
 * A pack the organisation disabled and a pack whose deprecation grace period ended
 * both end up excluded, but the remedies are opposites — re-enable it, versus
 * migrate off it because it can never come back. Labelling both "disabled" would
 * send an operator to a button that cannot help them.
 *
 * An unrecognised reason falls back to the neutral wording rather than being
 * rendered raw, so a future backend reason never leaks a snake_case code into the UI.
 */
export function excludedPackLabel(reason?: string | null): string {
  if (reason === "deprecation_grace_expired") return "grace period ended";
  return "disabled";
}

/**
 * The sentence explaining an excluded set, which may mix both reasons at once.
 * Each group states its own remedy; neither is described in the other's terms.
 */
export function excludedPacksDetail(excluded: ExcludedPackItem[]): string {
  const retired = excluded
    .filter((item) => item.reason === "deprecation_grace_expired")
    .map((item) => item.packId);
  const disabled = excluded
    .filter((item) => item.reason !== "deprecation_grace_expired")
    .map((item) => item.packId);

  const parts: string[] = [];
  if (disabled.length > 0) {
    parts.push(
      `${disabled.join(", ")} ${disabled.length === 1 ? "is" : "are"} disabled for this organisation, so ${disabled.length === 1 ? "it" : "they"} did not execute. Re-enable ${disabled.length === 1 ? "it" : "a pack"} to include ${disabled.length === 1 ? "it" : "them"} in future runs.`,
    );
  }
  if (retired.length > 0) {
    parts.push(
      `${retired.join(", ")} reached the end of ${retired.length === 1 ? "its" : "their"} deprecation grace period and ${retired.length === 1 ? "was" : "were"} retired, so ${retired.length === 1 ? "it" : "they"} did not execute. Migrate to the replacement pack — re-enabling will not bring ${retired.length === 1 ? "it" : "them"} back.`,
    );
  }
  return parts.join(" ");
}

function PacksPanel({
  resource,
  retry,
  highlighted,
}: {
  resource: ResourceState<PackHealthResponse>;
  retry: () => void;
  highlighted: boolean;
}) {
  const excluded = resource.status === "success" ? resource.data.excluded_packs ?? [] : [];
  const state = resource.status === "success"
    ? resource.data.packs.length === 0
      ? "empty"
      : resource.data.packs.some(
          (pack) =>
            !pack.pack_version ||
            pack.detector_count <= 0 ||
            !pack.detectors ||
            pack.detectors.length !== pack.detector_count,
        )
        ? "partial"
        : "healthy"
    : resource.status;
  return (
    <PanelFrame
      id="panel-packs"
      title="Packs"
      description="Confirm which analysis packs and detectors executed, the exact pack version used, and whether a pack is disabled or rolled back."
      icon={<Boxes className="h-5 w-5" aria-hidden="true" />}
      state={state}
      highlighted={highlighted}
    >
      {resource.status === "loading" ? <PanelLoading label="Pack health" /> : null}
      {resource.status === "error" ? <PanelError label="Pack health" message={resource.error} onRetry={retry} /> : null}
      {resource.status === "success" && resource.data.packs.length === 0 ? (
        excluded.length > 0 ? (
          // A disabled pack is the REASON there is nothing to show — say so instead
          // of the generic "no runs yet" message, which would be misleading.
          <EmptyState
            title="No pack executed for this run"
            detail={excludedPacksDetail(excluded)}
          />
        ) : (
          <EmptyState title="No pack executions yet" detail="Pack versions and detector execution will appear after a discovery run uses them." />
        )
      ) : null}
      {resource.status === "success" && resource.data.packs.length > 0 ? (
        <div className="space-y-3">
          {resource.data.packs.map((pack) => {
            const lifecycle = packLifecycleLabel(pack);
            return (
            <article key={`${resource.data.run_id}-${pack.pack_id}`} data-testid={`pack-row-${pack.pack_id}`} className="rounded-xl border border-border p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                {/* Merge note: `dev` widened this header — `min-w-0` + `break-all`
                    and the FULL run id rather than an 8-char slice, so a run id is
                    correlatable and long ids cannot overflow the card. Kept. */}
                <div className="min-w-0"><h3 className="font-semibold text-text">{pack.pack_name ?? pack.pack_id}</h3><p className="break-all text-sm text-muted">Run {resource.data.run_id ?? "Not available"} · Executed {formatDate(pack.executed_at)}</p></div>
                {/* `dev` had a single "Version …" pill here. This pill ROW supersedes
                    it: `lifecycle.versionLabel` renders the same text (and "Rolled
                    back to X" when pinned), beside the three other orthogonal facts. */}
                <div className="flex flex-wrap items-center gap-2">
                  <span data-testid={`pack-state-${pack.pack_id}`}><StatusPill label={lifecycle.stateLabel} tone={lifecycle.stateTone} /></span>
                  <span data-testid={`pack-version-${pack.pack_id}`}><StatusPill label={lifecycle.versionLabel} tone={lifecycle.versionTone} /></span>
                  {/* 2.0-C2 T3 (AT-833 / AC2): the level of the pack this run
                      attributed its findings to. A third orthogonal fact beside
                      state and version — assurance, not health — so it is its own
                      pill and uses the shared badge component every other surface
                      uses. */}
                  <span data-testid={`pack-certification-${pack.pack_id}`}>
                    <PackCertificationBadge
                      level={pack.certification_level}
                      label={pack.certification_label}
                      reviewDue={pack.certification_review_due}
                      reviewDueDetail={pack.certification_review_due_detail}
                      testId={`pack-certification-badge-${pack.pack_id}`}
                    />
                  </span>
                  {/* 2.0-C4 T2 (AT-843 / AC1): a fourth orthogonal fact — is this
                      pack being retired? Its own pill for the same reason
                      certification has one: a pack can be active, current, certified
                      AND deprecated all at once. */}
                  <span data-testid={`pack-deprecation-${pack.pack_id}`}>
                    <PackDeprecationBadge
                      phase={pack.deprecation_phase}
                      label={pack.deprecation_label}
                      notice={pack.deprecation_notice}
                      testId={`pack-deprecation-badge-${pack.pack_id}`}
                    />
                  </span>
                </div>
              </div>
              {pack.deprecation_phase ? (
                <div className="mt-3" data-testid={`pack-deprecation-note-${pack.pack_id}`}>
                  <PackDeprecationDetail
                    phase={pack.deprecation_phase}
                    notice={pack.deprecation_notice}
                    graceEndsOn={pack.deprecation_ends_on}
                    replacementLabel={pack.deprecation_replacement_label}
                    daysRemaining={pack.deprecation_days_remaining}
                    testId={`pack-deprecation-detail-${pack.pack_id}`}
                  />
                </div>
              ) : null}
              {pack.pack_state === "disabled" ? (
                <p data-testid={`pack-disabled-note-${pack.pack_id}`} className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-300">
                  This pack is disabled and will not run again. Everything it produced
                  below is kept exactly as it executed.
                </p>
              ) : null}
              {lifecycle.rolledBack ? (
                <p data-testid={`pack-rollback-note-${pack.pack_id}`} className="mt-3 rounded-lg border border-blue-500/30 bg-blue-500/10 px-3 py-2 text-xs leading-relaxed text-blue-300">
                  This run was pinned to version {pack.pinned_version ?? pack.pack_version} — a deliberate rollback,
                  not the version currently shipped.
                </p>
              ) : null}
              <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
                <div><dt className="text-muted">Detectors attempted</dt><dd className="text-xl font-semibold text-text">{pack.detector_count}</dd></div>
                <div><dt className="text-muted">Pack identifier</dt><dd className="text-lg font-semibold text-text">{pack.pack_id}</dd></div>
                <div><dt className="text-muted">Version executed</dt><dd className="text-lg font-semibold text-text">{pack.pack_version ?? "Not available"}</dd></div>
                <div><dt className="text-muted">Pack state</dt><dd className="text-lg font-semibold text-text">{lifecycle.stateLabel}</dd></div>
                {pack.certification_label ? <div><dt className="text-muted">Certification</dt><dd className="text-lg font-semibold text-text">{pack.certification_label}{pack.certification_review_due ? " (review due)" : ""}</dd>{pack.certification_review_due ? <p data-testid={`pack-certification-review-due-${pack.pack_id}`} className="mt-1 text-xs leading-relaxed text-amber-300">{pack.certification_review_due_detail ?? "This certification is due for review."}</p> : pack.certification_review_due_on ? <p className="mt-1 text-xs text-muted">Next review due {pack.certification_review_due_on}</p> : null}</div> : null}
                {pack.evaluated_count !== null && pack.evaluated_count !== undefined ? <div><dt className="text-muted">Evaluated successfully</dt><dd className="text-xl font-semibold text-text">{pack.evaluated_count}</dd></div> : null}
                {pack.not_evaluated_count !== null && pack.not_evaluated_count !== undefined ? <div><dt className="text-muted">Not evaluated</dt><dd className="text-xl font-semibold text-text">{pack.not_evaluated_count}</dd></div> : null}
              </dl>
              {pack.detectors && pack.detectors.length > 0 ? <details className="mt-3 text-sm"><summary className="cursor-pointer font-medium text-accent">Detector list</summary><div className="mt-2 flex flex-wrap gap-2">{pack.detectors.map((detector) => <StatusPill key={detector} label={detectorLabel(detector)} tone="neutral" />)}</div></details> : null}
            </article>
            );
          })}
        </div>
      ) : null}
      {resource.status === "success" && resource.data.packs.length > 0 && excluded.length > 0 ? (
        <section data-testid="packs-excluded" className="mt-4 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4">
          <h3 className="text-sm font-semibold text-amber-300">Selected but not run</h3>
          <p className="mt-1 text-xs text-amber-300/90">
            {excludedPacksDetail(excluded)} Findings they produced in earlier runs are
            unaffected.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            {excluded.map((item) => (
              <span key={item.packId} data-testid={`pack-excluded-${item.packId}`}>
                <StatusPill
                  label={`${item.packId} · ${excludedPackLabel(item.reason)}`}
                  tone="warn"
                />
              </span>
            ))}
          </div>
        </section>
      ) : null}
    </PanelFrame>
  );
}

// ContentPanel and PacksPanel are currently hidden from the dashboard grid but are
// kept intact so re-enabling either is a one-line change in the grid below. This
// reference keeps them from reading as dead code to a reader or a linter.
export const HIDDEN_PANELS = { ContentPanel, PacksPanel };

function AttentionStrip({ resource, retry }: { resource: ResourceState<AttentionHealthResponse>; retry: () => void }) {
  const state = resource.status === "success" ? (resource.data.items.length === 0 ? "empty" : "attention") : resource.status;
  return (
    <section data-testid="attention-strip" data-state={state} className="rounded-2xl border border-amber-500/30 bg-amber-500/10 p-5 shadow-sm">
      <div className="mb-4 flex items-start gap-3">
        <div className="rounded-xl bg-amber-500/15 p-2 text-amber-300"><ShieldAlert className="h-5 w-5" aria-hidden="true" /></div>
        <div><h2 className="text-lg font-semibold text-text">Attention Strip</h2><p className="mt-1 text-sm text-muted">Prioritized conditions that may need investigation, linked directly to supporting details.</p></div>
      </div>
      {resource.status === "loading" ? <PanelLoading label="Attention items" /> : null}
      {resource.status === "error" ? <PanelError label="Attention strip" message={resource.error} onRetry={retry} /> : null}
      {resource.status === "success" && resource.data.items.length === 0 ? (
        <div className="flex items-start gap-3 rounded-xl border border-emerald-500/30 bg-panel p-4 text-emerald-300"><CheckCircle2 className="mt-0.5 h-5 w-5" aria-hidden="true" /><div><div className="font-semibold">No current attention items</div><p className="mt-1 text-sm">The health service did not report any prioritized conditions for this organization.</p></div></div>
      ) : null}
      {resource.status === "success" && resource.data.items.length > 0 ? (
        <ol className="space-y-3">
          {resource.data.items.map((item) => (
            <li key={item.id} className="rounded-xl border border-amber-500/30 bg-panel p-4">
              <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><StatusPill label={item.severity.toUpperCase()} tone={item.severity === "critical" ? "bad" : item.severity === "high" || item.severity === "medium" ? "warn" : "info"} /><h3 className="font-semibold text-text">{item.title}</h3></div><p className="mt-2 text-sm text-muted">{item.explanation}</p><p className="mt-2 text-xs text-muted">Detected {formatDate(item.timestamp)}</p></div><Link to={item.href} className="shrink-0 rounded-lg border border-amber-500/30 bg-amber-500/15 px-3 py-2 text-sm font-semibold text-amber-300 hover:brightness-110">View {item.panel} details</Link></div>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
}

function overallSummary(
  connectors: ResourceState<ConnectorHealthResponse>,
  runs: ResourceState<RunHealthResponse>,
  content: ResourceState<ContentHealthResponse>,
  packs: ResourceState<PackHealthResponse>,
  attention: ResourceState<AttentionHealthResponse>,
) {
  const resources = [connectors, runs, content, packs, attention];
  if (resources.some((resource) => resource.status === "error")) {
    return { label: "Health partially unavailable", detail: "One or more health reads failed. Available sections remain visible; missing data is not treated as healthy.", tone: "bad" as const, icon: AlertCircle };
  }
  if (resources.some((resource) => resource.status === "loading")) {
    return { label: "Checking tenant health", detail: "Loading independent health signals for this organization.", tone: "info" as const, icon: Loader2 };
  }
  const connectorData = connectors.status === "success" ? connectors.data : null;
  const runData = runs.status === "success" ? runs.data : null;
  const contentData = content.status === "success" ? content.data : null;
  const packData = packs.status === "success" ? packs.data : null;
  const attentionData = attention.status === "success" ? attention.data : null;
  if (attentionData && attentionData.items.length > 0) {
    return { label: "Attention required", detail: `${attentionData.items.length} prioritized condition${attentionData.items.length === 1 ? "" : "s"} need review.`, tone: "warn" as const, icon: AlertTriangle };
  }
  if ((connectorData?.connectors.some((item) => connectorTone(item) !== "good") ?? false) || (runData?.runs.some((run) => ["failed", "degraded"].includes(run.health_status)) ?? false) || (contentData?.pending_embeddings ?? 0) > 0 || (contentData?.stale_chunks ?? 0) > 0 || (contentData?.failed_refreshes ?? 0) > 0) {
    return { label: "Tenant health is degraded", detail: "At least one operational signal needs review in the sections below.", tone: "warn" as const, icon: AlertTriangle };
  }
  if (runData?.runs.some((run) => ["running", "created", "queued", "pending"].includes(run.health_status))) {
    return { label: "Discovery is in progress", detail: "A discovery run is active. Current completed-run and ingestion health remains visible below.", tone: "info" as const, icon: Loader2 };
  }
  const hasData = (connectorData?.connectors.length ?? 0) > 0 || (runData?.runs.length ?? 0) > 0 || (contentData?.chunks_total ?? 0) > 0 || (packData?.packs.length ?? 0) > 0;
  if (!hasData) {
    return { label: "Waiting for health data", detail: "This organization has no connector, run, content, or pack activity to summarize yet.", tone: "neutral" as const, icon: Clock3 };
  }
  return { label: "Tenant health is healthy", detail: "Available connector, run, content, and pack signals show no current issues.", tone: "good" as const, icon: CheckCircle2 };
}

function ViewerDenied() {
  return (
    <PageShell title="Run Health" description="Tenant-level operational health for connectors, discovery runs, content, and analysis packs.">
      <section role="alert" className="mx-auto max-w-2xl rounded-2xl border border-amber-500/30 bg-amber-500/10 p-6 text-amber-200">
        <div className="flex gap-3"><ShieldAlert className="mt-0.5 h-6 w-6 shrink-0" aria-hidden="true" /><div><h2 className="text-lg font-semibold">Run Health access is restricted</h2><p className="mt-2 text-sm">Owners and Analysts can view organization health. Viewers do not have permission to access this dashboard.</p></div></div>
      </section>
    </PageShell>
  );
}

function RunHealthDashboard({ role }: { role: "owner" | "analyst" }) {
  const [searchParams] = useSearchParams();
  const selectedPanel = searchParams.get("panel") as HealthPanelId | null;
  const connectors = useHealthResource(cacheKeys.runHealthConnectors, fetchConnectorHealth, "Connector health");
  const runs = useHealthResource(cacheKeys.runHealthRuns, fetchRunHealth, "Run health");
  const content = useHealthResource(cacheKeys.runHealthContent, fetchContentHealth, "Content health");
  const packs = useHealthResource(cacheKeys.runHealthPacks, fetchPackHealth, "Pack health");
  const attention = useHealthResource(cacheKeys.runHealthAttention, fetchAttentionHealth, "Attention items");

  useEffect(() => {
    if (!selectedPanel || !PANEL_IDS.includes(selectedPanel)) return;
    document.getElementById(`panel-${selectedPanel}`)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [selectedPanel, connectors.state.status, runs.state.status, content.state.status, packs.state.status]);

  const summary = useMemo(
    () => overallSummary(connectors.state, runs.state, content.state, packs.state, attention.state),
    [connectors.state, runs.state, content.state, packs.state, attention.state],
  );
  const SummaryIcon = summary.icon;

  // Refresh state. The cache keeps the previous object reference when a refetch
  // returns structurally identical data (see dataCache.payloadsEqual), which is
  // correct — it avoids pointless re-renders — but it means an unchanged refresh
  // produced NO visible feedback at all and read as a dead button. So the click is
  // tracked explicitly: a busy state while the reads are in flight, and a
  // completion time afterwards, whether or not the data turned out to differ.
  const [refreshing, setRefreshing] = useState(false);
  const [refreshedAt, setRefreshedAt] = useState<string | null>(null);

  const refreshAll = useCallback(async () => {
    setRefreshing(true);
    try {
      // All five in parallel; each read reports its own failure through its panel,
      // so one failing read must not prevent the others from refreshing.
      await Promise.all([
        connectors.refresh(),
        runs.refresh(),
        content.refresh(),
        packs.refresh(),
        attention.refresh(),
      ]);
      setRefreshedAt(new Date().toISOString());
    } finally {
      setRefreshing(false);
    }
  }, [connectors, runs, content, packs, attention]);

  // Prefer the time of an explicit refresh; fall back to the backend's own
  // generated_at so the timestamp is never blank on first load.
  const generatedAt = content.state.status === "success" ? content.state.data.generated_at : null;
  const displayedRefreshedAt = refreshedAt ?? generatedAt;

  return (
    <PageShell
      title="Run Health"
      description="A tenant-level view of whether data is flowing, discovery is completing, content is fresh, and the expected analysis packs ran."
      actions={
        <div className="flex items-center gap-2">
          {role === "analyst" ? <StatusPill label="Read-only" tone="neutral" /> : null}
          <button
            type="button"
            data-testid="refresh-health"
            onClick={() => {
              void refreshAll();
            }}
            disabled={refreshing}
            aria-busy={refreshing}
            className="inline-flex items-center gap-2 rounded-lg border border-border bg-panel px-3 py-2 text-sm font-semibold text-muted shadow-sm hover:bg-bg/40 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} aria-hidden="true" />
            {refreshing ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      }
    >
      <div className="space-y-5">
        <section data-testid="tenant-health-summary" className="rounded-2xl border border-border bg-gradient-to-br from-panel to-panel2 p-5 text-text shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-4"><div className="flex items-start gap-3"><div className="rounded-xl bg-accent/10 p-2"><SummaryIcon className={`h-5 w-5 ${summary.icon === Loader2 ? "animate-spin" : ""}`} aria-hidden="true" /></div><div><h2 className="text-lg font-semibold">{summary.label}</h2><p className="mt-1 max-w-3xl text-sm text-muted">{summary.detail}</p></div></div><div className="text-xs text-muted" data-testid="health-updated-at">{refreshing ? "Refreshing…" : displayedRefreshedAt ? `Updated ${formatDate(displayedRefreshedAt)}` : "Update time unavailable"}</div></div>
        </section>
        <AttentionStrip resource={attention.state} retry={attention.refresh} />
        {/* The Content-and-Freshness and Packs panels are hidden from the grid.
            Their health reads are deliberately still performed above, so both
            still contribute to the tenant summary and the Attention Strip —
            hiding a card must not make a degraded tenant read as healthy. */}
        <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <ConnectorsPanel resource={connectors.state} retry={connectors.refresh} refresh={connectors.refresh} role={role} highlighted={selectedPanel === "connectors"} />
          <RunsPanel resource={runs.state} retry={runs.refresh} />
        </div>
      </div>
    </PageShell>
  );
}

export default function RunHealthDashboardPage() {
  const { user } = useAuth();
  if (user?.role === "viewer") return <ViewerDenied />;
  return <RunHealthDashboard role={user?.role === "analyst" ? "analyst" : "owner"} />;
}
