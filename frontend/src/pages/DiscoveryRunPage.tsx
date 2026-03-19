import React, { useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import PipelineStepper from '../components/discovery_run/PipelineStepper';
import RunLogPanel from '../components/discovery_run/RunLogPanel';
import RunSummaryPanel from '../components/discovery_run/RunSummaryPanel';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useConnectorContext } from '../context/ConnectorContext';
import { useSourceIntakeContext } from '../context/SourceIntakeContext';

export default function DiscoveryRunPage() {
  const { run, events, autoScroll, setAutoScroll, started, startRun, restartRun } = useDiscoveryRunContext();
  const { connectors } = useConnectorContext();
  const { uploadedFiles, sampleWorkspaceEnabled } = useSourceIntakeContext();
  const nav = useNavigate();

  const connectedNames = useMemo(
    () => connectors.filter((c) => c.status === 'connected').map((c) => c.name),
    [connectors]
  );
  const uploadedNames = useMemo(() => uploadedFiles.map((f) => f.name), [uploadedFiles]);

  useEffect(() => {
    if (started) return;
    const inputs = {
      connectedSources: connectedNames,
      uploadedFiles: uploadedNames,
      sampleWorkspaceEnabled,
      totalSources: connectedNames.length + uploadedNames.length + (sampleWorkspaceEnabled ? 1 : 0)
    };
    startRun(inputs);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [started]);

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="w-full px-8 py-6 pb-10">
        <div className="mb-6 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="text-2xl font-semibold">Discovery Run</div>
            <div className="mt-1 text-sm text-muted">
              Run ID: <span className="font-semibold text-text">{run.runId}</span>
            </div>
          </div>
          <div
            className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
              run.status === 'COMPLETED'
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
                : run.status === 'FAILED'
                  ? 'border-danger/40 bg-danger/10 text-danger'
                  : 'border-accent/40 bg-accent/10 text-text'
            }`}
          >
            <span className="h-2 w-2 rounded-full bg-current opacity-80" />
            {run.status === 'RUNNING' ? 'RUNNING…' : run.status}
            <span className="text-muted">·</span>
            <span>{run.progress.percent}%</span>
          </div>
        </div>

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <PipelineStepper steps={run.steps} />
          <RunLogPanel events={events} autoScroll={autoScroll} onToggleAutoScroll={setAutoScroll} />
          <RunSummaryPanel
            run={run}
            onViewPartial={() => nav('/partial-results')}
            onViewNormalization={() => nav('/normalization')}
            onDownload={() => {}}
            onRestart={() => restartRun()}
          />
        </div>
      </div>
    </div>
  );
}