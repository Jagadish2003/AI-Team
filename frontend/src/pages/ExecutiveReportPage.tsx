import React, { useCallback, useEffect, useMemo, useState } from 'react';
import PackCertificationBadge from '../components/common/PackCertificationBadge';
import { Loader2 } from 'lucide-react';
import PageShell from '../components/common/PageShell';
import { Skeleton, SkeletonStatCard } from '../components/common/Skeleton';
import ErrorPanel from '../components/common/ErrorPanel';
import { useToast } from '../components/common/Toast';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useNavigate } from 'react-router-dom';
import { useRunContext } from '../context/RunContext';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useAuthOptional } from '../context/AuthContext';
import { useOrgName } from '../context/LicenseContext';
import { RunRequiredEmptyState } from '../components/common/RunRequiredEmptyState';
import { buildPilotRoadmap } from '../utils/buildRoadmap';
import { fetchRunExecutiveReport, type ExecutiveReport } from '../api/runScopedS9S10Api';
import { fetchRunEnrichment, type RunEnrichment } from '../api/enrichmentApi';
import StatCard from '../components/executive_report/StatCard';
import SnapshotMatrix from '../components/executive_report/SnapshotMatrix';
import KeyInsights, { resolveExecutiveSummary } from '../components/executive_report/KeyInsights';
import TopQuickWins from '../components/executive_report/TopQuickWins';
import PilotRoadmapHighlights from '../components/executive_report/PilotRoadmapHighlights';
import ExecutiveOutcomeSection from '../components/outcomes/ExecutiveOutcomeSection';
import { downloadExecutiveReportPdf } from '../utils/exportPdf';
import { profileNameFromEmail } from '../utils/profileName';
import { runScopedErrorMessage } from '../utils/apiErrors';
import { useResource, useDataCache } from '../lib/dataCache';
import { cacheKeys } from '../lib/cacheKeys';
import { showRelease2ArcAUi } from '../config/releaseFlags';

