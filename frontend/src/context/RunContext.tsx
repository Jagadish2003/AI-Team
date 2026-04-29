import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useSearchParams } from "react-router-dom";
import { cleanRunId, isCanonicalRunId } from "../utils/runIds";

type RunContextValue = {
  runId: string | null;
  setRunId: (id: string | null) => void;
  clearRunId: () => void;
};

const RunContext = createContext<RunContextValue | null>(null);

const LS_KEY = "agentiq_run_id";
const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
const TOKEN = (import.meta.env.VITE_DEV_JWT as string | undefined) ?? "dev-token-change-me";

async function validateRunId(id: string): Promise<boolean> {
  if (!isCanonicalRunId(id)) return false;
  try {
    const res = await fetch(`${BASE_URL}/api/runs/${id}`, {
      headers: { Authorization: `Bearer ${TOKEN}` },
    });
    return res.ok;
  } catch {
    return false;
  }
}

export function RunProvider({ children }: { children: React.ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlRunId = searchParams.get("runId");
  const [runId, _setRunId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fromUrl = cleanRunId(urlRunId);
    const stored = (() => {
      try {
        return localStorage.getItem(LS_KEY);
      } catch {
        return null;
      }
    })();
    const fromLs = cleanRunId(stored);
    const candidate = fromUrl ?? fromLs;

    const clearStoredRunId = () => {
      _setRunId(null);
      try {
        localStorage.removeItem(LS_KEY);
      } catch {}
    };

    const clearUrlRunId = () => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        next.delete("runId");
        return next;
      }, { replace: true });
    };

    if (!candidate) {
      _setRunId(null);
      return () => {
        cancelled = true;
      };
    }

    if (!isCanonicalRunId(candidate)) {
      clearStoredRunId();
      if (fromUrl) clearUrlRunId();
      return () => {
        cancelled = true;
      };
    }

    _setRunId((current) => (current === candidate ? current : null));

    validateRunId(candidate).then((valid) => {
      if (cancelled) return;
      if (valid) {
        _setRunId(candidate);
        try {
          localStorage.setItem(LS_KEY, candidate);
        } catch {}
      } else {
        clearStoredRunId();
        if (fromUrl) clearUrlRunId();
      }
    });

    return () => {
      cancelled = true;
    };
  }, [urlRunId, setSearchParams]);

  const setRunId = useCallback(
    (id: string | null) => {
      const nextId = id && isCanonicalRunId(id) ? id : null;
      _setRunId(nextId);
      const next = new URLSearchParams(searchParams);
      if (nextId) {
        next.set("runId", nextId);
        try {
          localStorage.setItem(LS_KEY, nextId);
        } catch {}
      } else {
        next.delete("runId");
        try {
          localStorage.removeItem(LS_KEY);
        } catch {}
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
