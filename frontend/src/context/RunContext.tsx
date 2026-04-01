import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

type RunContextValue = {
  runId: string | null;
  setRunId: (id: string | null) => void;
};

const Ctx = createContext<RunContextValue | null>(null);

/**
 * Persistence decision (Task 7):
 * - URL param is primary: ?runId=run_xxx
 * - localStorage is fallback (refresh convenience)
 * URL always wins.
 */
const LS_KEY = "agentiq:lastRunId";

export function RunProvider({ children }: { children: React.ReactNode }) {
  const [runId, setRunIdState] = useState<string | null>(null);

  useEffect(() => {
    const url = new URL(window.location.href);
    const fromUrl = url.searchParams.get("runId");
    const fromLs = window.localStorage.getItem(LS_KEY);
    setRunIdState(fromUrl || fromLs || null);
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


  const value = useMemo(() => ({ runId, setRunId }), [runId, setRunId]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRunContext() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useRunContext must be used within RunProvider");
  return v;
}
