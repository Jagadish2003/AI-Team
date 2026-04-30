import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { DiscoveryRun, RunEvent, RunInputs } from '../types/discoveryRun';
import { fetchRun, fetchRunEvents, replayRun, startRun as apiStartRun, fetchRunStatus } from '../api/runApi';
import { useRunContext } from './RunContext';
import { isRunNotFoundError, runScopedErrorMessage } from '../utils/apiErrors';

type DiscoveryRunContextValue = {
  run: DiscoveryRun | null;
  events: RunEvent[];
  loading: boolean;
  error: string | null;
  started: boolean;
  computing: boolean;
  startRun: (inputs: RunInputs) => Promise<void>;
  restartRun: () => Promise<void>;
  refetch: () => void;
};

const Ctx = createContext<DiscoveryRunContextValue | null>(null);

function isTerminalStatus(status: string | undefined) {
  const normalized = status?.toLowerCase();
  return normalized === 'complete' || normalized === 'completed' || normalized === 'partial' || normalized === 'failed';
}

function sameEvents(a: RunEvent[], b: RunEvent[]) {
  if (a.length !== b.length) return false;
  const lastA = a[a.length - 1];
  const lastB = b[b.length - 1];
  return (lastA?.id ?? lastA?.tsLabel ?? lastA?.message) === (lastB?.id ?? lastB?.tsLabel ?? lastB?.message);
}

export function DiscoveryRunProvider({ children }: { children: React.ReactNode }) {
  const { runId, setRunId, clearRunId } = useRunContext();
  const [run, setRun] = useState<DiscoveryRun | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const [computing, setComputing] = useState(false);
  const [fetchCount, setFetchCount] = useState(0);

  const startingRef = useRef(false);
  const refetch = useCallback(() => setFetchCount((c) => c + 1), []);

  useEffect(() => {
    if (!runId) {
      setRun(null);
      setEvents([]);
      setStarted(false);
      setComputing(false);
      setError(null);
      setLoading(false);
      return;
    }

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
        setComputing(!isTerminalStatus(r.status));
      } catch (e: any) {
        if (cancelled) return;
        if (isRunNotFoundError(e)) {
          clearRunId();
          return;
        }
        setError(runScopedErrorMessage(e, 'Failed to load run'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, fetchCount, clearRunId]);

  useEffect(() => {
    if (!runId || !computing) return;
    let cancelled = false;
    const pollRunProgress = async () => {
      try {
        const [statusPayload, latestEvents] = await Promise.all([
          fetchRunStatus(runId),
          fetchRunEvents(runId),
        ]);
        if (cancelled) return;
        const { status } = statusPayload;
        setEvents((prev) => (sameEvents(prev, latestEvents) ? prev : latestEvents));
        setRun((prev) => (prev ? { ...prev, status: status as DiscoveryRun['status'] } : prev));
        if (isTerminalStatus(status)) {
          setComputing(false);
          setFetchCount((c) => c + 1);
        }
      } catch (e: any) {
        if (isRunNotFoundError(e)) {
          clearRunId();
          setComputing(false);
        }
      }
    };
    void pollRunProgress();
    const interval = setInterval(pollRunProgress, 1500);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [runId, computing, clearRunId]);

  const startRun = useCallback(async (inputs: RunInputs) => {
    if (runId || startingRef.current) return;
    startingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const res = await apiStartRun(inputs);
      setRunId(res.runId);
      setComputing(true);
    } catch (e: any) {
      setError(runScopedErrorMessage(e, 'Failed to start run'));
      setLoading(false);
    } finally {
      startingRef.current = false;
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
      setError(runScopedErrorMessage(e, 'Failed to replay run'));
    } finally {
      setLoading(false);
    }
  }, [runId, refetch]);

  const value = useMemo(
    () => ({ run, events, loading, error, started, computing, startRun, restartRun, refetch }),
    [run, events, loading, error, started, computing, startRun, restartRun, refetch]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDiscoveryRunContext() {
  const v = useContext(Ctx);
  if (!v) throw new Error('useDiscoveryRunContext must be used inside DiscoveryRunProvider');
  return v;
}
