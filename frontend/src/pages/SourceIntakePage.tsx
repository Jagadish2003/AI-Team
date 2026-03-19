import React, { useMemo, useRef } from 'react';
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

  const singleFileInputRef = useRef<HTMLInputElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement | null>(null);

  // ✅ Use unified source
  const { all } = useConnectorContext();

  const {
    uploadedFiles,
    sampleWorkspaceEnabled,
    addMockFile,
    addFilesFromSelection,
    removeFile,
    setSampleWorkspaceEnabled
  } = useSourceIntakeContext();

  // ✅ Get all connected sources (no duplication issues)
  const connected = useMemo(
    () => all.filter((c) => c.status === 'connected'),
    [all]
  );

  // ✅ Names (clean list)
  const connectedNames = useMemo(
    () => [...new Set(connected.map((c) => c.name))],
    [connected]
  );

  // ✅ Total sources count (IMPORTANT FIX)
  const totalSources = useMemo(() => {
    let total = connected.length + uploadedFiles.length;
    if (sampleWorkspaceEnabled) total += 3; // sample sources count
    return total;
  }, [connected.length, uploadedFiles.length, sampleWorkspaceEnabled]);

  const canBegin =
    connected.length > 0 || uploadedFiles.length > 0 || sampleWorkspaceEnabled;

  const handleSingleFileSelected = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []);
    if (selected.length === 0) return;

    const firstFile = selected[0];
    const { addedCount } = addFilesFromSelection([firstFile]);

    if (addedCount > 0) {
      push(`Added ${firstFile.name}.`);
    } else {
      push('Only CSV or Excel files are allowed.');
    }

    event.target.value = '';
  };

  const handleFolderSelected = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []);
    if (selected.length === 0) return;

    const allAreValid = selected.every((file) => {
      const lower = file.name.toLowerCase();
      return (
        lower.endsWith('.csv') ||
        lower.endsWith('.xls') ||
        lower.endsWith('.xlsx')
      );
    });

    if (!allAreValid) {
      push('Folder upload accepts only CSV or Excel files.');
      event.target.value = '';
      return;
    }

    const { addedCount } = addFilesFromSelection(selected);
    push(`Added ${addedCount} file${addedCount === 1 ? '' : 's'} from folder.`);

    event.target.value = '';
  };

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      {/* Hidden Inputs */}
      <input
        ref={singleFileInputRef}
        type="file"
        accept=".csv,.xls,.xlsx"
        className="hidden"
        onChange={handleSingleFileSelected}
      />

      <input
        ref={folderInputRef}
        type="file"
        accept=".csv,.xls,.xlsx"
        multiple
        className="hidden"
        onChange={handleFolderSelected}
        {...({ webkitdirectory: '', directory: '' } as React.InputHTMLAttributes<HTMLInputElement>)}
      />

      <div className="w-full px-8 py-6 pb-28">
        {/* Header */}
        <div className="mb-6">
          <div className="text-2xl font-semibold">Source Intake</div>
          <div className="mt-1 text-sm text-muted">
            <span className="font-semibold text-text">{connected.length}</span>{' '}
            sources already connected via Integration Hub. Add files for additional coverage.
          </div>
        </div>

        {/* Panels */}
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
          <UploadPanel
            files={uploadedFiles}
            onBrowse={() => singleFileInputRef.current?.click()}
            onAddMock={() => {
              addMockFile();
              push('Added a mock upload file.');
            }}
            onRemove={(id) => {
              removeFile(id);
              push('Removed file.');
            }}
            onUploadFolder={() => folderInputRef.current?.click()}
          />

          <ManagedAgentPanel
            onDownload={() =>
              push('Agent download available in Sprint 2 (mocked).')
            }
            onGuide={() =>
              push('Installation guide available in Sprint 2 (mocked).')
            }
          />

          <SampleWorkspacePanel
            enabled={sampleWorkspaceEnabled}
            onEnable={() => {
              setSampleWorkspaceEnabled(true);
              push('Sample workspace enabled.');
            }}
            onLearnMore={() =>
              push('More details available in Sprint 2.')
            }
          />
        </div>

        {/* Summary */}
        <div className="mt-4">
          <ReadySourcesSummary
            connectedNames={connectedNames}
            fileCount={uploadedFiles.length}
            sampleEnabled={sampleWorkspaceEnabled}
            totalSources={totalSources}   // ✅ FIXED COUNT
          />

          <div className="mt-2 text-xs text-muted">
            Begin Discovery is enabled when ≥1 source connected OR ≥1 file uploaded OR Sample Workspace selected.
          </div>
        </div>

        {/* Footer */}
        <div className="fixed bottom-0 left-0 right-0 z-40 border-t border-border bg-bg/90 shadow-[0_-2px_6px_rgba(0,0,0,0.12)] backdrop-blur">
          <div className="mx-auto flex w-full max-w-none items-center justify-between gap-2 px-8 py-3">
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