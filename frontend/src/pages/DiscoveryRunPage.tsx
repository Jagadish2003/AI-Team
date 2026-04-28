import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useConnectorContext } from '../context/ConnectorContext';
import { useSourceIntakeContext } from '../context/SourceIntakeContext';
import { useRunContext } from '../context/RunContext';

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
  const label = computing
    ? `Running (${displayPct}%)`
    : isComplete
      ? 'Complete (100%)'
      : (status ?? '-');
  const cls = computing
    ? 'border-accent/40 bg-accent/10 text-blue-200'
    : isComplete
      ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
      : 'border-border bg-bg/30 text-muted';

  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs whitespace-nowrap ${cls}`}>
      {label}
    </span>
  );
}

export default function DiscoveryRunPage() {
  const [autoScroll, setAutoScroll] = useState(true);
  const nav = useNavigate();
  const location = useLocation();
  const autoStartRequested = (location.state as { autoStart?: boolean } | null)?.autoStart === true;
  const { runId } = useRunContext();
  const { connectors } = useConnectorContext();
  const { uploadedFiles, sampleWorkspaceEnabled } = useSourceIntakeContext();

  const { run, events, loading, error, started, computing, startRun, restartRun, refetch } =
    useDiscoveryRunContext();

  const TOTAL_STAGES = 10;

  const status = run?.status?.toLowerCase();
  const isMaterialized = status === 'complete' || status === 'completed' || status === 'partial';
  const isComplete = status === 'complete' || status === 'completed';
  const runScopedPath = (path: string) => runId ? `${path}?runId=${runId}` : path;

  const [displayPct, setDisplayPct] = useState(0);
  const targetPct = useMemo(() => {
    if (isComplete) return 100;
    if (!computing) return 0;
    const seen = new Set(events.map((e: any) => e.stage).filter(Boolean));
    return Math.min(Math.round((seen.size / TOTAL_STAGES) * 100), 99);
  }, [isComplete, computing, events]);

  const animFrameRef = useRef<number | null>(null);
  useEffect(() => {
    if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    const step = () => {
      setDisplayPct((prev) => {
        if (prev < targetPct) {
          animFrameRef.current = requestAnimationFrame(step);
          return prev + 1;
        }
        if (prev > targetPct) {
          animFrameRef.current = requestAnimationFrame(step);
          return prev - 1;
        }
        return prev;
      });
    };
    animFrameRef.current = requestAnimationFrame(step);
    return () => {
      if (animFrameRef.current) cancelAnimationFrame(animFrameRef.current);
    };
  }, [targetPct]);

  const inputs = useMemo(() => {
    const connectedSources = connectors
      .filter((c) => c.status === 'connected')
      .map((c) => c.name);
    return {
      connectedSources,
      uploadedFiles: uploadedFiles.map((f) => f.name),
      sampleWorkspaceEnabled,
    };
  }, [connectors, uploadedFiles, sampleWorkspaceEnabled]);

  const hasAtLeastOneSource =
    inputs.connectedSources.length > 0 ||
    inputs.uploadedFiles.length > 0 ||
    inputs.sampleWorkspaceEnabled;

  useEffect(() => {
    if (!runId && autoStartRequested && !loading && hasAtLeastOneSource) {
      void startRun(inputs);
    }
  }, [runId, autoStartRequested, loading, startRun, inputs, hasAtLeastOneSource]);

  if (loading || (!runId && autoStartRequested && hasAtLeastOneSource)) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="mx-auto max-w-3xl px-6 py-10">
          <div className="rounded-xl border border-border bg-panel p-6">
            <div className="text-lg font-semibold">Starting discovery run...</div>
            <div className="mt-2 text-sm text-muted">Please wait...</div>
          </div>
        </div>
      </div>
    );
  }

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="flex h-[75vh] items-center justify-center">
          <div className="w-[550px] rounded-xl border border-white/20 bg-panel p-8 py-12 text-center shadow-xl shadow-black/20">
            <h2 className="mb-4 text-xl font-semibold text-text">No Active Run</h2>
            <p className="mb-6 text-sm text-muted">Start a new discovery run to continue.</p>
            <button
              onClick={() => void startRun(inputs)}
              disabled={!hasAtLeastOneSource}
              className="rounded-lg bg-accent px-6 py-2.5 text-sm font-medium text-white transition-all duration-200 hover:scale-[1.02] hover:opacity-90 active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:scale-100"
            >
              Start New Discovery Run
            </button>
            {!hasAtLeastOneSource && (
              <div className="mt-3 text-center text-sm font-medium text-muted">
                Connect Atleast One Source To Start Discovery
              </div>
            )}
          </div>
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
              The Discovery Run provides a clear, step-by-step view of progress with live logs and a continuously updated summary of detected applications, workflows, and opportunities.
            </div>
            <p className="mt-1 text-sm text-muted">
              Run ID: <span className="font-semibold text-text">{run?.id ?? runId ?? '-'}</span>
              {' - '}
              Status:{' '}
              <RunStatusPill
                computing={computing}
                isComplete={isComplete}
                displayPct={displayPct}
                status={run?.status}
              />
              {computing && (
                <span className="ml-2 inline-flex items-center gap-1 rounded-full bg-accent/20 px-2 py-0.5 text-xs font-medium text-accent">
                  <span className="animate-pulse">o</span> Computing...
                </span>
              )}
            </p>
            {run?.startedAt && (
              <p className="mt-1 text-xs text-muted">
                Started: {new Date(run.startedAt).toLocaleString()}
              </p>
            )}
          </div>

          <div className="flex items-center gap-2">
            <button
              className="rounded-md border border-border bg-buttonbg px-3 py-2 text-sm font-medium text-text transition hover:bg-panel disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => void restartRun()}
              disabled={!started || !isMaterialized || computing || loading}
              title={!isMaterialized ? 'Replay is available after this run finishes.' : undefined}
            >
              Replay Run
            </button>

            <button
              className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-textwhite transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => nav(runScopedPath('/partial-results'))}
              disabled={!started || !isMaterialized || computing}
              title={computing ? 'Waiting for compute to finish...' : undefined}
            >
              {computing ? 'Computing...' : 'Next: Evidence Collection'}
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="rounded-xl border border-border bg-panel p-4">
            <div className="text-lg font-semibold">Run Summary</div>
            <div className="mt-3 space-y-3 text-sm text-muted">
              <div>
                <div className="font-semibold text-text">Connected sources</div>
                <div className="mt-0.5">
                  {inputs.connectedSources.length
                    ? inputs.connectedSources.join(' - ')
                    : 'None'}
                </div>
              </div>
              <div>
                <div className="font-semibold text-text">Uploaded files</div>
                <div className="mt-0.5">
                  {inputs.uploadedFiles.length
                    ? inputs.uploadedFiles.join(' - ')
                    : 'None'}
                </div>
              </div>
              <div>
                <div className="font-semibold text-text">Sample workspace</div>
                <div className="mt-0.5">
                  {inputs.sampleWorkspaceEnabled ? 'Enabled' : 'Disabled'}
                </div>
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-border bg-panel p-4 lg:col-span-2">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-5">
                <div className="text-lg font-semibold">Discovery Log</div>
              <label className="flex items-center gap-2 text-sm text-text">
                Auto-scroll
                  <input
                  type="checkbox"
                  checked={autoScroll}
                  onChange={(e) => setAutoScroll(e.target.checked)}
                  className="accent-accent cursor-pointer"/>
              </label>
            </div>
              <button
                className="rounded-md border border-border bg-bg/20 px-3 py-2 text-sm font-semibold text-text transition hover:bg-panel2"
                onClick={() => refetch()}
              >
                Refresh
              </button>
            </div>

            <div className="mt-3 max-h-[420px] overflow-auto rounded-lg border border-border bg-bg/10 p-3">
              {events.length === 0 ? (
                <div className="text-sm text-muted">No events yet.</div>
              ) : (
                <div className="space-y-2 text-sm">
                  {events.map((e, i) => (
                    <div key={e.id ?? i} className="flex gap-3">
                      <div className="w-40 shrink-0 font-mono text-xs text-muted">
                        {e.tsLabel ?? e.ts ?? ''}
                      </div>
                      <div className="w-28 shrink-0 font-mono text-xs text-muted">
                        {e.stage ?? ''}
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
