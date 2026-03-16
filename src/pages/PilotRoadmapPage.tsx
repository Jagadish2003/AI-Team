import React, { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import PilotRoadmapHeader from '../components/pilot_roadmap/PilotRoadmapHeader';
import RoadmapSummaryBar from '../components/pilot_roadmap/RoadmapSummaryBar';
import StagesGrid from '../components/pilot_roadmap/StagesGrid';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useToast } from '../components/common/Toast';
import { buildPilotRoadmap } from '../utils/buildRoadmap';

export default function PilotRoadmapPage() {
  const { opportunities, select } = useAnalystReviewContext();
  const model = useMemo(() => buildPilotRoadmap(opportunities), [opportunities]);
  const { push } = useToast();
  const nav = useNavigate();

  const openReview = (id: string) => {
    select(id);
    nav('/analyst-review');
  };

  return (
    <div className="min-h-screen text-text">
      <TopNav />
      <div className="w-full px-8 py-6 pb-10">
        <PilotRoadmapHeader
          onExport={() => push('Export will be wired in Screen 10 (stub).')}
        />
        <RoadmapSummaryBar model={model} />
        <StagesGrid stages={model.stages} onOpenReview={openReview} />
      </div>
    </div>
  );
}