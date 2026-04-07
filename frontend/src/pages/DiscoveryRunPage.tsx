import React, { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import TopNav from "../components/common/TopNav";
import PipelineStepper from "../components/discovery_run/PipelineStepper";
import RunLogPanel from "../components/discovery_run/RunLogPanel";
import RunSummaryPanel from "../components/discovery_run/RunSummaryPanel";

import { useDiscoveryRunContext } from "../context/DiscoveryRunContext";
import { useConnectorContext } from "../context/ConnectorContext";
import { useSourceIntakeContext } from "../context/SourceIntakeContext";
import { useRunContext } from "../context/RunContext";

import { startRun as apiStartRun, fetchRun as apiGetRun, fetchRunEvents as apiGetRunEvents } from "../api/runApi";
import type { DiscoveryRun as BackendRun, RunEvent as BackendEvent } from "../types/discoveryRun";

export default function DiscoveryRunPage() {
  const {
    run,
    events,
    autoScroll,
    setAutoScroll,
    startRun,
    restartRun,
  } = useDiscoveryRunContext();

  const { all } = useConnectorContext();
  const { uploadedFiles, sampleWorkspaceEnabled } = useSourceIntakeContext();
  const nav = useNavigate();

  const { runId, setRunId, clearRunId } = useRunContext();

  const [backendRun, setBackendRun] = useState<BackendRun | null>(null);
  const [backendEvents, setBackendEvents] = useState<BackendEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const connectedNames = useMemo(
    () =>
      [...new Set(all.filter((c) => c.status === "connected").map((c) => c.name))],
    [all]
  );

  const uploadedNames = useMemo(
    () => uploadedFiles.map((f) => f.name),
    [uploadedFiles]
  );

  const handleStart = async () => {
    setError(null);

    try {
      setLoading(true);
      restartRun();
      setBackendRun(null);
      setBackendEvents([]);

      const res = await apiStartRun({
        connectedSources: connectedNames,
        uploadedFiles: uploadedNames,
        sampleWorkspaceEnabled,
      });
      setRunId(res.runId);

    } catch (e) {
      console.error(e);
      setError("Failed to start run");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    if (!runId) return;

    restartRun(); 

    startRun({
      connectedSources: connectedNames,
      uploadedFiles: uploadedNames,
      sampleWorkspaceEnabled,
      totalSources:
        connectedNames.length +
        uploadedNames.length +
        (sampleWorkspaceEnabled ? 1 : 0),
    });

  }, [runId]); 

  useEffect(() => {
    if (!runId) {
      setBackendRun(null);
      setBackendEvents([]);
      return;
    }

    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setError(null);

      try {
        const [runRes, eventsRes] = await Promise.all([
          apiGetRun(runId),
          apiGetRunEvents(runId),
        ]);

        if (!cancelled) {
          setBackendRun(runRes);
          setBackendEvents(eventsRes || []);
        }
      } catch (err: any) {
        console.error(err);

        if (err?.response?.status === 404) {
          clearRunId();
        } else {
          setError("Failed to load run data");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();

    return () => {
      cancelled = true;
    };
  }, [runId, clearRunId]);

 if (!runId) {
  return (
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="flex items-center justify-center h-[80vh]">
        <div className="w-[470px] rounded-xl border border-white/20 bg-panel p-8 py-12 text-center shadow-xl shadow-black/20  ">  
          <h2 className="text-lg font-semibold mb-2">
            No Active Run
          </h2>
          <p className="text-sm text-muted mb-6">
            Start a new discovery run to continue.
          </p>
          <button
            onClick={handleStart}
            disabled={loading}
            className="px-5 py-2 text-sm font-medium text-white bg-accent rounded-md hover:opacity-90 disabled:opacity-50 transition"
          >
            {loading ? "Starting..." : "Start New Discovery Run"}
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
        <div className="mb-6 flex justify-between items-center">
          <div>
            <h1 className="text-xl font-semibold">Discovery Run</h1>
            <p className="text-sm text-muted mt-3">
              Run ID: <span className="text-white">{runId}</span>
            </p>

            {backendRun?.startedAt && (
              <p className="text-xs text-muted mt-2">
                Started: {new Date(backendRun.startedAt).toLocaleString()}
              </p>
            )}
          </div>

          <div className="flex gap-2">
            <button
              onClick={() => {
                clearRunId();
                restartRun();
                setBackendRun(null);
                setBackendEvents([]);
              }}
              className="px-4 py-2 text-sm font-medium text-text bg-slate-700 rounded hover:bg-slate-600 transition"
            >
              Clear Run
            </button>

            <button
              onClick={handleStart}
              disabled={loading}
              className="px-4 py-2 text-sm font-medium text-white bg-accent rounded hover:opacity-90 disabled:opacity-50 transition"
            >
              New Run
            </button>
          </div>
        </div>

        {/* PROGRESS */}
        <div className="flex justify-end mt-2">
          <div className="flex items-center gap-2 px-3 py-3 rounded-full bg-accent/20 text-white text-xs font-medium">
            
            {/* Dot */}
            <span className="w-2 h-2 rounded-full bg-accent animate-pulse"></span>

            {/* Status */}
            <span>
              {run.status === "RUNNING" ? "RUNNING..." : run.status}
            </span>

            {/* Percentage */}
            <span className="opacity-80">
              {run.progress.percent}%
            </span>

          </div>
        </div>

        {/* STATUS */}
        <div className="mb-6 text-md">
          {backendRun?.status && <div>Status: {backendRun.status}</div>}
          {loading && <div>Loading...</div>}
          {error && <div className="text-red-400">{error}</div>}
        </div>

        {/* MAIN */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <PipelineStepper steps={run.steps} />

          <RunLogPanel
            events={events}
            autoScroll={autoScroll}
            onToggleAutoScroll={setAutoScroll}
          />

          <RunSummaryPanel
            run={run}
            onViewPartial={() => nav(`/partial-results?runId=${runId}`)}
            onViewNormalization={() => nav(`/normalization?runId=${runId}`)}
            onDownload={() => {}}
            onRestart={() => restartRun()}
          />
        </div>

        <div className="mt-6 text-xs text-muted">
          Backend Events: {backendEvents.length}
        </div>
      </div>
    </div>
  );
}