import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import PilotRoadmapHeader from '../components/pilot_roadmap/PilotRoadmapHeader';
import RoadmapSummaryBar from '../components/pilot_roadmap/RoadmapSummaryBar';
import StagesGrid from '../components/pilot_roadmap/StagesGrid';
import LoadingPanel from '../components/common/LoadingPanel';
import ErrorPanel from '../components/common/ErrorPanel';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
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

  const [model, setModel] = useState<PilotRoadmapModel | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState(0);

  const refetch = useCallback(() => setFetchCount(c => c + 1), []);

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
            pageTitle="Pilot Roadmap"
            pageDescription="30/60/90-day plan. Pilots fail due to access - this page makes readiness explicit."
            onStart={() => nav('/discovery-run')}
          />
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <LoadingPanel title="Loading pilot roadmap…" />
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
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="w-full px-8 py-6 pb-10">

        <PilotRoadmapHeader
          onExport={() => push('Export will be wired in Screen 10 (stub).')}
        />

        <div className="mt-4">
          <RoadmapSummaryBar model={model} />
        </div>

        <div className="mt-6">
          <StagesGrid
            stages={model.stages}
            onOpenReview={openReview}
          />
        </div>

      </div>
    </div>
  );
}
