import React from 'react';
 
interface Props { onExport: () => void; }
 
export default function PilotRoadmapHeader({ onExport }: Props) {
  return (
    <div className="mb-3 flex items-start justify-between gap-3">
      <div>
        <div className="text-2xl font-semibold">Agent Roadmap</div>
        <div className="mt-1 text-sm text-muted">
          Your prioritised AI agent implementation plan — grounded in discovery findings.
        </div>
      </div>
 
      <button
        className="rounded-lg border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        onClick={onExport}
      >
        Export Report
      </button>
    </div>
  );
}
