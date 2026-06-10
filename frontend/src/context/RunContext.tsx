import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useSearchParams } from "react-router-dom";
import { authHeaderForToken } from "../lib/apiClient";
import { useAuthOptional } from "./AuthContext";
import { cleanRunId, isCanonicalRunId } from "../utils/runIds";

type RunContextValue = {
  runId: string | null;
  setRunId: (id: string | null) => void;
  clearRunId: () => void;
};

const RunContext = createContext<RunContextValue | null>(null);

const LS_KEY = "agentiq_run_id";
const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

interface RunSummary {
  id: string;
  status?: string | null;
  startedAt?: string | null;
}

async function validateRunId(id: string, token: string | null): Promise<boolean> {
  if (!isCanonicalRunId(id)) return false;
  try {
    // Sign with the in-session JWT so the run is validated against THIS user's
    // org. A run owned by another org returns 404 → not valid here, which
    // correctly drops a stale cross-org runId from the URL/localStorage.
    const res = await fetch(`${BASE_URL}/api/runs/${id}`, {
      headers: authHeaderForToken(token),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** Most recent run for the caller's org, or null. The backend list is org-
 * scoped and newest-first, so any member of the org (and the creator after a
 * logout that wiped the in-memory token) can re-find the workspace's run. */
async function fetchLatestOrgRunId(token: string | null): Promise<string | null> {
  try {
    const res = await fetch(`${BASE_URL}/api/runs`, {
      headers: authHeaderForToken(token),
    });
    if (!res.ok) return null;
    const runs = (await res.json()) as RunSummary[];
    if (!Array.isArray(runs)) return null;
    const latest = runs.find((r) => r && isCanonicalRunId(r.id));
    return latest ? latest.id : null;
  } catch {
    return null;
  }
}

export function RunProvider({ children }: { children: React.ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlRunId = searchParams.get("runId");
  const [runId, _setRunId] = useState<string | null>(null);
  // Use OUR own in-session token, not apiClient's module token: RunProvider is a
  // child of AuthProvider, so this effect runs before AuthProvider syncs the
  // module token — reading it here would race. token from context is current.
  const auth = useAuthOptional();
  const token = auth?.token ?? null;

  useEffect(() => {
    let cancelled = false;

    // Logged out: nothing to resolve. Leave localStorage intact so the run is
    // recoverable after re-login (and don't 404-clear it with a dev token).
    if (!token) {
      _setRunId(null);
      return () => {
        cancelled = true;
      };
    }

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

    const setActiveRunId = (id: string | null) => {
      if (cancelled) return;
      _setRunId(id);
      try {
        if (id) localStorage.setItem(LS_KEY, id);
        else localStorage.removeItem(LS_KEY);
      } catch {}
    };

    const clearUrlRunId = () => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        next.delete("runId");
        return next;
      }, { replace: true });
    };

    // No usable candidate → fall back to this org's most recent run so any
    // member of the org sees the active run (and the creator sees it again
    // after logout). Auto-selection updates state + localStorage but NOT the
    // URL, to avoid stamping ?runId= onto every page.
    const selectLatestForOrg = async () => {
      const latest = await fetchLatestOrgRunId(token);
      if (cancelled) return;
      setActiveRunId(latest);
    };

    if (!candidate || !isCanonicalRunId(candidate)) {
      if (candidate) {
        // Malformed stored/url value — drop it.
        if (fromUrl) clearUrlRunId();
      }
      void selectLatestForOrg();
      return () => {
        cancelled = true;
      };
    }

    validateRunId(candidate, token).then((valid) => {
      if (cancelled) return;
      if (valid) {
        setActiveRunId(candidate);
      } else {
        // Stale or belongs to another org — drop it and fall back to this
        // org's latest run instead of leaving the user with nothing.
        if (fromUrl) clearUrlRunId();
        void selectLatestForOrg();
      }
    });

    return () => {
      cancelled = true;
    };
  }, [urlRunId, setSearchParams, token]);

  const setRunId = useCallback(
    (id: string | null) => {
      const nextId = id && isCanonicalRunId(id) ? id : null;
      _setRunId(nextId);
      try {
        if (nextId) localStorage.setItem(LS_KEY, nextId);
        else localStorage.removeItem(LS_KEY);
      } catch {}

      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        if (nextId) next.set("runId", nextId);
        else next.delete("runId");
        return next;
      }, { replace: true });
    },
    [setSearchParams]
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
