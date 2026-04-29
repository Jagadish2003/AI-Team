import React from 'react';
import { Upload, FileText, CirclePlus } from 'lucide-react';
import { UploadedFile } from '../../types/upload';

export default function UploadPanel({
  files,
  onBrowse,
  onAddMock,
  onRemove,
  onDrop,
}: {
  files: UploadedFile[];
  onBrowse: () => void;
  onAddMock: () => void;
  onRemove: (id: string) => void;
  onDrop: (files: File[]) => void;
}) {
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
          entry.file((file: File) => {
            collected.push(file);
            resolve();
          });
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
    const items = Array.from(e.dataTransfer.items);
    for (const item of items) {
      const entry = item.webkitGetAsEntry?.();
      if (entry) await readEntry(entry);
    }
    if (collected.length > 0) onDrop(collected);
  };

  return (
    <div className="w-full max-w-md rounded-xl border border-border bg-panel p-6 flex flex-col">
      <h2 className="mb-5 text-xl font-semibold text-text">Upload Your Files</h2>

      <div
        className="mb-5 flex flex-col items-center rounded-lg border border-dashed border-border bg-bg/20 px-6 py-10 text-center"
        onDragOver={handleDragOver}
        onDrop={handleDrop}
      >
        <Upload className="mb-4 text-muted/60" size={32} />
        <div className="mb-4 text-sm text-text">Drag & drop files or folders here</div>

        <div className="flex flex-wrap justify-center gap-2">
          <button
            onClick={onBrowse}
            className="rounded-md border border-border bg-panel px-6 py-2 text-sm text-text transition-colors hover:bg-bg/50"
          >
            Browse my computer
          </button>

          <button
            onClick={onAddMock}
            className="inline-flex items-center gap-2 rounded-md border border-border bg-panel px-4 py-2 text-sm text-text transition-colors hover:bg-bg/50"
          >
            <CirclePlus size={14} />
            Add a mock file
          </button>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-border bg-bg/5">
        <div className="h-[216px] overflow-y-auto scrollbar-thin scrollbar-thumb-slate-800 scrollbar-track-transparent">
          {files.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center p-4 text-sm text-muted">
              <FileText size={32} className="mb-2 opacity-20" />
              No files uploaded yet.
            </div>
          ) : (
            <div className="divide-y divide-border">
              {files.map((file) => (
                <div
                  key={file.id}
                  className="flex items-center justify-between bg-panel/10 p-4 transition-colors hover:bg-panel/20"
                >
                  <div className="flex min-w-0 items-center gap-4">
                    <FileText size={24} className="shrink-0 text-muted/70" />

                    <div className="flex min-w-0 flex-col">
                      <div className="truncate text-sm font-semibold leading-tight text-text">{file.name}</div>
                      <div className="mt-1 text-[11px] text-muted">
                        {file.sizeLabel} {'\u00b7'} {file.uploadedLabel}
                      </div>
                    </div>
                  </div>

                  <button
                    onClick={() => onRemove(file.id)}
                    className="ml-3 shrink-0 rounded-md border border-red-500/40 bg-gradient-to-b from-red-500/15 via-red-500/5 to-red-500/15 px-3 py-1 text-xs font-semibold text-red-400 transition-all duration-200 hover:border-red-400/60 hover:from-red-500/25 hover:to-red-500/25 hover:text-red-300 hover:shadow-[0_0_10px_rgba(239,68,68,0.2)] active:scale-95"
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="flex flex-1 min-h-[90px] items-center justify-center">
        <p className="text-center text-[10px] leading-tight text-muted/60">
          Begin Discovery also stays available when at least one file is uploaded.
        </p>
      </div>
    </div>
  );
}
