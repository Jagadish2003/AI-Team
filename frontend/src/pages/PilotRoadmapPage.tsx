import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import PageShell from '../components/common/PageShell';
import RoadmapSummaryBar from '../components/pilot_roadmap/RoadmapSummaryBar';
import StagesGrid from '../components/pilot_roadmap/StagesGrid';
import LoadingPanel from '../components/common/LoadingPanel';
import ErrorPanel from '../components/common/ErrorPanel';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useToast } from '../components/common/Toast';
import { useRunContext } from '../context/RunContext';
import { RunRequiredEmptyState } from '../components/common/RunRequiredEmptyState';
import { fetchRunRoadmap } from '../api/runScopedS9S10Api';
import type { PilotRoadmapModel } from '../types/pilotRoadmap';
import { isRunNotFoundError, runScopedErrorMessage } from '../utils/apiErrors';

export default function PilotRoadmapPage() {
  const { select } = useAnalystReviewContext();
  const { push } = useToast();
  const nav = useNavigate();
  const { runId, clearRunId } = useRunContext();
  const { run, computing } = useDiscoveryRunContext();
  const runStatus = run?.status?.toLowerCase();

  const [model, setModel] = useState<PilotRoadmapModel | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState(0);

  const refetch = useCallback(() => setFetchCount(c => c + 1), []);

  const runHasMaterializedResults =
    runStatus === 'complete' || runStatus === 'completed' || runStatus === 'partial';
  const resultsPreparing =
    computing ||
    (Boolean(run) && !runHasMaterializedResults) ||
    /still being prepared/i.test(error ?? '');

  useEffect(() => {
    if (!runId) {
      setModel(null);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;

    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await fetchRunRoadmap(runId);
        if (!cancelled) setModel(data);
      } catch (e: any) {
        if (cancelled) return;
        if (isRunNotFoundError(e)) {
          clearRunId();
          return;
        }
        setError(runScopedErrorMessage(e, 'Failed to load roadmap'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [runId, fetchCount, clearRunId]);

  useEffect(() => {
    if (!runId || !resultsPreparing || loading) return;
    const timer = window.setTimeout(() => refetch(), 1500);
    return () => window.clearTimeout(timer);
  }, [runId, resultsPreparing, loading, refetch]);

  const pageHeader = (
    <PageShell
      title="Agent Roadmap"
      description="Your prioritised Agentforce implementation plan, grounded in discovery findings."
    >
      <LoadingPanel
        title="Loading Agent Roadmap"
        subtitle="Waiting for roadmap results to become available for this discovery run."
      />
    </PageShell>
  );

  const openReview = (id: string) => {
    select(id);
    nav(runId ? `/opportunity-review?runId=${runId}` : '/opportunity-review');
  };

  if (!runId) {
    return (
      <PageShell
        title="Agent Roadmap"
        description="Your prioritised Agentforce implementation plan, grounded in discovery findings."
      >
        <RunRequiredEmptyState onStart={() => nav('/discovery-run')} />
      </PageShell>
    );
  }

  if (loading || resultsPreparing) {
    return pageHeader;
  }

  if (error) {
    return (
      <PageShell
        title="Agent Roadmap"
        description="Your prioritised Agentforce implementation plan, grounded in discovery findings."
      >
        <ErrorPanel message={error} onRetry={refetch} title="Failed to load roadmap" />
      </PageShell>
    );
  }

  if (!model) return null;

  return (
    <PageShell
      title="Agent Roadmap"
      description="Your prioritised Agentforce implementation plan, grounded in discovery findings."
      actions={
        <button
          className="rounded-lg border border-border bg-buttonbg px-4 py-2 text-sm font-medium text-text hover:bg-panel"
          onClick={() => push('Export will be wired in Screen 10.')}
        >
          Export Report
        </button>
      }
    >
        <div className="shrink-0">
          <RoadmapSummaryBar model={model} />
        </div>

        <div className="mt-2 lg:h-[680px] lg:flex-none">
          <StagesGrid
            stages={model.stages}
            onOpenReview={openReview}
          />
        </div>

    </PageShell>
  );
}
