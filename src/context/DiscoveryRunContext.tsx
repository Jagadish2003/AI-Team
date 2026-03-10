import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import baseRun from '../data/mockDiscoveryRun.json';
import baseEvents from '../data/mockRunEvents.json';
import { DiscoveryRun, RunEvent, StepStatus } from '../types/discoveryRun';

export type RunInputsLike = DiscoveryRun['inputs'];

type DiscoveryRunContextValue = {
  run: DiscoveryRun;
  events: RunEvent[];
  autoScroll: boolean;
  started: boolean;

  setAutoScroll: (v: boolean) => void;
  startRun: (inputs: RunInputsLike) => void;
  restartRun: () => void;
};

const Ctx = createContext<DiscoveryRunContextValue | null>(null);

function nowIso() { return new Date().toISOString(); }
function clamp(n: number, a: number, b: number) { return Math.max(a, Math.min(b, n)); }

function stageMessage(stepId: string, percent: number): { level: 'INFO' | 'WARNING'; message: string } {
  const pool: Record<string, string[]> = {
    ingest: [
      'Validating source inventory and permissions…',
      'Parsing file exports and API metadata…',
      'Extracting ticket timelines and audit trails…',
      'Indexing documents for evidence retrieval…'
    ],
    normalize: [
      'Normalizing to common schema (Applications, Roles, Actions)…',
      'Linking CMDB records to ticket components…',
      'Mapping Jira transitions to workflow steps…',
      'Resolving duplicates across sources…'
    ],
    extract: [
      'Extracting entities (apps, workflows, roles, data objects)…',
      'Detecting handoffs and approvals from timelines…',
      'Clustering recurring incidents by theme…',
      'Tagging regulated keywords and compliance signals…'
    ],
    opportunities: [
      'Detecting high-friction loops (triage, routing, approvals)…',
      'Finding repeat questions that indicate a copilot opportunity…',
      'Identifying bounce patterns across 5+ owners…',
      'Flagging manual rework and exception handling hotspots…'
    ],
    score: [
      'Scoring opportunities on feasibility, impact, speed, risk…',
      'Computing confidence based on evidence coverage…',
      'Estimating quick wins vs strategic bets…',
      'Preparing executive-ready prioritization list…'
    ],
    draft: [
      'Drafting report sections from validated evidence…',
      'Generating initial opportunity map summary…',
      'Compiling findings into deliverable format…',
      'Finalizing run summary for review…'
    ]
  };

  const list = pool[stepId] ?? ['Processing…'];
  const idx = Math.floor(percent / 7) % list.length;
  const message = list[idx];

  const warn = (stepId === 'ingest' && percent > 10 && percent < 25) || (stepId === 'normalize' && percent > 35 && percent < 45);
  return warn
    ? { level: 'WARNING', message: 'Encountered minor data quality gaps — continuing with best-effort normalization.' }
    : { level: 'INFO', message };
}

export function DiscoveryRunProvider({ children }: { children: React.ReactNode }) {
  const [run, setRun] = useState<DiscoveryRun>(baseRun as unknown as DiscoveryRun);
  const [events, setEvents] = useState<RunEvent[]>(baseEvents as unknown as RunEvent[]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [started, setStarted] = useState(false);
  const timerRef = useRef<number | null>(null);

  const thresholds = [
    { id: 'ingest', at: 0, doneAt: 18 },
    { id: 'normalize', at: 18, doneAt: 42 },
    { id: 'extract', at: 42, doneAt: 60 },
    { id: 'opportunities', at: 60, doneAt: 78 },
    { id: 'score', at: 78, doneAt: 92 },
    { id: 'draft', at: 92, doneAt: 100 }
  ] as const;

  const stopTimer = useCallback(() => {
    if (timerRef.current) window.clearInterval(timerRef.current);
    timerRef.current = null;
  }, []);

  const tick = useCallback(() => {
    setRun(prev => {
      if (prev.status !== 'RUNNING') return prev;

      const percent = clamp(prev.progress.percent + 6, 0, 100);
      const updatedAt = nowIso();

      const steps = prev.steps.map(s => {
        const t = thresholds.find(x => x.id === s.id)!;
        const status: StepStatus =
          percent >= t.doneAt ? 'DONE' :
          percent >= t.at ? 'RUNNING' :
          'PENDING';
        return { ...s, status };
      });

      const appsDetected = percent < 42 ? Math.floor(percent / 6) : 12;
      const workflowsInferred = percent < 60 ? Math.floor(Math.max(0, percent - 42) / 6) : 5;
      const opportunitiesFound = percent < 78 ? 0 : (percent < 92 ? 7 : 14);
      const confidence = percent < 30 ? 'LOW' : (percent < 75 ? 'MEDIUM' : 'HIGH');
      const warnings = 1;

      const status = percent >= 100 ? 'COMPLETED' : 'RUNNING';
      const current = thresholds.find(t => percent < t.doneAt) ?? thresholds[thresholds.length - 1];

      return {
        ...prev,
        status,
        updatedAt,
        progress: { percent, currentStepId: current.id, etaSeconds: Math.max(0, prev.progress.etaSeconds - 20) },
        steps,
        summary: { appsDetected, workflowsInferred, opportunitiesFound, confidence, warnings }
      };
    });

    setEvents(prev => {
      const ts = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      const stageOrder = ['ingest','normalize','extract','opportunities','score','draft'] as const;
      const idx = Math.floor(prev.length / 4) % stageOrder.length;
      const stepId = stageOrder[idx];
      const percentApprox = clamp((prev.length * 6) % 100, 0, 100);
      const msg = stageMessage(stepId, percentApprox);
      return [...prev, { ts, level: msg.level as any, message: msg.message }];
    });
  }, []);

  const startTimer = useCallback(() => {
    stopTimer();
    timerRef.current = window.setInterval(tick, 2000);
  }, [tick, stopTimer]);

  const startRun = useCallback((inputs: RunInputsLike) => {
    if (started) return;

    const seeded = (baseRun as unknown as DiscoveryRun);
    setRun({
      ...seeded,
      runId: 'RUN-' + String(Math.floor(Math.random() * 900) + 100),
      status: 'RUNNING',
      startedAt: nowIso(),
      updatedAt: nowIso(),
      inputs,
      progress: { percent: 0, currentStepId: 'ingest', etaSeconds: 300 },
      steps: seeded.steps.map((s, i) => ({ ...s, status: i === 0 ? 'RUNNING' : 'PENDING' })),
      summary: { appsDetected: 0, workflowsInferred: 0, opportunitiesFound: 0, confidence: 'LOW', warnings: 0 }
    });
    setEvents(baseEvents as unknown as RunEvent[]);
    setStarted(true);
    startTimer();
  }, [started, startTimer]);

  const restartRun = useCallback(() => {
    setStarted(false);
    stopTimer();
    const seeded = (baseRun as unknown as DiscoveryRun);
    setRun({ ...seeded, startedAt: nowIso(), updatedAt: nowIso() });
    setEvents(baseEvents as unknown as RunEvent[]);
  }, [stopTimer]);

  useEffect(() => {
    if (run.status === 'COMPLETED') stopTimer();
  }, [run.status, stopTimer]);

  useEffect(() => {
    return () => stopTimer();
  }, [stopTimer]);

  const value = useMemo(() => ({
    run,
    events,
    autoScroll,
    started,
    setAutoScroll,
    startRun,
    restartRun
  }), [run, events, autoScroll, started, startRun, restartRun]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useDiscoveryRunContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useDiscoveryRunContext must be used inside DiscoveryRunProvider');
  return ctx;
}
