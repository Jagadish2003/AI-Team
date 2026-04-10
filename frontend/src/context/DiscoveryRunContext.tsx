/**
 * DiscoveryRunContext — run-scoped API wiring for Screen 3 (Task 11 Phase 1).
 * Requires RunContext (runId persistence) to already exist.
 */
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { DiscoveryRun, RunEvent, RunInputs } from '../types/discoveryRun';
import { fetchRun, fetchRunEvents, replayRun, startRun as apiStartRun } from '../api/runApi';
import { useRunContext } from './RunContext';

type DiscoveryRunContextValue = {
  run: DiscoveryRun | null;
  events: RunEvent[];
  loading: boolean;
  error: string | null;
  started: boolean;
  startRun: (inputs: RunInputs) => Promise<void>;
  restartRun: () => Promise<void>;
  refetch: () => void;
};

const Ctx = createContext<DiscoveryRunContextValue | null>(null);

export function DiscoveryRunProvider({ children }: { children: React.ReactNode }) {
  const { runId, setRunId } = useRunContext();
  const [run, setRun] = useState<DiscoveryRun | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const [fetchCount, setFetchCount] = useState(0);

  const refetch = useCallback(() => setFetchCount((c) => c + 1), []);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [r, ev] = await Promise.all([fetchRun(runId), fetchRunEvents(runId)]);
        if (cancelled) return;
        setRun(r);
        setEvents(ev);
        setStarted(true);
      } catch (e: any) {
        if (cancelled) return;
        setError(e?.message ?? 'Failed to load run');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [runId, fetchCount]);

  const startRun = useCallback(async (inputs: RunInputs) => {
    if (runId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiStartRun(inputs);
      setRunId(res.runId);
      // load() runs via the effect once runId is set.
    } catch (e: any) {
      setError(e?.message ?? 'Failed to start run');
      setLoading(false);
    }
  }, [setRunId, runId]);

  const restartRun = useCallback(async () => {
    if (!runId) return;
    setLoading(true);
    setError(null);
    try {
      await replayRun(runId);
      refetch();
    } catch (e: any) {
      setError(e?.message ?? 'Failed to replay run');
    } finally {
      setLoading(false);
    }
  }, [runId, refetch]);

  const value = useMemo(
    () => ({ run, events, loading, error, started, startRun, restartRun, refetch }),
    [run, events, loading, error, started, startRun, restartRun, refetch]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDiscoveryRunContext() {
  const v = useContext(Ctx);
  if (!v) throw new Error('useDiscoveryRunContext must be used inside DiscoveryRunProvider');
  return v;
}
