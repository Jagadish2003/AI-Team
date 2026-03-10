import React from 'react';
import { Upload, FileText, FolderPlus, CirclePlus } from 'lucide-react';
import { UploadedFile } from '../../types/upload';

export default function UploadPanel({
  files,
  onBrowse,
  onAddMock,
  onRemove,
  onUploadFolder
}: {
  files: UploadedFile[];
  onBrowse: () => void;
  onAddMock: () => void;
  onRemove: (id: string) => void;
  onUploadFolder: () => void;
}) {
  return (
    <div className="w-full max-w-md rounded-xl border border-border bg-panel p-6">
      <h2 className="mb-5 text-xl font-semibold text-text">Upload Your Files</h2>

      <div className="mb-5 flex flex-col items-center rounded-lg border border-dashed border-border bg-bg/20 px-6 py-10 text-center">
        <Upload className="mb-4 text-muted/60" size={32} />
        <div className="mb-4 text-sm text-text">Drag & drop files here</div>

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
            <div className="flex h-full flex-col items-center justify-center p-4 text-sm italic text-muted">
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
                      <div className="truncate text-sm font-semibold leading-tight text-text">
                        {file.name}
                      </div>
                      <div className="mt-1 text-[11px] text-muted">
                        {file.sizeLabel} · {file.uploadedLabel}
                      </div>
                    </div>
                  </div>

                  <button
                    onClick={() => onRemove(file.id)}
                    className="ml-4 text-xs font-bold text-muted transition-colors hover:text-white"
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <button
        onClick={onUploadFolder}
        className="mt-5 flex w-full items-center justify-center gap-2 rounded-md border border-border bg-panel/40 py-2.5 text-sm text-text transition-colors hover:bg-panel"
      >
        <FolderPlus size={16} />
        Upload a folder of CSV/Excel files
      </button>

      <p className="mt-4 text-[10px] leading-tight text-muted/60">
        Begin Discovery enabled when ≥1 source connected or ≥1 file uploaded.
      </p>
    </div>
  );
}