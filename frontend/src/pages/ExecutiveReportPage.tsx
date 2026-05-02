import React, { useCallback, useEffect, useMemo, useState } from 'react';
import TopNav from '../components/common/TopNav';
import LoadingPanel from '../components/common/LoadingPanel';
import ErrorPanel from '../components/common/ErrorPanel';
import { useToast } from '../components/common/Toast';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useNavigate } from 'react-router-dom';
import { useRunContext } from '../context/RunContext';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { RunRequiredEmptyState } from '../components/common/RunRequiredEmptyState';
import { buildPilotRoadmap } from '../utils/buildRoadmap';
import { fetchRunExecutiveReport, type ExecutiveReport } from '../api/runScopedS9S10Api';
import StatCard from '../components/executive_report/StatCard';
import SnapshotMatrix from '../components/executive_report/SnapshotMatrix';
import KeyInsights from '../components/executive_report/KeyInsights';
import TopQuickWins from '../components/executive_report/TopQuickWins';
import PilotRoadmapHighlights from '../components/executive_report/PilotRoadmapHighlights';
import { isRunNotFoundError, runScopedErrorMessage } from '../utils/apiErrors';

export default function ExecutiveReportPage() {
  const { push } = useToast();
  const { opportunities } = useAnalystReviewContext();
  const nav = useNavigate();
  const { runId, clearRunId } = useRunContext();
  const { run, computing } = useDiscoveryRunContext();
  const runStatus = run?.status?.toLowerCase();

  const [report, setReport] = useState<ExecutiveReport | null>(null);
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
      setReport(null);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;

    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await fetchRunExecutiveReport(runId);
        if (!cancelled) setReport(data);
      } catch (e: any) {
        if (cancelled) return;
        if (isRunNotFoundError(e)) {
          clearRunId();
          return;
        }
        setError(runScopedErrorMessage(e, 'Failed to load executive report'));
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

  const roadmap = useMemo(() => buildPilotRoadmap(opportunities), [opportunities]);

  const blockerCount = useMemo(() => {
    const required = roadmap.stages.flatMap(s => s.requiredPermissions).filter(p => p.required);
    const missing = required.filter(p => !p.satisfied);
    const uniq = new Map<string, boolean>();
    for (const p of missing) uniq.set(p.label, true);
    return uniq.size;
  }, [roadmap]);

  const quickWins = useMemo(() => (
    opportunities
      .filter(o => o.tier === 'Quick Win')
      .slice()
      .sort((a, b) => ((b.impact - b.effort) - (a.impact - a.effort)) || (b.impact - a.impact))
      .slice(0, 5)
  ), [opportunities]);

  const pageHeader = (
    <div className="mb-4">
      <div className="text-2xl font-semibold text-text">Executive Report</div>
      <div className="mt-1 text-sm text-muted">
        Internal Demo Gate stub: exports are toasts; narrative is hardcoded.
      </div>
    </div>
  );

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          <RunRequiredEmptyState
            pageTitle="Executive Report"
            pageDescription="Internal Demo Gate stub: exports are toasts; narrative is hardcoded."
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
            title="Loading Executive Report"
            subtitle="Waiting for executive report results to become available for this discovery run."
          />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <ErrorPanel message={error} onRetry={refetch} title="Failed to load executive report" />
      </div>
    );
  }

  // sourcesAnalyzed comes from run.inputs (run-scoped) via the API
  const sourcesAnalyzed = report?.sourcesAnalyzed;
  const sourcesLabel = sourcesAnalyzed
    ? `${sourcesAnalyzed.totalConnected} Connected`
    : '— Connected';

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="w-full px-8 py-6 pb-10">

        <div className="mb-3 flex items-start justify-between">
          <div>
            <div className="text-2xl font-semibold">Executive Report</div>
            <div className="mt-1 text-sm text-muted">
              Internal Demo Gate stub: exports are toasts; narrative is hardcoded.
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <button
              className="rounded-lg border border-border bg-buttonbg px-4 py-2 text-sm font-medium text-text hover:bg-panel"
              onClick={() => push('Downloading PDF...')}
            >
              Download PDF
            </button>

            <button
              className="rounded-lg border border-border bg-buttonbg  px-4 py-2 text-sm font-medium text-text hover:bg-panel"
              onClick={() => push('Downloading PPTX...')}
            >
              Download PPTX
            </button>

            <button
              className="rounded-lg border border-border bg-buttonbg  px-4 py-2 text-sm font-medium text-text hover:bg-panel"
              onClick={() => push('Downloading XLSX...')}
            >
              Download XLSX
            </button>
          </div>
        </div>

        <div className="mb-4 rounded-xl bg-panel px-4 py-3 text-sm text-muted">
          Overview of confidence, sources, and prioritized quick wins across the Agent Roadmap.
        </div>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <StatCard title="Overall Confidence" value={roadmap.overallReadiness} />
          <StatCard title="Sources Analyzed" value={sourcesLabel} />
          <StatCard title="Top Opportunities" value={`${quickWins.length} Quick Wins`} />
          <StatCard title="Agent Roadmap" value="Phase 1/2/3" />
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
          <div className="space-y-4">
            <KeyInsights />
            <SnapshotMatrix opportunities={opportunities} />
          </div>

          <div className="space-y-4">
            <TopQuickWins quickWins={quickWins} />
            <PilotRoadmapHighlights
              stages={roadmap.stages}
              blockerCount={blockerCount}
              overallReadiness={roadmap.overallReadiness}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
