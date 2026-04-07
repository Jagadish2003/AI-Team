import React, { useMemo } from 'react';
import TopNav from '../components/common/TopNav';
import { useToast } from '../components/common/Toast';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useConnectorContext } from '../context/ConnectorContext';
import { useNavigate } from 'react-router-dom';
import { useRunContext } from '../context/RunContext';
import { RunRequiredEmptyState } from '../components/common/RunRequiredEmptyState';
import { buildPilotRoadmap } from '../utils/buildRoadmap';
import StatCard from '../components/executive_report/StatCard';
import SnapshotMatrix from '../components/executive_report/SnapshotMatrix';
import KeyInsights from '../components/executive_report/KeyInsights';
import TopQuickWins from '../components/executive_report/TopQuickWins';
import PilotRoadmapHighlights from '../components/executive_report/PilotRoadmapHighlights';

export default function ExecutiveReportPage() {
  const { push } = useToast();
  const { opportunities } = useAnalystReviewContext();
  const { connectors } = useConnectorContext();
  const nav = useNavigate();
  const { runId } = useRunContext();

  const connectedCount = useMemo(
    () => connectors.filter(c => c.tier === 'recommended' && c.status === 'connected').length,
    [connectors]
  );

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

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          <RunRequiredEmptyState onStart={() => nav('/discovery-run')} />
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="w-full px-8 py-6 pb-10">

        {/* ✅ HEADER */}
        <div className="mb-3 flex items-start justify-between">
          
          {/* LEFT */}
          <div>
            <div className="text-2xl font-semibold">Executive Report</div>
            <div className="mt-1 text-sm text-muted">
              Internal Demo Gate stub: exports are toasts; narrative is hardcoded.
            </div>
          </div>

          {/* RIGHT BUTTONS (FIXED) */}
          <div className="flex items-center gap-2 shrink-0">
            <button
              className="rounded-lg border border-border bg-panel2 px-4 py-2 text-sm font-medium text-text hover:bg-panel"
              onClick={() => push('Downloading PDF...')}
            >
              Download PDF
            </button>

            <button
              className="rounded-lg border border-border bg-panel2 px-4 py-2 text-sm font-medium text-text hover:bg-panel"
              onClick={() => push('Downloading PPTX...')}
            >
              Download PPTX
            </button>

            <button
              className="rounded-lg border border-border bg-panel2 px-4 py-2 text-sm font-medium text-text hover:bg-panel"
              onClick={() => push('Downloading XLSX...')}
            >
              Download XLSX
            </button>
          </div>
        </div>

        {/* INFO BAR */}
        <div className="mb-4 rounded-xl bg-panel px-4 py-3 text-sm text-muted">
          Overview of confidence, sources, and prioritized quick wins across the roadmap.
        </div>

        {/* STAT CARDS */}
        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <StatCard title="Overall Confidence" value={roadmap.overallReadiness} />
          <StatCard title="Sources Analyzed" value={`${connectedCount} Connected`} />
          <StatCard title="Top Opportunities" value={`${quickWins.length} Quick Wins`} />
          <StatCard title="Pilot Roadmap" value="30/60/90 Days" />
        </div>

        {/* MAIN CONTENT */}
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