import React, { useState } from 'react';
import { DiscoveryRun } from '../../types/discoveryRun';
 
function row(label: string, value: string) {
  return (
    <div className="flex items-center justify-between border-b border-border/60 py-2 text-sm">
      <div className="text-muted">{label}</div>
      <div className="font-semibold text-text">{value}</div>
    </div>
  );
}
 
function ConfidenceBars({ level }: { level: string }) {
  const barShades: Record<string, string[]> = {
    LOW:    ['bg-gray-200', 'bg-gray-600', 'bg-gray-700', 'bg-gray-800'],
    MEDIUM: ['bg-gray-200', 'bg-gray-300', 'bg-gray-700', 'bg-gray-800'],
    HIGH:   ['bg-gray-200', 'bg-gray-200', 'bg-gray-200', 'bg-gray-300']
  };
  const shades = barShades[level] || barShades.LOW;
  return (
    <div className="flex items-center gap-2">
      <span className="font-semibold text-text">{level}</span>
      <div className="flex gap-1">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className={`h-3 w-6 rounded-sm border border-border ${shades[i]}`} />
        ))}
      </div>
    </div>
  );
}
 
export default function RunSummaryPanel({
  run,
  onViewPartial,
  onViewNormalization,
  onDownload,
  onRestart
}: {
  run: DiscoveryRun;
  onViewPartial: () => void;
  onViewNormalization: () => void;
  onDownload: () => void;
  onRestart: () => void;
}) {
  const [showDownloadModal, setShowDownloadModal] = useState(false);
 
  const canViewPartial = run.progress.percent >= 42;
  const canDownload = run.status === 'COMPLETED';
  const normalizationStep = run.steps.find((step) => step.id === 'normalize');
  const canViewNormalization = normalizationStep?.status !== 'PENDING';
 
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xl font-semibold text-text">Run Summary</div>
 
      <div className="mt-3 rounded-lg border border-border bg-bg/20 p-3">
        <div className="text-xs font-semibold text-text">Sources Feeding Discovery Run</div>
        <ul className="mt-2 space-y-1 text-sm text-muted">
          {run.inputs.connectedSources.map((s) => (
            <li key={s}>• {s}</li>
          ))}
          {run.inputs.uploadedFiles.length > 0 && (
            <li>• {run.inputs.uploadedFiles.length} Uploaded Files</li>
          )}
          {run.inputs.sampleWorkspaceEnabled && <li>• Sample Workspace</li>}
        </ul>
      </div>
 
      <div className="mt-4">
        {row('Apps Detected', String(run.summary.appsDetected))}
        {row('Workflows Inferred', String(run.summary.workflowsInferred))}
        {row('Opportunities Found', String(run.summary.opportunitiesFound))}
        <div className="flex items-center justify-between border-b border-border/60 py-2 text-sm">
          <div className="text-muted">Confidence</div>
          <ConfidenceBars level={run.summary.confidence} />
        </div>
      </div>
 
      <div className="mt-4 space-y-2">
        <button
          className="w-full rounded-lg border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
          onClick={onViewNormalization}
          disabled={!canViewNormalization}
          title={!canViewNormalization ? 'Available after Normalization starts' : undefined}
        >
          Normalization
        </button>
        <button
          className="w-full rounded-lg border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
          onClick={onViewPartial}
          disabled={!canViewPartial}
          title={!canViewPartial ? 'Available after Entity Extraction starts' : undefined}
        >
          View Partial Results
        </button>
        <button
          className="w-full rounded-lg border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
          disabled={!canDownload}
          onClick={() => {
            setShowDownloadModal(true);
            setTimeout(() => setShowDownloadModal(false), 2200);
          }}
        >
          Download Summary
        </button>
        <button
          className="w-full rounded-lg border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60"
          onClick={onRestart}
        >
          Restart Run (mock)
        </button>
      </div>
 
      {showDownloadModal && (
        <div
          className="fixed bottom-6 right-6 z-50 max-w-sm cursor-pointer rounded-md border border-border bg-panel px-3 py-2 text-sm text-text shadow"
          onClick={() => setShowDownloadModal(false)}
        >
          Download export will be enabled in a later sprint.
        </div>
      )}
    </div>
  );
}
 