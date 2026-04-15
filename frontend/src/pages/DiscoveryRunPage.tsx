import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useConnectorContext } from '../context/ConnectorContext';
import { useSourceIntakeContext } from '../context/SourceIntakeContext';
import { useRunContext } from '../context/RunContext';

export default function DiscoveryRunPage() {
  const [autoScroll, setAutoScroll] = useState(true);
  const nav = useNavigate();
  const { runId } = useRunContext();
  const { connectors } = useConnectorContext();
  const { uploadedFiles, sampleWorkspaceEnabled } = useSourceIntakeContext();

  const { run, events, loading, error, started, startRun, restartRun, refetch } =
    useDiscoveryRunContext();

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

  useEffect(() => {
    if (runId) return;
    void startRun(inputs);
  }, [runId, startRun, inputs]);

  if (loading) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="mx-auto max-w-3xl px-6 py-10">
          <div className="rounded-xl border border-border bg-panel p-6">
            <div className="text-lg font-semibold">Starting discovery run…</div>
            <div className="mt-2 text-sm text-muted">Please wait…</div>
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
                else void startRun(inputs);
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

        {/* HEADER */}
        <div className="mb-6 flex items-center justify-between">
          <div>
            <h1 className="text-xl font-semibold">Discovery Run</h1>
            <p className="mt-1 text-sm text-muted">
              Run ID: <span className="font-mono text-text">{run?.id ?? runId ?? '—'}</span>
              {' · '}
              Status: <span className="font-mono text-text">{run?.status ?? '—'}</span>
            </p>
            {run?.startedAt && (
              <p className="mt-1 text-xs text-muted">
                Started: {new Date(run.startedAt).toLocaleString()}
              </p>
            )}
          </div>

          <div className="flex items-center gap-2">
            <button
             className="rounded-md border border-border bg-panel px-3 py-2 text-sm font-medium text-text hover:bg-buttonhoverbg transition disabled:cursor-not-allowed disabled:opacity-50"
              onClick={() => void restartRun()}
              disabled={!started}
            >
              Replay Run
            </button>

            <button
              className="rounded-md bg-accent px-3 py-2 text-sm text-bg font-medium hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 transition"
              onClick={() => nav('/partial-results')}
              disabled={!started}
            >
              Next: Partial Results
            </button>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">

          {/* Run Inputs */}
          <div className="rounded-xl border border-border bg-panel p-4">
            <div className="text-lg font-semibold">Run Inputs</div>
            <div className="mt-3 space-y-3 text-sm text-muted">
              <div>
                <div className="font-semibold text-text">Connected sources</div>
                <div className="mt-0.5">
                  {inputs.connectedSources.length
                    ? inputs.connectedSources.join(' · ')
                    : 'None'}
                </div>
              </div>
              <div>
                <div className="font-semibold text-text">Uploaded files</div>
                <div className="mt-0.5">
                  {inputs.uploadedFiles.length
                    ? inputs.uploadedFiles.join(' · ')
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

          {/* Discovery Log */}
          <div className="lg:col-span-2 rounded-xl border border-border bg-panel p-4">
            {/* Header with Auto-scroll */}
           <div className="flex items-center justify-between">
            <div className="flex items-center gap-5">
              <div className="text-lg font-semibold">Discovery Log</div>
              <label className="flex items-center gap-2 text-sm text-text">
                Auto-scroll
                  <input
                  type="checkbox" 
                  checked={autoScroll}
                  onChange={(e) => setAutoScroll(e.target.checked)}
                  className="accent-[#00B4B4] cursor-pointer"/>
              </label>
            </div>
            <button
              className="rounded-md border border-border bg-bg/20 px-3 py-2 text-sm text-text font-semibold hover:bg-panel2 transition"
              onClick={() => refetch()}
            >
              Refresh
            </button>
          </div>

            {/* Log Content */}
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

            <div className="mt-3 text-xs text-muted">
              Phase 1 read-only view. Decisions happen in Screen 6 (Analyst Review).
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}