import React from 'react';
import { PilotRoadmapModel } from '../../types/pilotRoadmap';

interface Props { model: PilotRoadmapModel; }

export default function RoadmapSummaryBar({ model }: Props) {
  return (
    <div className="mb-4 rounded-xl border border-border bg-panel p-3 text-sm text-muted">
      Selected:{' '}
      <span className="font-semibold text-text">{model.selectedOpportunityCount}</span>
      <span className="mx-2">|</span>
      Permissions:{' '}
      <span className="font-semibold text-text">{model.requiredPermissionsCount} Required</span>
      <span className="mx-2">|</span>
      Dependencies:{' '}
      <span className="font-semibold text-text">{model.dependencyCount} Identified</span>
      <span className="mx-2">|</span>
      Overall Readiness:{' '}
      <span className="font-semibold text-text">{model.overallReadiness}</span>
    </div>
  );
}