import React from 'react';
import { RoadmapStage } from '../../types/pilotRoadmap';
import StageCard from './StageCard';

interface Props {
  stages: RoadmapStage[];
  onOpenReview: (id: string) => void;
}

export default function StagesGrid({ stages, onOpenReview }: Props) {
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      {stages.map((s: RoadmapStage) => (
        <div key={s.id} className="space-y-2">
          <div className="text-center text-sm font-bold uppercase tracking-wide text-muted">
            {s.title}
          </div>
          <StageCard stage={s} onOpenReview={onOpenReview} />
        </div>
      ))}
    </div>
  );
}