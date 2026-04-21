import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

type RunContextValue = {
  runId: string | null;
  setRunId: (id: string | null) => void;
  clearRunId: () => void;
};

const RunContext = createContext<RunContextValue | null>(null);

const LS_KEY = "agentiq:lastRunId";

export function RunProvider({ children }: { children: React.ReactNode }) {
  const [runId, setRunIdState] = useState<string | null>(() => {
    try {
      const fromUrl = new URL(window.location.href).searchParams.get("runId");
      const fromLs = window.localStorage.getItem(LS_KEY);
      return fromUrl ?? (fromLs && fromLs.trim().length > 0 ? fromLs : null);
    } catch {
      return null;
    }
  });

  // Sync URL on first render if runId came from localStorage only.
  useEffect(() => {
    if (!runId) return;
    const url = new URL(window.location.href);
    if (!url.searchParams.get("runId")) {
      url.searchParams.set("runId", runId);
      window.history.replaceState({}, "", url.toString());
    }
    window.localStorage.setItem(LS_KEY, runId);
  }, []);
  const setRunId = useCallback((id: string | null) => {
    setRunIdState(id);

    const url = new URL(window.location.href);

    if (id) {
      url.searchParams.set("runId", id);
      window.localStorage.setItem(LS_KEY, id);
    } else {
      url.searchParams.delete("runId");
      window.localStorage.removeItem(LS_KEY);
    }

    window.history.replaceState({}, "", url.toString());
  }, []);

  const clearRunId = useCallback(() => {
    setRunIdState(null);

    const url = new URL(window.location.href);
    url.searchParams.delete("runId");

    window.localStorage.removeItem(LS_KEY);

    window.history.replaceState({}, "", url.toString());
  }, []);

  const value = useMemo(
    () => ({
      runId,
      setRunId,
      clearRunId,
    }),
    [runId, setRunId, clearRunId]
  );

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
}

export function useRunContext() {
  const context = useContext(RunContext);
  if (!context) {
    throw new Error("useRunContext must be used within RunProvider");
  }
  return context;
}