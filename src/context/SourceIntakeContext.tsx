import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import uploadsMock from '../data/mockUploads.json';
import { UploadedFile } from '../types/upload';

type SourceIntakeContextValue = {
  uploadedFiles: UploadedFile[];
  sampleWorkspaceEnabled: boolean;

  addMockFile: () => void;
  removeFile: (id: string) => void;
  setSampleWorkspaceEnabled: (v: boolean) => void;
};

const Ctx = createContext<SourceIntakeContextValue | null>(null);

export function SourceIntakeProvider({ children }: { children: React.ReactNode }) {
  const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>(uploadsMock as unknown as UploadedFile[]);
  const [sampleWorkspaceEnabled, setSampleWorkspaceEnabled] = useState<boolean>(false);

  const removeFile = useCallback((id: string) => {
    setUploadedFiles(prev => prev.filter(f => f.id !== id));
  }, []);

  // Demo helper: add a fake file
  const addMockFile = useCallback(() => {
    setUploadedFiles(prev => {
      const n = prev.length + 1;
      const id = `file_${String(n).padStart(3,'0')}`;
      return [...prev, { id, name: `extra_upload_${n}.csv`, sizeLabel: '420 KB', uploadedLabel: 'Uploaded just now' }];
    });
  }, []);

  const value = useMemo(() => ({
    uploadedFiles,
    sampleWorkspaceEnabled,
    addMockFile,
    removeFile,
    setSampleWorkspaceEnabled
  }), [uploadedFiles, sampleWorkspaceEnabled, addMockFile, removeFile]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSourceIntakeContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useSourceIntakeContext must be used inside SourceIntakeProvider');
  return ctx;
}
