import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useSearchParams } from "react-router-dom";

type RunContextValue = {
  runId: string | null;
  setRunId: (id: string | null) => void;
  clearRunId: () => void;
};

const RunContext = createContext<RunContextValue | null>(null);

const LS_KEY = "agentiq_run_id";
const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
const TOKEN   = (import.meta.env.VITE_DEV_JWT    as string | undefined) ?? "dev-token-change-me";

async function validateRunId(id: string): Promise<boolean> {
  try {
    const res = await fetch(`${BASE_URL}/api/runs/${id}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    return res.ok;
  } catch {
    // Network error — treat as valid so we don't clear a good runId offline
    return true;
  }
}

export function RunProvider({ children }: { children: React.ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlRunId = searchParams.get("runId");

  // Start null — run-scoped pages show RunRequiredEmptyState on first render,
  // then resolve after validation below.
  const [runId, _setRunId] = useState<string | null>(null);

  // Re-run whenever URL ?runId changes. Validates the candidate against the
  // backend: unknown runIds resolve to null so pages show RunRequiredEmptyState
  // instead of an error panel.
  useEffect(() => {
    const fromUrl = urlRunId && urlRunId.trim() ? urlRunId : null;
    const stored  = (() => { try { return localStorage.getItem(LS_KEY); } catch { return null; } })();
    const fromLs  = stored && stored.trim() ? stored : null;
    const candidate = fromUrl ?? fromLs;

    if (!candidate) {
      _setRunId(null);
      return;
    }

    validateRunId(candidate).then((valid) => {
      if (valid) {
        _setRunId(candidate);
        try { localStorage.setItem(LS_KEY, candidate); } catch {}
      } else {
        // Run not found — clear so RunRequiredEmptyState is shown
        _setRunId(null);
        try { localStorage.removeItem(LS_KEY); } catch {}
        // Remove stale ?runId from URL
        const next = new URLSearchParams(searchParams);
        next.delete("runId");
        setSearchParams(next, { replace: true });
      }
    });
  }, [urlRunId]); // eslint-disable-line react-hooks/exhaustive-deps

  const setRunId = useCallback(
    (id: string | null) => {
      _setRunId(id);
      const next = new URLSearchParams(searchParams);
      if (id) {
        next.set("runId", id);
        try { localStorage.setItem(LS_KEY, id); } catch {}
      } else {
        next.delete("runId");
        try { localStorage.removeItem(LS_KEY); } catch {}
      }
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams]
  );

  const clearRunId = useCallback(() => setRunId(null), [setRunId]);

  const value = useMemo(
    () => ({ runId, setRunId, clearRunId }),
    [runId, setRunId, clearRunId]
  );

  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
}

export function useRunContext() {
  const ctx = useContext(RunContext);
  if (!ctx) throw new Error("useRunContext must be used within RunProvider");
  return ctx;
}