export default function ExecutiveReportPage() {
  const { push } = useToast();
  const { opportunities } = useAnalystReviewContext();
  const nav = useNavigate();
  const { runId } = useRunContext();
  const { run, computing } = useDiscoveryRunContext();
  const auth = useAuthOptional();
  // R17-D4 Addendum A §2 / T13 — customer-facing reports carry the organisation
  // name resolved from the license (the same name shown in the header, via T12),
  // not the ad-hoc auth org_name, so exports carry the correct organisation
  // identity and stay consistent with the rest of the product.
  const orgName = useOrgName();
  const runStatus = run?.status?.toLowerCase();
  const cache = useDataCache();

  const [pdfBusy, setPdfBusy] = useState(false);

  // Report + enrichment via the shared cache. Both sit under the run scope, so a
  // decision/override in Opportunity Review (which invalidates 'runs/{runId}')
  // refreshes this report live. Enrichment on cacheKeys.runEnrichment is shared
  // with the Key Insights card — one fetch, not two.
  const {
    data: reportData,
    loading: reportLoading,
    error: reportErrObj,
    refetch,
  } = useResource<ExecutiveReport>(
    runId ? cacheKeys.runExecutiveReport(runId) : null,
    () => fetchRunExecutiveReport(runId as string),
  );
  const report = reportData ?? null;
  const error = reportErrObj
    ? runScopedErrorMessage(reportErrObj, 'Failed to load executive report')
    : null;
  // Gate "loading" on not-yet-loaded so the first render doesn't flash content.
  const loading = reportLoading || (reportData === undefined && reportErrObj === null);

  // Non-blocking: a failed enrichment fetch leaves the static fallback in place.
  const { data: enrichmentData } = useResource<RunEnrichment>(
    runId ? cacheKeys.runEnrichment(runId) : null,
    () => fetchRunEnrichment(runId as string),
  );
  const enrichment = enrichmentData ?? null;

  const runHasMaterializedResults =
    runStatus === 'complete' || runStatus === 'completed' || runStatus === 'partial';
  const resultsPreparing =
    computing ||
    (Boolean(run) && !runHasMaterializedResults) ||
    /still being prepared/i.test(error ?? '');

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

  // Display values shared by the page and the PDF export.
  const sourcesLabel = report?.sourcesAnalyzed
    ? `${report.sourcesAnalyzed.totalConnected} Connected`
    : '— Connected';
  const reportConfidence = report?.confidence
    ? report.confidence.charAt(0).toUpperCase() + report.confidence.slice(1).toLowerCase()
    : 'Unavailable';
  const packCertifications = report?.packCertifications ?? [];
  const roadmapStageLabel = roadmap.stages.length
    ? roadmap.stages.map((_, i) => `Phase ${i + 1}`).join(' / ')
    : '—';

  const handleDownloadPdf = useCallback(async () => {
    if (pdfBusy) return;
    setPdfBusy(true);
    push('Preparing executive report PDF…');
    try {
      const stamp = new Date().toISOString().slice(0, 10);
      const generatedAt = new Date().toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'long',
        day: 'numeric',
      });
      // Ensure the PDF uses the same enrichment summary the UI shows. The page's
      // enrichment fetch is async, so on a fast click it may not have resolved
      // yet — fetch it now (and cache it) so the PDF never silently falls back to
      // the static summary while the on-screen Key Insights shows the real one.
      let enrichmentForPdf = enrichment;
      if (!enrichmentForPdf && runId) {
        try {
          enrichmentForPdf = await fetchRunEnrichment(runId);
          // Populate the shared cache so the on-screen Key Insights card reflects
          // it too (same runEnrichment key).
          cache.setData(cacheKeys.runEnrichment(runId), enrichmentForPdf);
        } catch {
          enrichmentForPdf = null; // genuinely unavailable → static fallback
        }
      }
      await downloadExecutiveReportPdf(
        {
          confidence: reportConfidence,
          sourcesLabel,
          quickWinsCount: quickWins.length,
          roadmapStageLabel,
          summary: resolveExecutiveSummary(enrichmentForPdf),
          quickWins,
          stageCounts: roadmap.stages.map((s) => s.opportunities.length),
          blockerCount,
          overallReadiness: roadmap.overallReadiness,
          opportunities,
          orgName,
          userName: profileNameFromEmail(auth?.user?.email),
          generatedAt,
          runId,
          packCertifications: report?.packCertifications ?? [],
          outcomeSection: showRelease2ArcAUi ? report?.outcomeSection ?? null : null,
        },
        {
          filename: `AgentIQ-Executive-Report-${stamp}.pdf`,
          footerText: 'AgentIQ Executive Report — Confidential',
        },
      );
      push('Executive report downloaded.', 'success');
    } catch (e) {
      console.error('[ExecutiveReport] PDF export failed:', e);
      push('Could not generate the PDF. Please try again.', 'error');
    } finally {
      setPdfBusy(false);
    }
  }, [
    pdfBusy,
    push,
    reportConfidence,
    sourcesLabel,
    roadmapStageLabel,
    quickWins,
    enrichment,
    roadmap,
    blockerCount,
    opportunities,
    auth,
    orgName,
    runId,
    cache,
  ]);

  const pageHeader = (
    <PageShell
      title="Executive Report"
      description="Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness."
    >
      {/* Skeleton mirrors the summary bar + 4-card stat row + report sections so
          the real content fills the same space with no layout shift. */}
      <div aria-busy="true" aria-label="Loading Executive Report">
        <Skeleton className="mb-4 h-12 w-full" />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <SkeletonStatCard />
          <SkeletonStatCard />
          <SkeletonStatCard />
          <SkeletonStatCard />
        </div>
        <div className="mt-4 space-y-4">
          <Skeleton className="h-40 w-full" />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(420px,520px)]">
            <Skeleton className="h-64 w-full" />
            <Skeleton className="h-64 w-full" />
          </div>
          <Skeleton className="h-72 w-full" />
        </div>
      </div>
    </PageShell>
  );

  if (!runId) {
    return (
      <PageShell
        title="Executive Report"
        description="Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness."
      >
        <RunRequiredEmptyState onStart={() => nav('/integration-hub')} />
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

  return (
    <PageShell
      title="Executive Report"
      description="Board-ready summary of source coverage, confidence, opportunity value, and implementation readiness."
      actions={
          <>
            <button
              type="button"
              className="inline-flex items-center gap-2 rounded-lg border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-70"
              onClick={handleDownloadPdf}
              disabled={pdfBusy}
              aria-busy={pdfBusy}
            >
              {pdfBusy && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
              {pdfBusy ? 'Generating PDF…' : 'Download PDF'}
            </button>

            <button
              type="button"
              className="cursor-not-allowed rounded-lg border border-border bg-transparent px-4 py-2 text-sm font-medium text-muted opacity-60"
              disabled
              aria-disabled
              title="PPTX export is not available yet"
            >
              Download PPTX
            </button>

            <button
              type="button"
              className="cursor-not-allowed rounded-lg border border-border bg-transparent px-4 py-2 text-sm font-medium text-muted opacity-60"
              disabled
              aria-disabled
              title="XLSX export is not available yet"
            >
              Download XLSX
            </button>
          </>
      }
    >

        <div className="mb-4 rounded-xl border border-border bg-panel px-4 py-3 text-sm text-muted">
          Overview of confidence, source coverage, and prioritized quick wins across the Agent Roadmap.
          {/* 2.0-C2 T3 (AT-833 / AC2): which level of pack produced the claims in
              this report. On-screen counterpart of the same line in the PDF, so the
              exported and viewed report say the same thing. */}
          {packCertifications.length > 0 ? (
            <div data-testid="report-pack-certifications" className="mt-2 flex flex-wrap items-center gap-2">
              <span className="text-xs">Produced by</span>
              {packCertifications.map((item) => (
                <span key={item.packId} className="flex items-center gap-1.5">
                  <span className="font-mono text-xs text-text">{item.packId}</span>
                  <PackCertificationBadge
                    level={item.level}
                    label={item.label}
                    reviewDue={item.reviewDue}
                    reviewDueDetail={item.reviewDueDetail}
                    testId={`report-pack-certification-${item.packId}`}
                  />
                </span>
              ))}
            </div>
          ) : null}
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

          {showRelease2ArcAUi && (
            <ExecutiveOutcomeSection section={report?.outcomeSection ?? null} />
          )}

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
