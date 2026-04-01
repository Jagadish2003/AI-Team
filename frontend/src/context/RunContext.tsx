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
  const [runId, setRunIdState] = useState<string | null>(null);
  useEffect(() => {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("runId");
    const fromLs = window.localStorage.getItem(LS_KEY);

    if (fromUrl) {
      setRunIdState(fromUrl);
      window.localStorage.setItem(LS_KEY, fromUrl);
    } else if (fromLs) {
      setRunIdState(fromLs);
      url.searchParams.set("runId", fromLs);
      window.history.replaceState({}, "", url.toString());
    } else {
      setRunIdState(null);
    }
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