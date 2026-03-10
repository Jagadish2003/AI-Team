import React from 'react';
import Button from '../common/Button';
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
    LOW: [
      'bg-gray-200',
      'bg-gray-600',
      'bg-gray-700',
      'bg-gray-800'
    ],
    MEDIUM: [
      'bg-gray-200',
      'bg-gray-300',
      'bg-gray-700',
      'bg-gray-800'
    ],
    HIGH: [
      'bg-gray-200',
      'bg-gray-200',
      'bg-gray-200',
      'bg-gray-300'
    ]
  };

  const shades = barShades[level] || barShades.LOW;

  return (
    <div className="flex items-center gap-2">
      <span className="font-semibold text-text">{level}</span>

      <div className="flex gap-1">
        {[0,1,2,3].map((i)=>(
          <div
            key={i}
            className={`h-3 w-6 rounded-sm border border-border ${shades[i]}`}
          />
        ))}
      </div>
    </div>
  );
}
export default function RunSummaryPanel({
  run,
  onViewPartial,
  onDownload,
  onRestart
}: {
  run: DiscoveryRun;
  onViewPartial: () => void;
  onDownload: () => void;
  onRestart: () => void;
}) {
  const canViewPartial = run.progress.percent >= 42;
  const canDownload = run.status === 'COMPLETED';

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Run Summary</div>

      <div className="mt-3 rounded-lg border border-border bg-bg/20 p-3">
        <div className="text-xs font-semibold text-text">Sources Feeding Discovery Run</div>

        <ul className="mt-2 space-y-1 text-sm text-muted">
          {run.inputs.connectedSources.map((s) => (
            <li key={s}>• {s}</li>
          ))}

          {run.inputs.uploadedFiles.length > 0 && <li>• {run.inputs.uploadedFiles.length} Uploaded Files</li>}
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
        <Button
          className="w-full"
          variant="secondary"
          onClick={onViewPartial}
          disabled={!canViewPartial}
          title={!canViewPartial ? 'Available after Entity Extraction starts' : undefined}
        >
          View Partial Results
        </Button>

        <Button
          className="w-full"
          variant="secondary"
          onClick={onDownload}
          disabled={!canDownload}
          title={!canDownload ? 'Available when run completes' : undefined}
        >
          Download Summary
        </Button>

        <Button className="w-full" variant="ghost" onClick={onRestart}>
          Restart Run (mock)
        </Button>
      </div>
    </div>
  );
}