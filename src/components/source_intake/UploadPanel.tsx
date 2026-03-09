import React from "react";
import { Upload, FileText, FolderPlus } from "lucide-react";
import { UploadedFile } from "../../types/upload";

export default function UploadPanel({
  files,
  onBrowse,
  onRemove,
  onUploadFolder
}: {
  files: UploadedFile[];
  onBrowse: () => void;
  onRemove: (id: string) => void;
  onUploadFolder: () => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-5">

      
      <h2 className="text-lg font-semibold text-text mb-4">
        Upload Your Files
      </h2>

    
      <div className="border-2 border-dashed border-border rounded-lg p-6 text-center bg-bg/40">

        <Upload className="mx-auto mb-3 text-muted" size={28} />

        <div className="text-sm text-text mb-3">
          Drag & drop files here
        </div>

        <button
          onClick={onBrowse}
          className="px-4 py-2 text-sm rounded-md border border-border bg-panel hover:bg-bg text-text"
        >
          Browse my computer
        </button>

      </div>

      
      <div
        onClick={onUploadFolder}
        className="flex items-center gap-2 mt-3 text-sm text-text cursor-pointer hover:text-primary"
      >
        <FolderPlus size={16} />
        Upload a folder of CSV/Excel files
      </div>

      <div className="mt-4 border-t border-border pt-3 space-y-3">

        {files.length === 0 ? (
          <div className="text-sm text-muted">
            No files uploaded yet.
          </div>
        ) : (
          files.map((file) => (
            <div
              key={file.id}
              className="flex items-center justify-between border-b border-border pb-3 bg-panel/30"
            >
              <div className="flex items-center gap-3">

                <FileText size={20} className="text-muted" />

                <div>
                  <div className="text-sm font-medium text-text">
                    {file.name}
                  </div>

                  <div className="text-xs text-muted">
                    {file.sizeLabel} · {file.uploadedLabel}
                  </div>
                </div>

              </div>

              <button
                onClick={() => onRemove(file.id)}
                className="text-sm text-muted hover:text-red-400"
              >
                Remove
              </button>
            </div>
          ))
        )}

      </div>


      <button
        onClick={onUploadFolder}
        className="mt-4 w-full flex items-center justify-center gap-2 border border-border rounded-md py-2 text-sm bg-panel hover:bg-bg text-text"
      >
        <FolderPlus size={16} />
        Upload a folder of CSV/Excel files
      </button>

   
      <p className="text-xs text-muted mt-3">
        Begin Discovery enabled when ≥1 source connected or ≥1 file uploaded.
      </p>

    </div>
  );
}