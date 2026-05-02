/**
 * T41-8 — SourceConfigPanel
 *
 * Merged from SourceIntakePage into the Integration Hub right panel.
 * Shows only production-ready functionality:
 *   - File upload (CSV / Excel) with drag-and-drop
 *   - Uploaded file list with remove
 *
 * Deliberately excluded from this panel (T41-8 decisions):
 *   - "Add mock file" button — engineering testing aid, not user-facing
 *   - SampleWorkspacePanel — demo-framing language removed from product
 *   - ManagedAgentPanel ("Launch Managed Agent") — future capability,
 *     surfaces when customers request it, not on a sprint schedule
 *
 * The "Begin Discovery" trigger remains in DiscoveryStartBar at the
 * bottom of IntegrationHubPage — unchanged from before T41-8.
 */

import React, { useRef } from 'react';
import { Upload, FileText, ChevronDown, ChevronUp } from 'lucide-react';
import { useSourceIntakeContext } from '../../context/SourceIntakeContext';
import { useToast } from '../common/Toast';

const VALID_EXTENSIONS = ['.csv', '.xls', '.xlsx'];

function isValidFile(file: File): boolean {
  const lower = file.name.toLowerCase();
  return VALID_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

export default function SourceConfigPanel() {
  const { push } = useToast();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [expanded, setExpanded] = React.useState(false);

  const {
    uploadedFiles,
    addFilesFromSelection,
    removeFile,
  } = useSourceIntakeContext();

  const handleBrowse = () => fileInputRef.current?.click();

  const handleFileSelected = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files ?? []);
    if (selected.length === 0) return;
    const { addedCount } = addFilesFromSelection(selected);
    if (addedCount > 0) {
      push(`Added ${addedCount} file${addedCount === 1 ? '' : 's'}.`);
    } else {
      push('Only CSV or Excel files are supported.');
    }
    e.target.value = '';
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const collected: File[] = [];

    const readEntry = (entry: any): Promise<void> => {
      if (entry.isFile) {
        return new Promise((resolve) => {
          entry.file((file: File) => { collected.push(file); resolve(); });
        });
      }
      if (entry.isDirectory) {
        return new Promise((resolve) => {
          const reader = entry.createReader();
          reader.readEntries(async (entries: any[]) => {
            for (const child of entries) await readEntry(child);
            resolve();
          });
        });
      }
      return Promise.resolve();
    };

    for (const item of Array.from(e.dataTransfer.items)) {
      const entry = item.webkitGetAsEntry?.();
      if (entry) await readEntry(entry);
    }

    const valid = collected.filter(isValidFile);
    if (valid.length === 0) {
      push('Only CSV or Excel files are supported.');
      return;
    }
    const { addedCount } = addFilesFromSelection(valid);
    push(`Added ${addedCount} file${addedCount === 1 ? '' : 's'}.`);
  };

  const hasFiles = uploadedFiles.length > 0;

  return (
    <div className="mt-3 rounded-xl border border-border bg-panel overflow-hidden">

      {/* Collapsible header */}
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-panel2 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Upload size={14} className="text-accent shrink-0" />
          <span className="text-sm font-medium text-text">
            Upload Additional Data
          </span>
          {hasFiles && (
            <span className="rounded-full bg-accent/20 px-2 py-0.5 text-[10px] font-semibold text-accent">
              {uploadedFiles.length}
            </span>
          )}
        </div>
        {expanded
          ? <ChevronUp size={14} className="text-muted shrink-0" />
          : <ChevronDown size={14} className="text-muted shrink-0" />
        }
      </button>

      {/* Expandable body */}
      {expanded && (
        <div className="border-t border-border px-4 pb-4 pt-3">

          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.xls,.xlsx"
            multiple
            className="hidden"
            onChange={handleFileSelected}
          />

          {/* Drop zone */}
          <div
            className="mb-3 flex flex-col items-center rounded-lg border border-dashed border-border bg-bg/20 px-4 py-5 text-center cursor-pointer hover:border-accent/40 hover:bg-bg/30 transition-colors"
            onDragOver={handleDragOver}
            onDrop={handleDrop}
            onClick={handleBrowse}
          >
            <Upload className="mb-2 text-muted/50" size={22} />
            <p className="text-xs text-muted">
              Drag & drop or{' '}
              <span className="text-accent font-medium">browse</span>
            </p>
            <p className="mt-1 text-[10px] text-muted/60">
              CSV, XLS, XLSX
            </p>
          </div>

          {/* File list */}
          {hasFiles && (
            <div className="rounded-lg border border-border overflow-hidden">
              <div className="max-h-[160px] overflow-y-auto divide-y divide-border scrollbar-thin scrollbar-thumb-slate-800">
                {uploadedFiles.map((file) => (
                  <div
                    key={file.id}
                    className="flex items-center gap-3 px-3 py-2 bg-panel/10 hover:bg-panel/20 transition-colors"
                  >
                    <FileText size={14} className="shrink-0 text-muted/60" />
                    <div className="flex-1 min-w-0">
                      <div className="truncate text-xs font-medium text-text">
                        {file.name}
                      </div>
                      <div className="text-[10px] text-muted">
                        {file.sizeLabel} · {file.uploadedLabel}
                      </div>
                    </div>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        removeFile(file.id);
                        push('File removed.');
                      }}
                      className="shrink-0 rounded border border-red-500/30 px-2 py-0.5 text-[10px] text-red-400 hover:border-red-400/60 hover:text-red-300 transition-colors"
                    >
                      Remove
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {!hasFiles && (
            <p className="text-center text-[10px] text-muted/50 mt-2">
              Uploaded files count as a data source for discovery.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
