import React, { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import PilotRoadmapHeader from '../components/pilot_roadmap/PilotRoadmapHeader';
import RoadmapSummaryBar from '../components/pilot_roadmap/RoadmapSummaryBar';
import StagesGrid from '../components/pilot_roadmap/StagesGrid';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useToast } from '../components/common/Toast';
import { buildPilotRoadmap } from '../utils/buildRoadmap';
import { useRunContext } from '../context/RunContext';
import { RunRequiredEmptyState } from '../components/common/RunRequiredEmptyState';

export default function PilotRoadmapPage() {
  const { opportunities, select } = useAnalystReviewContext();
  const model = useMemo(() => buildPilotRoadmap(opportunities), [opportunities]);
  const { push } = useToast();
  const nav = useNavigate();
  const { runId } = useRunContext();

  const openReview = (id: string) => {
    select(id);
    nav('/analyst-review');
  };

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

      {/* ✅ SAME GLOBAL LAYOUT SYSTEM */}
      <div className="w-full px-8 py-6 pb-10">

        {/* ✅ HEADER */}
        <PilotRoadmapHeader
          onExport={() => push('Export will be wired in Screen 10 (stub).')}
        />

        {/* ✅ SUMMARY BAR */}
        <div className="mt-4">
          <RoadmapSummaryBar model={model} />
        </div>

        {/* ✅ STAGES GRID */}
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