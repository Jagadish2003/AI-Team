import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import PilotRoadmapHeader from '../components/pilot_roadmap/PilotRoadmapHeader';
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
    <div className="mb-4">
      <div className="text-2xl font-semibold text-text">Agent Roadmap</div>
      <div className="mt-1 text-sm text-muted">
        Your prioritised Agentforce implementation plan - grounded in discovery findings.
      </div>
    </div>
  );

  const openReview = (id: string) => {
    select(id);
    nav(runId ? `/analyst-review?runId=${runId}` : '/analyst-review');
  };

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          <RunRequiredEmptyState
            pageTitle="Agent Roadmap"
            pageDescription="Your prioritised Agentforce implementation plan - grounded in discovery findings."
            onStart={() => nav('/discovery-run')}
          />
        </div>
      </div>
    );
  }

  if (loading || resultsPreparing) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          {pageHeader}
          <LoadingPanel
            title="Loading Agent Roadmap"
            subtitle="Waiting for roadmap results to become available for this discovery run."
          />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <ErrorPanel message={error} onRetry={refetch} title="Failed to load roadmap" />
      </div>
    );
  }

  if (!model) return null;

  return (
    <div className="min-h-screen text-text lg:h-screen lg:overflow-hidden">
      <TopNav />

      <div className="w-full px-8 py-6 lg:flex lg:h-[calc(100vh-70px)] lg:flex-col lg:overflow-hidden">

        <PilotRoadmapHeader
          onExport={() => push('Export will be wired in Screen 10 (stub).')}
        />

        <div className="mt-4 shrink-0">
          <RoadmapSummaryBar model={model} />
        </div>

        <div className="mt-2 lg:min-h-0 lg:flex-1 lg:overflow-hidden">
          <StagesGrid
            stages={model.stages}
            onOpenReview={openReview}
          />
        </div>

      </div>
    </div>
  );
}
