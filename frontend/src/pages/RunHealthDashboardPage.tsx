import { useCallback, useEffect, useMemo, useState } from "react";
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
  ShieldAlert,
} from "lucide-react";

import {
  fetchAttentionHealth,
  fetchConnectorHealth,
  fetchContentHealth,
  fetchPackHealth,
  fetchRunHealth,
} from "../api/runHealthApi";
import PageShell from "../components/common/PageShell";
import { useAuth } from "../context/AuthContext";
import type {
  AttentionHealthResponse,
  ConnectorHealthItem,
  ConnectorHealthResponse,
  ContentHealthResponse,
  HealthPanelId,
  HealthSeverity,
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

const PANEL_IDS: HealthPanelId[] = ["connectors", "runs", "content", "packs"];

const severityStyles: Record<HealthSeverity, string> = {
  critical: "border-red-300 bg-red-50 text-red-800",
  high: "border-orange-300 bg-orange-50 text-orange-800",
  medium: "border-amber-300 bg-amber-50 text-amber-800",
  low: "border-blue-300 bg-blue-50 text-blue-800",
};

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return fallback;
}

function useHealthResource<T>(loader: () => Promise<T>, label: string) {
  const [state, setState] = useState<ResourceState<T>>(INITIAL_STATE);

  const refresh = useCallback(async () => {
    setState({ status: "loading", data: null, error: null });
    try {
      const data = await loader();
      setState({ status: "success", data, error: null });
    } catch (error) {
      setState({
        status: "error",
        data: null,
        error: errorMessage(error, `${label} could not be loaded.`),
      });
    }
  }, [label, loader]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { state, refresh };
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

function StatusPill({ label, tone }: { label: string; tone: "good" | "warn" | "bad" | "info" | "neutral" }) {
  const styles = {
    good: "border-emerald-200 bg-emerald-50 text-emerald-700",
    warn: "border-amber-200 bg-amber-50 text-amber-800",
    bad: "border-red-200 bg-red-50 text-red-700",
    info: "border-blue-200 bg-blue-50 text-blue-700",
    neutral: "border-slate-200 bg-slate-50 text-slate-700",
  };
  return (
    <span className={`inline-flex rounded-full border px-2.5 py-1 text-xs font-semibold ${styles[tone]}`}>
      {label}
    </span>
  );
}

function Metric({ label, value, detail }: { label: string; value: string | number; detail?: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50/80 p-3">
      <div className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-900">{value}</div>
      {detail ? <div className="mt-1 text-xs text-slate-500">{detail}</div> : null}
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
      className={`scroll-mt-24 rounded-2xl border bg-white p-5 shadow-sm transition ${
        highlighted ? "border-blue-400 ring-4 ring-blue-100" : "border-slate-200"
      }`}
    >
      <div className="mb-4 flex items-start gap-3">
        <div className="rounded-xl bg-slate-100 p-2 text-slate-700">{icon}</div>
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-slate-900">{title}</h2>
          <p className="mt-1 text-sm text-slate-600">{description}</p>
        </div>
        <PanelStateIndicator state={state} />
      </div>
      {children}
    </section>
  );
}

function PanelLoading({ label }: { label: string }) {
  return (
    <div role="status" className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 p-5 text-sm text-slate-600">
      <Loader2 className="h-5 w-5 animate-spin text-blue-600" aria-hidden="true" />
      Loading {label.toLowerCase()}…
    </div>
  );
}

function PanelError({ label, message, onRetry }: { label: string; message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="rounded-xl border border-red-200 bg-red-50 p-4 text-red-900">
      <div className="flex gap-3">
        <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
        <div className="min-w-0">
          <div className="font-semibold">{label} unavailable</div>
          <p className="mt-1 break-words text-sm text-red-800">{message}</p>
          <p className="mt-1 text-xs text-red-700">No healthy or zero-value state is inferred while this read is unavailable.</p>
          <button
            type="button"
            onClick={onRetry}
            className="mt-3 rounded-lg border border-red-300 bg-white px-3 py-1.5 text-sm font-semibold hover:bg-red-100"
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
    <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-5 text-center">
      <div className="font-semibold text-slate-800">{title}</div>
      <p className="mt-1 text-sm text-slate-600">{detail}</p>
    </div>
  );
}

function connectorTone(item: ConnectorHealthItem): "good" | "warn" | "bad" | "neutral" {
  if (["error", "disconnected", "needs_auth", "refresh_failed"].includes(item.connection_state)) return "bad";
  if (item.last_error || !["connected", "live"].includes(item.connection_state)) return "warn";
  return "good";
}

function ConnectorsPanel({
  resource,
  retry,
  highlighted,
}: {
  resource: ResourceState<ConnectorHealthResponse>;
  retry: () => void;
  highlighted: boolean;
}) {
  let state: string = resource.status;
  if (resource.status === "success") {
    const hasIssues = resource.data.connectors.some((item) => connectorTone(item) !== "good");
    const hasMissing = resource.data.connectors.some(
      (item) => !item.last_successful_ingestion || item.checkpoint_age_seconds === null,
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
          <div className="space-y-3">
            {resource.data.connectors.map((item) => (
              <article key={item.connector_id} className="rounded-xl border border-slate-200 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="font-semibold text-slate-900">{item.name}</h3>
                    <p className="text-sm text-slate-500">{item.tier ? `${labelize(item.tier)} connector` : labelize(item.connector_id)}</p>
                  </div>
                  <StatusPill label={labelize(item.connection_state)} tone={connectorTone(item)} />
                </div>
                <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-3">
                  <div><dt className="text-slate-500">Last ingestion</dt><dd className="mt-0.5 font-medium text-slate-800">{formatDate(item.last_successful_ingestion)}</dd></div>
                  <div><dt className="text-slate-500">Checkpoint age</dt><dd className="mt-0.5 font-medium text-slate-800">{formatAge(item.checkpoint_age_seconds)}</dd></div>
                  <div><dt className="text-slate-500">Authentication</dt><dd className="mt-0.5 font-medium text-slate-800">{item.auth_mode ? labelize(item.auth_mode) : "Not available"}</dd></div>
                </dl>
                {item.last_error ? (
                  <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
                    <span className="font-semibold">Latest issue:</span> {item.last_error}
                  </div>
                ) : null}
                <details className="mt-3 text-sm">
                  <summary className="cursor-pointer font-medium text-blue-700">Supporting details</summary>
                  <dl className="mt-2 grid gap-2 rounded-lg bg-slate-50 p-3 sm:grid-cols-2">
                    <div><dt className="text-slate-500">Checkpoint position</dt><dd className="font-medium text-slate-800">{item.checkpoint_position ?? "Not available"}</dd></div>
                    <div><dt className="text-slate-500">Checkpoint recorded</dt><dd className="font-medium text-slate-800">{formatDate(item.checkpoint_captured_at)}</dd></div>
                  </dl>
                </details>
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
  return (
    <details className="mt-3 text-sm">
      <summary className="cursor-pointer font-medium text-blue-700">Stage and detector details</summary>
      <div className="mt-2 space-y-3 rounded-lg bg-slate-50 p-3">
        {(run.degraded_stages?.length ?? 0) > 0 ? (
          <div>
            <div className="font-semibold text-slate-800">Stages needing attention</div>
            <ul className="mt-1 space-y-1 text-slate-700">
              {run.degraded_stages?.map((stage, index) => (
                <li key={`${stage.stage}-${index}`}>
                  <span className="font-medium">{labelize(stage.stage)}:</span> {stage.reason}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {(run.stage_outcomes?.length ?? 0) > 0 ? (
          <div className="flex flex-wrap gap-2">
            {run.stage_outcomes?.map((stage, index) => (
              <StatusPill key={`${stage.stage}-${index}`} label={`${labelize(stage.stage ?? "Unknown stage")}: ${labelize(stage.level ?? "Recorded")}`} tone={runTone(stage.level)} />
            ))}
          </div>
        ) : (
          <div className="text-slate-500">No stage-level events were recorded.</div>
        )}
      </div>
    </details>
  );
}

function RunsPanel({
  resource,
  retry,
  highlighted,
}: {
  resource: ResourceState<RunHealthResponse>;
  retry: () => void;
  highlighted: boolean;
}) {
  const state = resource.status === "success"
    ? resource.data.runs.length === 0
      ? "empty"
      : resource.data.runs.some((run) => ["failed", "degraded"].includes(run.health_status))
        ? "degraded"
        : resource.data.runs.some((run) => ["running", "created", "queued", "pending"].includes(run.health_status))
          ? "in-progress"
          : resource.data.runs.some((run) => run.health_status !== "healthy")
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
      highlighted={highlighted}
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
          <div className="space-y-3">
            {resource.data.runs.map((run) => (
              <article key={run.run_id} className="rounded-xl border border-slate-200 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h3 className="font-semibold text-slate-900">Run {run.run_id.slice(0, 8)}</h3>
                    <p className="text-sm text-slate-500">{formatDate(run.started_at)}</p>
                  </div>
                  <StatusPill label={labelize(run.health_status)} tone={runTone(run.health_status)} />
                </div>
                <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-5">
                  <div><dt className="text-slate-500">Systems</dt><dd className="font-semibold text-slate-800">{run.system_count ?? "Not available"}</dd></div>
                  <div><dt className="text-slate-500">Detectors</dt><dd className="font-semibold text-slate-800">{run.detectors_evaluated ?? "Not available"} evaluated</dd></div>
                  <div><dt className="text-slate-500">Findings</dt><dd className="font-semibold text-slate-800">{run.detectors_fired ?? "Not available"} fired</dd></div>
                  <div><dt className="text-slate-500">Opportunities</dt><dd className="font-semibold text-slate-800">{run.opportunities ?? "Not available"}</dd></div>
                  <div><dt className="text-slate-500">Duration</dt><dd className="font-semibold text-slate-800">{run.duration_seconds === null || run.duration_seconds === undefined ? "Not available" : formatDuration(run.duration_seconds * 1000)}</dd></div>
                </dl>
                {run.pack_id ? (
                  <p className="mt-3 text-sm text-slate-600">
                    Pack: <span className="font-medium text-slate-800">{run.pack_id}</span>
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
          <div className="rounded-xl border border-slate-200 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="font-semibold text-slate-900">Refresh progress</h3>
              <StatusPill label={resource.data.backfill.complete ? "Complete" : resource.data.backfill.awaiting_backfill ? "In progress" : "No active refresh"} tone={resource.data.backfill.complete ? "good" : resource.data.backfill.awaiting_backfill ? "info" : "neutral"} />
            </div>
            {progress !== null ? (
              <div className="mt-3">
                <div className="h-2 overflow-hidden rounded-full bg-slate-200" aria-label={`Refresh ${progress}% complete`}>
                  <div className="h-full rounded-full bg-blue-600" style={{ width: `${progress}%` }} />
                </div>
                <p className="mt-1 text-xs text-slate-500">{progress}% complete</p>
              </div>
            ) : (
              <p className="mt-2 text-sm text-slate-500">No active refresh progress is available.</p>
            )}
          </div>
          <details className="rounded-xl border border-slate-200 p-4" open={resource.data.skipped.length > 0}>
            <summary className="cursor-pointer font-semibold text-slate-900">Supporting source details</summary>
            <div className="mt-3 overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="text-xs uppercase text-slate-500"><tr><th className="pb-2 pr-4">Source</th><th className="pb-2 pr-4">Discovered</th><th className="pb-2">Embedded</th></tr></thead>
                <tbody className="divide-y divide-slate-100">
                  {resource.data.indexed_by_source.map((source) => (
                    <tr key={source.source_system}><td className="py-2 pr-4 font-medium text-slate-800">{labelize(source.source_system)}</td><td className="py-2 pr-4">{source.chunk_count}</td><td className="py-2">{source.embedded_count}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            {resource.data.skipped.length > 0 ? (
              <div className="mt-4">
                <div className="text-sm font-semibold text-slate-800">Skipped items</div>
                <ul className="mt-2 space-y-1 text-sm text-slate-600">
                  {resource.data.skipped.map((item) => <li key={item.reason}>{labelize(item.reason)}: {item.count}</li>)}
                </ul>
              </div>
            ) : null}
          </details>
        </div>
      ) : null}
    </PanelFrame>
  );
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
  const state = resource.status === "success"
    ? resource.data.packs.length === 0
      ? "empty"
      : resource.data.packs.some((pack) => !pack.pack_version)
        ? "partial"
        : "healthy"
    : resource.status;
  return (
    <PanelFrame
      id="panel-packs"
      title="Packs"
      description="Confirm which analysis packs and detectors executed, including the exact pack version used."
      icon={<Boxes className="h-5 w-5" aria-hidden="true" />}
      state={state}
      highlighted={highlighted}
    >
      {resource.status === "loading" ? <PanelLoading label="Pack health" /> : null}
      {resource.status === "error" ? <PanelError label="Pack health" message={resource.error} onRetry={retry} /> : null}
      {resource.status === "success" && resource.data.packs.length === 0 ? (
        <EmptyState title="No pack executions yet" detail="Pack versions and detector execution will appear after a discovery run uses them." />
      ) : null}
      {resource.status === "success" && resource.data.packs.length > 0 ? (
        <div className="space-y-3">
          {resource.data.packs.map((pack) => (
            <article key={`${resource.data.run_id}-${pack.pack_id}`} className="rounded-xl border border-slate-200 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div><h3 className="font-semibold text-slate-900">{pack.pack_name ?? pack.pack_id}</h3><p className="text-sm text-slate-500">Run {resource.data.run_id?.slice(0, 8) ?? "Not available"}</p></div>
                <StatusPill label={pack.pack_version ? `Version ${pack.pack_version}` : "Version unavailable"} tone={pack.pack_version ? "info" : "warn"} />
              </div>
              <dl className="mt-4 grid gap-3 text-sm sm:grid-cols-2">
                <div><dt className="text-slate-500">Detectors in executed pack</dt><dd className="text-xl font-semibold text-slate-900">{pack.detector_count}</dd></div>
                <div><dt className="text-slate-500">Pack identifier</dt><dd className="text-lg font-semibold text-slate-900">{pack.pack_id}</dd></div>
              </dl>
              {pack.detectors && pack.detectors.length > 0 ? <details className="mt-3 text-sm"><summary className="cursor-pointer font-medium text-blue-700">Detector list</summary><div className="mt-2 flex flex-wrap gap-2">{pack.detectors.map((detector) => <StatusPill key={detector} label={detectorLabel(detector)} tone="neutral" />)}</div></details> : null}
            </article>
          ))}
        </div>
      ) : null}
    </PanelFrame>
  );
}

function AttentionStrip({ resource, retry }: { resource: ResourceState<AttentionHealthResponse>; retry: () => void }) {
  const state = resource.status === "success" ? (resource.data.items.length === 0 ? "empty" : "attention") : resource.status;
  return (
    <section data-testid="attention-strip" data-state={state} className="rounded-2xl border border-amber-300 bg-amber-50/70 p-5 shadow-sm">
      <div className="mb-4 flex items-start gap-3">
        <div className="rounded-xl bg-amber-100 p-2 text-amber-800"><ShieldAlert className="h-5 w-5" aria-hidden="true" /></div>
        <div><h2 className="text-lg font-semibold text-slate-900">Attention Strip</h2><p className="mt-1 text-sm text-slate-600">Prioritized conditions that may need investigation, linked directly to supporting details.</p></div>
      </div>
      {resource.status === "loading" ? <PanelLoading label="Attention items" /> : null}
      {resource.status === "error" ? <PanelError label="Attention strip" message={resource.error} onRetry={retry} /> : null}
      {resource.status === "success" && resource.data.items.length === 0 ? (
        <div className="flex items-start gap-3 rounded-xl border border-emerald-200 bg-white p-4 text-emerald-800"><CheckCircle2 className="mt-0.5 h-5 w-5" aria-hidden="true" /><div><div className="font-semibold">No current attention items</div><p className="mt-1 text-sm">The health service did not report any prioritized conditions for this organization.</p></div></div>
      ) : null}
      {resource.status === "success" && resource.data.items.length > 0 ? (
        <ol className="space-y-3">
          {resource.data.items.map((item) => (
            <li key={item.id} className="rounded-xl border border-amber-200 bg-white p-4">
              <div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><StatusPill label={item.severity.toUpperCase()} tone={item.severity === "critical" ? "bad" : item.severity === "high" || item.severity === "medium" ? "warn" : "info"} /><h3 className="font-semibold text-slate-900">{item.title}</h3></div><p className="mt-2 text-sm text-slate-700">{item.explanation}</p><p className="mt-2 text-xs text-slate-500">Detected {formatDate(item.timestamp)}</p></div><Link to={item.href} className={`shrink-0 rounded-lg border px-3 py-2 text-sm font-semibold hover:brightness-95 ${severityStyles[item.severity]}`}>View {item.panel} details</Link></div>
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
      <section role="alert" className="mx-auto max-w-2xl rounded-2xl border border-amber-200 bg-amber-50 p-6 text-amber-950">
        <div className="flex gap-3"><ShieldAlert className="mt-0.5 h-6 w-6 shrink-0" aria-hidden="true" /><div><h2 className="text-lg font-semibold">Run Health access is restricted</h2><p className="mt-2 text-sm">Owners and Analysts can view organization health. Viewers do not have permission to access this dashboard.</p></div></div>
      </section>
    </PageShell>
  );
}

function RunHealthDashboard({ role }: { role: "owner" | "analyst" }) {
  const [searchParams] = useSearchParams();
  const selectedPanel = searchParams.get("panel") as HealthPanelId | null;
  const connectors = useHealthResource(fetchConnectorHealth, "Connector health");
  const runs = useHealthResource(fetchRunHealth, "Run health");
  const content = useHealthResource(fetchContentHealth, "Content health");
  const packs = useHealthResource(fetchPackHealth, "Pack health");
  const attention = useHealthResource(fetchAttentionHealth, "Attention items");

  useEffect(() => {
    if (!selectedPanel || !PANEL_IDS.includes(selectedPanel)) return;
    document.getElementById(`panel-${selectedPanel}`)?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [selectedPanel, connectors.state.status, runs.state.status, content.state.status, packs.state.status]);

  const summary = useMemo(
    () => overallSummary(connectors.state, runs.state, content.state, packs.state, attention.state),
    [connectors.state, runs.state, content.state, packs.state, attention.state],
  );
  const SummaryIcon = summary.icon;
  const refreshedAt = content.state.status === "success" ? content.state.data.generated_at : null;
  const refreshAll = () => {
    void connectors.refresh();
    void runs.refresh();
    void content.refresh();
    void packs.refresh();
    void attention.refresh();
  };

  return (
    <PageShell
      title="Run Health"
      description="A tenant-level view of whether data is flowing, discovery is completing, content is fresh, and the expected analysis packs ran."
      actions={<div className="flex items-center gap-2">{role === "analyst" ? <StatusPill label="Read-only" tone="neutral" /> : null}<button type="button" onClick={refreshAll} className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 shadow-sm hover:bg-slate-50"><RefreshCw className="h-4 w-4" aria-hidden="true" />Refresh</button></div>}
    >
      <div className="space-y-5">
        <section data-testid="tenant-health-summary" className="rounded-2xl border border-slate-200 bg-gradient-to-br from-slate-900 to-slate-800 p-5 text-white shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-4"><div className="flex items-start gap-3"><div className="rounded-xl bg-white/10 p-2"><SummaryIcon className={`h-5 w-5 ${summary.icon === Loader2 ? "animate-spin" : ""}`} aria-hidden="true" /></div><div><h2 className="text-lg font-semibold">{summary.label}</h2><p className="mt-1 max-w-3xl text-sm text-slate-300">{summary.detail}</p></div></div><div className="text-xs text-slate-400">{refreshedAt ? `Updated ${formatDate(refreshedAt)}` : "Update time unavailable"}</div></div>
        </section>
        <AttentionStrip resource={attention.state} retry={attention.refresh} />
        <div className="grid gap-5 xl:grid-cols-2">
          <ConnectorsPanel resource={connectors.state} retry={connectors.refresh} highlighted={selectedPanel === "connectors"} />
          <RunsPanel resource={runs.state} retry={runs.refresh} highlighted={selectedPanel === "runs"} />
          <ContentPanel resource={content.state} retry={content.refresh} highlighted={selectedPanel === "content"} />
          <PacksPanel resource={packs.state} retry={packs.refresh} highlighted={selectedPanel === "packs"} />
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
