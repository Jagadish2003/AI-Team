import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { DiscoveryRun, RunEvent, RunInputs } from '../types/discoveryRun';
import { fetchRun, fetchRunEvents, replayRun, startRun as apiStartRun, fetchRunStatus } from '../api/runApi';
import { useRunContext } from './RunContext';
import { useConnectorContext } from './ConnectorContext';
import { isRunNotFoundError, runScopedErrorMessage } from '../utils/apiErrors';
import { useRevalidateOnFocus } from '../lib/useRevalidate';

type DiscoveryRunContextValue = {
  run: DiscoveryRun | null;
  events: RunEvent[];
  loading: boolean;
  error: string | null;
  started: boolean;
  computing: boolean;
  // CS-4 T5 / progress checklist: the run's active step and any failed steps,
  // read from the SAME single /status poll below (no separate page-level poller).
  currentStep: string | null;
  failedSteps: string[];
  startRun: (inputs: RunInputs) => Promise<void>;
  restartRun: () => Promise<void>;
  refetch: () => void;
};

const Ctx = createContext<DiscoveryRunContextValue | null>(null);

// How often the single run-progress poll (status + events) fires WHILE a run is
// computing. A discovery run takes minutes, so a calm cadence is plenty for the
// progress bar / step list — the derived data (normalization, opportunities) is
// no longer polled alongside it (it fetches once at completion), so this is the
// only in-run poll. Kept as a named constant so it is easy to tune.
const RUN_PROGRESS_POLL_MS = 5000;

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
  const { refetch: refetchConnectors } = useConnectorContext();
  const [run, setRun] = useState<DiscoveryRun | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [started, setStarted] = useState(false);
  const [computing, setComputing] = useState(false);
  const [currentStep, setCurrentStep] = useState<string | null>(null);
  const [failedSteps, setFailedSteps] = useState<string[]>([]);
  const [fetchCount, setFetchCount] = useState(0);

  const startingRef = useRef(false);
  // The run id whose data we already hold. Used to tell a FIRST load (show the
  // loading state) from a refetch of a run we already have (must stay rendered).
  const loadedRunIdRef = useRef<string | null>(null);
  const refetch = useCallback(() => setFetchCount((c) => c + 1), []);

  // Latest-value refs for the callbacks the effects below call.
  //
  // These must NOT be effect dependencies. RunContext's clearRunId derives from
  // react-router's setSearchParams, which is recreated as the location/search
  // params change — so a new identity re-ran the fetch effect, whose setState
  // re-rendered, which produced yet another identity: an endless run/events/
  // status request storm that saturated the browser's per-origin connection pool
  // and starved every other page's requests. Reading them through refs keeps the
  // effects keyed on their REAL inputs (runId / fetchCount / computing) while
  // still calling the current function.
  const clearRunIdRef = useRef(clearRunId);
  clearRunIdRef.current = clearRunId;
  const refetchConnectorsRef = useRef(refetchConnectors);
  refetchConnectorsRef.current = refetchConnectors;

  // Reset the step indicator whenever the active run changes, so a newly started
  // run never inherits the previous run's last step until its own /status lands.
  useEffect(() => {
    setCurrentStep(null);
    setFailedSteps([]);
  }, [runId]);

  useEffect(() => {
    if (!runId) {
      loadedRunIdRef.current = null;
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
      // Show the loading state ONLY on the first load of this run. This effect
      // also re-runs for every refetch — the terminal-status transition, a
      // replay, and the focus/interval/push revalidation that keeps the org in
      // sync — and those must keep the current run on screen. Flipping loading
      // on them blanked an already-complete run with a full-page loading panel.
      // Mirrors the cache's background-revalidate rule: never show a loading
      // state for data we already have.
      if (loadedRunIdRef.current !== runId) setLoading(true);
      setError(null);
      try {
        const [r, ev, statusPayload] = await Promise.all([
          fetchRun(runId),
          fetchRunEvents(runId),
          fetchRunStatus(runId),
        ]);
        if (cancelled) return;
        const status = (statusPayload.status ?? r.status) as DiscoveryRun['status'];
        // We now hold this run — later refetches revalidate it in the background
        // instead of re-showing the loading state.
        loadedRunIdRef.current = runId;
        setRun({ ...r, status });
        setEvents(ev);
        if (statusPayload.current_step != null) setCurrentStep(statusPayload.current_step);
        setFailedSteps(Array.isArray(statusPayload.failed_steps) ? statusPayload.failed_steps : []);
        setStarted(true);
        const isTerminal = isTerminalStatus(status);
        setComputing(!isTerminal);
        if (isTerminal) refetchConnectorsRef.current();
      } catch (e: any) {
        if (cancelled) return;
        if (isRunNotFoundError(e)) {
          clearRunIdRef.current();
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
  }, [runId, fetchCount]);

  useEffect(() => {
    if (!runId || !computing) return;
    let cancelled = false;
    const pollRunProgress = async () => {
      // Pause polling while the tab is backgrounded — no point hammering the API
      // for a run the user isn't looking at. The visibilitychange listener below
      // fires an immediate poll when the tab is foregrounded again.
      if (typeof document !== 'undefined' && document.hidden) return;
      try {
        const [statusPayload, latestEvents] = await Promise.all([
          fetchRunStatus(runId),
          fetchRunEvents(runId),
        ]);
        if (cancelled) return;
        const { status } = statusPayload;
        if (statusPayload.current_step != null) setCurrentStep(statusPayload.current_step);
        setFailedSteps(Array.isArray(statusPayload.failed_steps) ? statusPayload.failed_steps : []);
        setEvents((prev) => (sameEvents(prev, latestEvents) ? prev : latestEvents));
        setRun((prev) => (prev ? { ...prev, status: status as DiscoveryRun['status'] } : prev));
        if (isTerminalStatus(status)) {
          setComputing(false);
          setFetchCount((c) => c + 1);
          refetchConnectorsRef.current();
        }
      } catch (e: any) {
        if (isRunNotFoundError(e)) {
          clearRunIdRef.current();
          setComputing(false);
        }
      }
    };
    void pollRunProgress();
    const interval = setInterval(pollRunProgress, RUN_PROGRESS_POLL_MS);
    const onVisibility = () => {
      if (typeof document !== 'undefined' && !document.hidden) void pollRunProgress();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [runId, computing]);

  // A run is SHARED across the org — another user may replay it or drive it to a
  // new state. While it is computing the run-progress poll above already tracks
  // it, so this covers the settled case: on focus / a slow tick, pick up a run
  // another user has since changed, without a reload.
  useRevalidateOnFocus(refetch, {
    enabled: Boolean(runId) && !computing,
  });

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
    () => ({ run, events, loading, error, started, computing, currentStep, failedSteps, startRun, restartRun, refetch }),
    [run, events, loading, error, started, computing, currentStep, failedSteps, startRun, restartRun, refetch]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDiscoveryRunContext() {
  const v = useContext(Ctx);
  if (!v) throw new Error('useDiscoveryRunContext must be used inside DiscoveryRunProvider');
  return v;
}
