import React, { useMemo } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import TopNav from '../components/common/TopNav';
import UploadPanel from '../components/source_intake/UploadPanel';
import ManagedAgentPanel from '../components/source_intake/ManagedAgentPanel';
import SampleWorkspacePanel from '../components/source_intake/SampleWorkspacePanel';
import ReadySourcesSummary from '../components/source_intake/ReadySourcesSummary';
import Button from '../components/common/Button';
import { useToast } from '../components/common/Toast';
import { useConnectorContext } from '../context/ConnectorContext';
import { useSourceIntakeContext } from '../context/SourceIntakeContext';

export default function SourceIntakePage() {
  const { push } = useToast();
  const nav = useNavigate();

  const { connectors } = useConnectorContext();
  const { uploadedFiles, sampleWorkspaceEnabled, addMockFile, removeFile, setSampleWorkspaceEnabled } =
    useSourceIntakeContext();

  const connected = useMemo(() => connectors.filter((c) => c.status === 'connected'), [connectors]);
  const connectedNames = useMemo(() => connected.map((c) => c.name), [connected]);

  const canBegin = connected.length > 0 || uploadedFiles.length > 0 || sampleWorkspaceEnabled;

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="mx-auto max-w-6xl px-4 pb-28 py-6">
        <div className="mb-6">
          <div className="text-2xl font-semibold">Source Intake</div>
          <div className="mt-1 text-sm text-muted">
            <span className="font-semibold text-text">{connected.length}</span> sources already connected via
            Integration Hub. Add files for additional coverage.
          </div>
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <UploadPanel
            files={uploadedFiles}
            onBrowse={() => push('Browse upload is mocked for Sprint 1. Use “Add a mock file”.')}
            onAddMock={() => {
              addMockFile();
              push('Added a mock upload file.');
            }}
            onRemove={(id) => {
              removeFile(id);
              push('Removed file.');
            }}
            onUploadFolder={() => push('Folder upload available in Sprint 2 (mocked).')}
          />

          <ManagedAgentPanel
            onDownload={() => push('Agent download available in Sprint 2 (mocked).')}
            onGuide={() => push('Installation guide available in Sprint 2 (mocked).')}
          />

          <SampleWorkspacePanel
            enabled={sampleWorkspaceEnabled}
            onEnable={() => {
              setSampleWorkspaceEnabled(true);
              push('Sample workspace enabled.');
            }}
            onLearnMore={() => push('More details available in Sprint 2.')}
          />
        </div>

        <div className="mt-4">
          <ReadySourcesSummary
            connectedNames={connectedNames}
            fileCount={uploadedFiles.length}
            sampleEnabled={sampleWorkspaceEnabled}
          />
          <div className="mt-2 text-xs text-muted">
            Begin Discovery is enabled when ≥1 source connected OR ≥1 file uploaded OR Sample Workspace selected.
          </div>
        </div>

        <div className="fixed bottom-0 left-0 right-0 z-40 border-t border-border bg-bg/90 backdrop-blur">
          <div className="mx-auto flex max-w-6xl items-center justify-between gap-2 px-4 py-3">
            <Button variant="secondary" onClick={() => nav('/integration-hub')}>
              <ChevronLeft size={16} strokeWidth={2.5} />
              Back
            </Button>

            <Button
              variant="primary"
              disabled={!canBegin}
              title={!canBegin ? 'Connect at least one source to continue' : undefined}
              onClick={() => nav('/discovery-run')}
            >
              Begin Discovery
              <ChevronRight size={16} strokeWidth={2.5} />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}