import React, { useCallback, useEffect, useMemo, useState } from 'react';
import PageShell from '../components/common/PageShell';
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
    <PageShell
      title="Executive Report"
      description="Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness."
    >
      <LoadingPanel
        title="Loading Executive Report"
        subtitle="Waiting for executive report results to become available for this discovery run."
      />
    </PageShell>
  );

  if (!runId) {
    return (
      <PageShell
        title="Executive Report"
        description="Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness."
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
        title="Executive Report"
        description="Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness."
      >
        <ErrorPanel message={error} onRetry={refetch} title="Failed to load executive report" />
      </PageShell>
    );
  }

  // sourcesAnalyzed comes from run.inputs (run-scoped) via the API
  const sourcesAnalyzed = report?.sourcesAnalyzed;
  const sourcesLabel = sourcesAnalyzed
    ? `${sourcesAnalyzed.totalConnected} Connected`
    : '— Connected';

  const reportConfidence = report?.confidence
    ? report.confidence.charAt(0).toUpperCase() + report.confidence.slice(1).toLowerCase()
    : 'Unavailable';
  const roadmapStageLabel = roadmap.stages.length
    ? roadmap.stages.map(stage => stage.title).join(' / ')
    : '—';

  return (
    <PageShell
      title="Executive Report"
      description="Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness."
      actions={
          <>
            <button
              className="rounded-lg border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
              onClick={() => push('Downloading PDF...')}
            >
              Download PDF
            </button>

            <button
              className="rounded-lg border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
              onClick={() => push('Downloading PPTX...')}
            >
              Download PPTX
            </button>

            <button
              className="rounded-lg border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
              onClick={() => push('Downloading XLSX...')}
            >
              Download XLSX
            </button>
          </>
      }
    >

        <div className="mb-4 rounded-xl border border-border bg-panel px-4 py-3 text-sm text-muted">
          Overview of confidence, source coverage, and prioritized quick wins across the Agent Roadmap.
        </div>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <StatCard title="Overall Confidence" value={reportConfidence} />
          <StatCard title="Sources Analyzed" value={sourcesLabel} />
          <StatCard title="Top Opportunities" value={`${quickWins.length} Quick Wins`} />
          <StatCard title="Agent Roadmap" value={roadmapStageLabel} />
        </div>

        <div className="mt-4 space-y-4">
          {/* Key Insights — full width */}
          <KeyInsights />

          {/* Top Quick Wins + Agent Roadmap Highlights — side by side */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(420px,520px)]">
            <TopQuickWins quickWins={quickWins} />
            <PilotRoadmapHighlights
              stages={roadmap.stages}
              blockerCount={blockerCount}
              overallReadiness={roadmap.overallReadiness}
            />
          </div>

          {/* Effort vs Impact matrix — full width */}
          <SnapshotMatrix opportunities={opportunities} />
        </div>
    </PageShell>
  );
}
