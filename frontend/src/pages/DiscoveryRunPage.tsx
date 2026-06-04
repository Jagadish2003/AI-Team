import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { useLocation, useNavigate } from "react-router-dom";
import { InfoPanel } from "../components/common/InfoPanel";
import LoadingPanel from "../components/common/LoadingPanel";
import PageShell from "../components/common/PageShell";
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
    <span className="inline-flex h-7 items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 text-[13px] font-semibold leading-none text-blue-100">
      <Loader2
        size={14}
        strokeWidth={2.5}
        className="shrink-0 animate-spin text-accent"
      />
      <span>Computing</span>
    </span>
  );
}

function SourceIntelligenceReadyPill() {
  return (
    <span className="inline-flex h-7 items-center gap-2 rounded-full border border-accent/30 bg-accent/10 px-3 text-[13px] font-semibold leading-none text-blue-100">
      <span className="h-1.5 w-1.5 rounded-full bg-accent" />
      Source Intelligence ready
    </span>
  );
}

function formatRunTimestamp(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatRunMessage(message?: string | null) {
  return (message ?? "").replace(/(\.\.\.|…)$/, "");
}

export default function DiscoveryRunPage() {
  const [autoScroll, setAutoScroll] = useState(true);
  const [logHasOverflow, setLogHasOverflow] = useState(false);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const nav = useNavigate();
  const location = useLocation();
  const autoStartRequested =
    (location.state as { autoStart?: boolean } | null)?.autoStart === true;
  const { runId } = useRunContext();
  const { connectors } = useConnectorContext();
  const { uploadedFiles } = useSourceIntakeContext(); // T41-8: sampleWorkspaceEnabled removed

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
      sampleWorkspaceEnabled: false,
      mode: "live" as const,
    };
  }, [connectors, uploadedFiles]); // T41-8: sampleWorkspaceEnabled removed

  // Fix Pack Sprint 7: read connected sources from run record when available.
  // Stack Builder runs store selectedSystemIds on the run record via the
  // launch endpoint (routes_stack_builder_launch.py). Use those system IDs
  // as the display source list so Discovery Run shows the actual systems
  // used — not Integration Hub connector status (which shows None when
  // systems are not yet authorized in Integration Hub).
  const runSelectedSystems: string[] = (run as any)?.selectedSystemIds ?? [];
  const summaryInputs = useMemo(() => {
    if (runSelectedSystems.length > 0) {
      return {
        connectedSources: runSelectedSystems,
        uploadedFiles: uploadedFiles.map((f) => f.name),
        sampleWorkspaceEnabled: false,
        mode: "live" as const,
      };
    }
    return run?.inputs ?? inputs;
  }, [run, runSelectedSystems, inputs, uploadedFiles]);

  const hasAtLeastOneSource =
    inputs.connectedSources.length > 0 ||
    inputs.uploadedFiles.length > 0; // T41-8: sampleWorkspaceEnabled removed

  const pageDescription =
    "Monitor discovery progress, live logs, and the run summary for connected sources and uploaded files.";

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

  const updateLogOverflow = useCallback(() => {
    const el = logScrollRef.current;
    if (!el) {
      setLogHasOverflow(false);
      return;
    }
    setLogHasOverflow(el.scrollHeight > el.clientHeight + 1);
  }, []);

  useEffect(() => {
    updateLogOverflow();
  }, [events, updateLogOverflow]);

  useEffect(() => {
    const el = logScrollRef.current;
    if (!el) return;

    updateLogOverflow();

    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateLogOverflow);
      return () => window.removeEventListener("resize", updateLogOverflow);
    }

    const observer = new ResizeObserver(updateLogOverflow);
    observer.observe(el);
    return () => observer.disconnect();
  }, [updateLogOverflow]);

  if (loading || (!runId && autoStartRequested && hasAtLeastOneSource)) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <LoadingPanel
          title="Starting discovery run"
          subtitle="Preparing the run and connecting the selected sources."
        />
      </PageShell>
    );
  }

  if (!runId) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
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
      </PageShell>
    );
  }

  if (error) {
    return (
      <PageShell title="Discovery Run" description={pageDescription}>
        <div className="mx-auto max-w-3xl">
          <div className="rounded-xl border border-border bg-panel p-6">
            <div className="text-lg font-semibold">Discovery run failed</div>
            <div className="mt-2 text-sm text-red-300">{error}</div>
            <button
              className="mt-4 rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
              onClick={() => {
                if (runId) refetch();
                else if (hasAtLeastOneSource) void startRun(inputs);
              }}
            >
              Retry
            </button>
          </div>
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell
      title="Discovery Run"
      description={pageDescription}
      actions={
        <>
          <button
            className="rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
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
            className="rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => nav(runScopedPath("/source-intelligence"))}
            disabled={!started || !isMaterialized || computing}
            title={computing ? "Waiting for compute to finish..." : undefined}
          >
            {computing ? "Computing..." : "Next: Source Intelligence"}
          </button>
        </>
      }
    >
        <div className="mb-5 rounded-xl border border-border bg-panel px-4 py-3">
            <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted">
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
              {!computing && isMaterialized && <SourceIntelligenceReadyPill />}
            </p>
            {run?.startedAt && (
              <p className="mt-1 text-xs text-muted">
                Started: {formatRunTimestamp(run.startedAt)}
              </p>
            )}
        </div>

        <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-3">
          <div className="flex h-[460px] min-h-0 flex-col rounded-xl border border-border bg-panel p-4 lg:h-[560px]">
            <div className="shrink-0 text-lg font-semibold">Run Summary</div>
            <div className="mt-3 min-h-0 flex-1 space-y-4 overflow-auto pr-1 text-sm text-muted">
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
              {/* T41-8: Sample workspace panel removed */}
            </div>
          </div>

          <div className="flex h-[460px] min-h-0 flex-col rounded-xl border border-border bg-panel p-4 lg:col-span-2 lg:h-[560px]">
            <div className="flex shrink-0 items-center justify-between">
              <div className="flex items-center gap-5">
                <div className="text-lg font-semibold">Discovery Log</div>
                {logHasOverflow && (
                  <label className="flex items-center gap-2 text-sm text-text">
                    Auto-scroll
                    <input
                      type="checkbox"
                      checked={autoScroll}
                      onChange={(e) => setAutoScroll(e.target.checked)}
                      className="accent-accent cursor-pointer"
                    />
                  </label>
                )}
              </div>
              <button
                className="rounded-md border border-accent/20 bg-accent/5 px-3 py-2 text-sm font-semibold text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
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
                    <div key={e.id ?? i} className="grid gap-x-3 gap-y-1 text-sm leading-6 sm:grid-cols-[10rem_7rem_minmax(0,1fr)]">
                      <div className="text-muted">
                        {formatRunTimestamp(e.tsLabel ?? e.ts)}
                      </div>
                      <div className="font-normal normal-case tracking-normal text-muted">
                        {e.stage ?? ""}
                      </div>
                      <div className="min-w-0 whitespace-normal break-words text-text">{formatRunMessage(e.message)}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
    </PageShell>
  );
}
