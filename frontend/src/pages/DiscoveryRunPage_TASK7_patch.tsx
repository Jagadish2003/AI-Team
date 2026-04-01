/**
 * TASK 7 PATCH — DiscoveryRunPage.tsx (reference implementation)
 *
 * - Read runId from RunContext
 * - If runId is null: show RunRequiredEmptyState
 * - Start button:
 *    POST /api/runs/start with inputs derived from connected connectors + uploaded files
 *    setRunId(runId) -> updates URL and localStorage
 * - Fetch run + events from API
 *
 * Copy the logic into your real DiscoveryRunPage.tsx.
 */
import React, { useEffect, useState } from "react";
import { useRunContext } from "../context/RunContext";
import { RunRequiredEmptyState } from "../components/common/RunRequiredEmptyState";
import { startRun, fetchRun, fetchRunEvents } from "../services/runApi";
import { useConnectorContext } from "../context/ConnectorContext";
import { useSourceIntakeContext } from "../context/SourceIntakeContext";

export function DiscoveryRunPageTask7Patch() {
  const { runId, setRunId } = useRunContext();
  const { connectors } = useConnectorContext();
  const { uploadedFiles, sampleWorkspaceEnabled } = useSourceIntakeContext();

  const [run, setRun] = useState<any>(null);
  const [events, setEvents] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onStart = async () => {
    setLoading(true);
    setError(null);
    try {
      const connectedSources = connectors.filter((c: any) => c.status === "connected").map((c: any) => c.name);
      const inputs = {
        connectedSources,
        uploadedFiles: uploadedFiles.map((f: any) => f.name ?? f.fileName ?? String(f)),
        sampleWorkspaceEnabled: !!sampleWorkspaceEnabled,
      };
      const res = await startRun(inputs);
      setRunId(res.runId);
    } catch (e: any) {
      setError(e?.message ?? "Failed to start run");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    Promise.all([fetchRun(runId), fetchRunEvents(runId)])
      .then(([r, ev]) => {
        setRun(r);
        setEvents(ev);
      })
      .catch((e: any) => setError(e?.message ?? "Failed to load run"))
      .finally(() => setLoading(false));
  }, [runId]);

  if (!runId) return <RunRequiredEmptyState onStart={onStart} />;
  if (loading) return <div className="text-sm text-muted">Loading run…</div>;
  if (error) return <div className="text-sm text-red-400">{error}</div>;

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-border bg-panel p-4">
        <div className="text-sm text-muted">Run</div>
        <div className="text-base font-semibold text-text">{run?.id ?? runId}</div>
        <div className="text-xs text-muted">Status: {run?.status} · Started: {run?.startedAt}</div>
      </div>

      <div className="rounded-xl border border-border bg-panel p-4">
        <div className="text-sm font-semibold text-text">Events</div>
        <div className="mt-2 space-y-2">
          {events.map((e, idx) => (
            <div key={idx} className="rounded-md border border-border bg-bg/20 px-3 py-2">
              <div className="text-xs text-muted">{e.tsLabel} · {e.stage}</div>
              <div className="text-sm text-text">{e.message}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
