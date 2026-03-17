import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import uploadsMock from '../data/mockUploads.json';
import { UploadedFile } from '../types/upload';

type SourceIntakeContextValue = {
  uploadedFiles: UploadedFile[];
  sampleWorkspaceEnabled: boolean;

  addMockFile: () => void;
  addFilesFromSelection: (files: File[]) => { addedCount: number; rejectedCount: number };
  removeFile: (id: string) => void;
  setSampleWorkspaceEnabled: (v: boolean) => void;
};

const Ctx = createContext<SourceIntakeContextValue | null>(null);

const ALLOWED_EXTENSIONS = ['.csv', '.xls', '.xlsx'];

function isAllowedSpreadsheetFile(file: File) {
  const lower = file.name.toLowerCase();
  return ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext));
}

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;

  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  return `${value >= 100 ? Math.round(value) : value.toFixed(value >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

export function SourceIntakeProvider({ children }: { children: React.ReactNode }) {
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>(uploadsMock as unknown as UploadedFile[]);
  const [sampleWorkspaceEnabled, setSampleWorkspaceEnabled] = useState<boolean>(false);

  const removeFile = useCallback((id: string) => {
    setUploadedFiles((prev) => prev.filter((f) => f.id !== id));
  }, []);

  const addMockFile = useCallback(() => {
    setUploadedFiles((prev) => {
      const n = prev.length + 1;
      const id = `file_${String(n).padStart(3, '0')}`;
      return [
        ...prev,
        {
          id,
          name: `extra_upload_${n}.csv`,
          sizeLabel: '420 KB',
          uploadedLabel: 'Uploaded just now'
        }
      ];
    });
  }, []);

  const addFilesFromSelection = useCallback((files: File[]) => {
    const accepted = files.filter(isAllowedSpreadsheetFile);
    const rejectedCount = files.length - accepted.length;

    if (accepted.length === 0) {
      return { addedCount: 0, rejectedCount };
    }

    setUploadedFiles((prev) => {
      const nextIndex = prev.length;

      const mapped: UploadedFile[] = accepted.map((file, index) => ({
        id: `file_${String(nextIndex + index + 1).padStart(3, '0')}`,
        name: file.name,
        sizeLabel: formatFileSize(file.size),
        uploadedLabel: 'Uploaded just now'
      }));

      return [...prev, ...mapped];
    });

    return { addedCount: accepted.length, rejectedCount };
  }, []);

  const value = useMemo(
    () => ({
      uploadedFiles,
      sampleWorkspaceEnabled,
      addMockFile,
      addFilesFromSelection,
      removeFile,
      setSampleWorkspaceEnabled
    }),
    [uploadedFiles, sampleWorkspaceEnabled, addMockFile, addFilesFromSelection, removeFile]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSourceIntakeContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useSourceIntakeContext must be used inside SourceIntakeProvider');
  return ctx;
}