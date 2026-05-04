import React, { useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { InfoPanel } from "../components/common/InfoPanel";
import LoadingPanel from "../components/common/LoadingPanel";
import TopNav from "../components/common/TopNav";
import { useDiscoveryRunContext } from "../context/DiscoveryRunContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useSourceIntakeContext } from "../context/SourceIntakeContext";
import { useRunContext } from "../context/RunContext";
import {
  DISCOVERY_SOURCE_REQUIREMENT_MESSAGE,
  isDiscoveryReadyConnector,
} from "../utils/sourceReadiness";

function RunStatusPill({
  computing,
  isComplete,
  displayPct,
  status,
}: {
  computing: boolean;
  isComplete: boolean;
  displayPct: number;
  status?: string;
}) {
  const normalizedStatus = status?.toLowerCase();
  const isPartial = normalizedStatus === "partial";
  const isFinished = isComplete || isPartial;
  const label = computing
    ? `Running (${displayPct}%)`
    : isFinished
      ? "Completed 100%"
        : (status ?? "-");
  const cls = computing
    ? "border-accent/40 bg-accent/10 text-blue-100"
    : isFinished
      ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
        : "border-border bg-bg/30 text-muted";
  const dotCls = computing
    ? "bg-accent"
    : isFinished
      ? "bg-emerald-400"
        : "bg-muted/50";

  return (
    <span
      className={`inline-flex h-7 items-center gap-2 whitespace-nowrap rounded-full border px-3 text-[13px] font-semibold leading-none align-middle ${cls}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${dotCls}`} />
      {label}
    </span>
  );
}

function ComputingPill() {
  return (
    <span className="inline-flex h-7 items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 text-[13px] font-semibold leading-none text-blue-100 shadow-[0_0_0_1px_rgba(37,99,235,0.08)]">
      <Loader2
        size={14}
        strokeWidth={2.5}
        className="shrink-0 animate-spin text-accent"
      />
      <span>Computing</span>
    </span>
  );
}

function PartialResultsPill() {
  return (
    <span className="inline-flex h-7 items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 text-[13px] font-semibold leading-none text-blue-100 shadow-[0_0_0_1px_rgba(37,99,235,0.08)]">
      <span className="h-1.5 w-1.5 rounded-full bg-accent" />
      Evidence collection ready
    </span>
  );
}

function formatRunTimestamp(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export default function DiscoveryRunPage() {
  const [autoScroll, setAutoScroll] = useState(true);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const nav = useNavigate();
  const location = useLocation();
  const autoStartRequested =
    (location.state as { autoStart?: boolean } | null)?.autoStart === true;
  const { runId } = useRunContext();
  const { connectors } = useConnectorContext();
  const { uploadedFiles, sampleWorkspaceEnabled } = useSourceIntakeContext();

  const {
    run,
    events,
    loading,
    error,
    started,
    computing,
    startRun,
    restartRun,
    refetch,
  } = useDiscoveryRunContext();

  const TOTAL_STAGES = 10;

  const status = run?.status?.toLowerCase();
  const isMaterialized =
    status === "complete" || status === "completed" || status === "partial";
  const isComplete = status === "complete" || status === "completed";
  const isPartial = status === "partial";
  const runScopedPath = (path: string) =>
    runId ? `${path}?runId=${runId}` : path;

  const [displayPct, setDisplayPct] = useState(0);
  const targetPct = useMemo(() => {
    if (isComplete) return 100;
    if (!computing) return 0;
    const seen = new Set(events.map((e: any) => e.stage).filter(Boolean));
    return Math.min(Math.round((seen.size / TOTAL_STAGES) * 100), 99);
  }, [isComplete, computing, events]);

  // FIX: Safe requestAnimationFrame implementation
  useEffect(() => {
    if (displayPct === targetPct) return;

    const id = requestAnimationFrame(() => {
      setDisplayPct((prev) => {
        if (prev < targetPct) return prev + 1;
        if (prev > targetPct) return prev - 1;
        return prev;
      });
    });

    return () => cancelAnimationFrame(id);
  }, [displayPct, targetPct]);

  const inputs = useMemo(() => {
    const connectedSources = connectors
      .filter(isDiscoveryReadyConnector)
      .map((c) => c.name);
    return {
      connectedSources,
      uploadedFiles: uploadedFiles.map((f) => f.name),
      sampleWorkspaceEnabled,
      mode: "live" as const,
    };
  }, [connectors, uploadedFiles, sampleWorkspaceEnabled]);
  const summaryInputs = run?.inputs ?? inputs;

  const hasAtLeastOneSource =
    inputs.connectedSources.length > 0 ||
    inputs.uploadedFiles.length > 0 ||
    inputs.sampleWorkspaceEnabled;

  useEffect(() => {
    if (!runId && autoStartRequested && !loading && hasAtLeastOneSource) {
      void startRun(inputs);
    }
  }, [
    runId,
    autoStartRequested,
    loading,
    startRun,
    inputs,
    hasAtLeastOneSource,
  ]);

  useEffect(() => {
    if (!autoScroll) return;
    const el = logScrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [events, autoScroll]);

  if (loading || (!runId && autoStartRequested && hasAtLeastOneSource)) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-8">
          <div className="mb-4">
            <div className="text-2xl font-semibold text-text">
              Discovery Run
            </div>
            <div className="mt-1 text-sm text-muted">
              The Discovery Run provides a clear, step-by-step view of progress
              with live logs and a continuously updated summary of detected
              applications, workflows, and opportunities.
            </div>
          </div>
          <LoadingPanel
            title="Starting discovery run"
            subtitle="Preparing the run and connecting the selected sources."
          />
        </div>
      </div>
    );
  }

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          <div className="mb-4">
            <div className="text-2xl font-semibold text-text">
              Discovery Run
            </div>
            <div className="mt-1 text-sm text-muted">
              The Discovery Run provides a clear, step-by-step view of progress
              with live logs and a continuously updated summary of detected
              applications, workflows, and opportunities.
            </div>
          </div>
          <InfoPanel
            title="No Active Run"
            message="Start a new discovery run to continue."
            actionLabel="Start New Discovery Run"
            actionDisabled={!hasAtLeastOneSource}
            onAction={() => void startRun(inputs)}
          >
            {!hasAtLeastOneSource && (
              <div className="mt-3 text-center text-sm font-medium text-muted">
                {DISCOVERY_SOURCE_REQUIREMENT_MESSAGE}
              </div>
            )}
          </InfoPanel>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="mx-auto max-w-3xl px-6 py-10">
          <div className="rounded-xl border border-border bg-panel p-6">
            <div className="text-lg font-semibold">Discovery run failed</div>
            <div className="mt-2 text-sm text-red-300">{error}</div>
            <button
              className="mt-4 rounded-md bg-accent px-3 py-2 text-sm text-bg hover:opacity-90"
              onClick={() => {
                if (runId) refetch();
                else if (hasAtLeastOneSource) void startRun(inputs);
              }}
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen text-text">
      <TopNav />
      <div className="px-8 py-6">
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold">Discovery Run</h1>
            <div className="mb-2 mt-1 text-sm text-muted">
              The Discovery Run provides a clear, step-by-step view of progress
              with live logs and a continuously updated summary of detected
              applications, workflows, and opportunities.
            </div>
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted">
              <span>
                Run ID:{" "}
                <span className="font-semibold text-text">
                  {run?.id ?? runId ?? "-"}
                </span>
              </span>
              <span className="text-muted">-</span>
              <span>Status:</span>
              <RunStatusPill
                computing={computing}
                isComplete={isComplete}
                displayPct={displayPct}
                status={run?.status}
              />
              {computing && <ComputingPill />}
              {!computing && isPartial && <PartialResultsPill />}
            </p>
            {run?.startedAt && (
              <p className="mt-1 text-xs text-muted">
                Started: {formatRunTimestamp(run.startedAt)}
              </p>
            )}
          </div>

          <div className="flex items-center gap-2">
            <button
              className="rounded-md border border-border bg-buttonbg px-3 py-2 text-sm font-medium text-text transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => void restartRun()}
              disabled={!started || !isMaterialized || computing || loading}
              title={
                !isMaterialized
                  ? "Replay is available after this run finishes."
                  : undefined
              }
            >
              Replay Run
            </button>

            <button
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-textwhite transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => nav(runScopedPath("/partial-results"))}
              disabled={!started || !isMaterialized || computing}
              title={computing ? "Waiting for compute to finish..." : undefined}
            >
              {computing ? "Computing..." : "Next: Evidence Collection"}
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-3">
          <div className="flex h-[460px] min-h-0 flex-col rounded-xl border border-border bg-panel p-4 lg:h-[520px]">
            <div className="shrink-0 text-lg font-semibold">Run Summary</div>
            <div className="mt-3 min-h-0 flex-1 space-y-8 overflow-auto pr-1 text-sm text-muted">
              <div className="rounded-lg border border-border bg-bg/10 p-3">
                <div className="font-semibold text-text">Connected sources</div>
                <div className="mt-1 max-h-28 overflow-auto break-words pr-1">
                  {summaryInputs.connectedSources.length
                    ? summaryInputs.connectedSources.join(" - ")
                    : "None"}
                </div>
              </div>
              <div className="rounded-lg border border-border bg-bg/10 p-3">
                <div className="font-semibold text-text">Uploaded files</div>
                <div className="mt-1 max-h-28 overflow-auto break-words pr-1">
                  {summaryInputs.uploadedFiles.length
                    ? summaryInputs.uploadedFiles.join(" - ")
                    : "None"}
                </div>
              </div>
              <div className="rounded-lg border border-border bg-bg/10 p-3">
                <div className="font-semibold text-text">Sample workspace</div>
                <div className="mt-1 max-h-28 overflow-auto break-words pr-1">
                  {summaryInputs.sampleWorkspaceEnabled
                    ? "Enabled"
                    : "Disabled"}
                </div>
              </div>
            </div>
          </div>

          <div className="flex h-[460px] min-h-0 flex-col rounded-xl border border-border bg-panel p-4 lg:col-span-2 lg:h-[520px]">
            <div className="flex shrink-0 items-center justify-between">
              <div className="flex items-center gap-5">
                <div className="text-lg font-semibold">Discovery Log</div>
                <label className="flex items-center gap-2 text-sm text-text">
                  Auto-scroll
                  <input
                    type="checkbox"
                    checked={autoScroll}
                    onChange={(e) => setAutoScroll(e.target.checked)}
                    className="accent-accent cursor-pointer"
                  />
                </label>
              </div>
              <button
                className="rounded-md border border-border bg-bg/20 px-3 py-2 text-sm font-semibold text-text transition hover:bg-panel2"
                onClick={() => refetch()}
              >
                Refresh
              </button>
            </div>

            <div
              ref={logScrollRef}
              className="mt-3 min-h-0 flex-1 overflow-auto rounded-lg border border-border bg-bg/10 p-3"
            >
              {events.length === 0 ? (
                <div className="text-sm text-muted">No events yet.</div>
              ) : (
                <div className="space-y-2 text-sm">
                  {events.map((e, i) => (
                    <div key={e.id ?? i} className="flex gap-3">
                      <div className="w-40 shrink-0 font-mono text-xs text-muted">
                        {formatRunTimestamp(e.tsLabel ?? e.ts)}
                      </div>
                      <div className="w-28 shrink-0 font-mono text-xs text-muted">
                        {e.stage ?? ""}
                      </div>
                      <div className="text-text">{e.message}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
