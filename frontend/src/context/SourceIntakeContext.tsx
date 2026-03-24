import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { UploadedFile } from '../types/upload';
import { addUpload, fetchUploads } from '../services/staticApi';

type SourceIntakeContextValue = {
  uploadedFiles: UploadedFile[];
  sampleWorkspaceEnabled: boolean;

  // Task 5 additions
  loading: boolean;
  error: string | null;
  refetch: () => void;

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
  const[uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
  const[sampleWorkspaceEnabled, setSampleWorkspaceEnabled] = useState<boolean>(false);

  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const[fetchCount, setFetchCount] = useState<number>(0);
  
  const refetch = useCallback(() => setFetchCount((c) => c + 1),[]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchUploads()
      .then((data) => {
        if (!alive) return;
        setUploadedFiles(data);
        setError(null);
      })
      .catch((e) => {
        if (!alive) return;
        setError(e?.message ?? 'Failed to load uploads');
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [fetchCount]);

  const removeFile = useCallback((id: string) => {
    setUploadedFiles((prev) => prev.filter((f) => f.id !== id));
  },[]);

  const addMockFile = useCallback(() => {
    const name = `extra_upload_${Date.now()}.csv`;
    addUpload({ name, sizeLabel: '420 KB' })
      .then((u) => {
        setUploadedFiles((prev) =>[...prev, u]);
        setError(null);
      })
      .catch((e) => setError(e?.message ?? 'Failed to add upload'));
  },[]);

  const addFilesFromSelection = useCallback((files: File[]) => {
    const accepted = files.filter(isAllowedSpreadsheetFile);
    const rejectedCount = files.length - accepted.length;

    if (accepted.length === 0) {
      return { addedCount: 0, rejectedCount };
    }

    accepted.forEach((file) => {
      addUpload({ name: file.name, sizeLabel: formatFileSize(file.size) })
        .then((u) => {
          setUploadedFiles((prev) => [...prev, u]);
        })
        .catch((e) => setError(e?.message ?? 'Failed to add file'));
    });

    return { addedCount: accepted.length, rejectedCount };
  },[]);

  const value = useMemo(
    () => ({
      uploadedFiles,
      sampleWorkspaceEnabled,
      loading,
      error,
      refetch,
      addMockFile,
      addFilesFromSelection,
      removeFile,
      setSampleWorkspaceEnabled
    }),[uploadedFiles, sampleWorkspaceEnabled, loading, error, refetch, addMockFile, addFilesFromSelection, removeFile]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSourceIntakeContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useSourceIntakeContext must be used inside SourceIntakeProvider');
  return ctx;
}