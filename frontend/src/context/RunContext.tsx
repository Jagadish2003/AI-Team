import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
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
    // correctly drops a stale cross-org runId from the URL/localStorage. Only a
    // definitive 404 invalidates; transient errors (5xx / auth-not-ready / a
    // network blip) keep the run so it isn't dropped on a hiccup.
    const res = await fetch(`${BASE_URL}/api/runs/${id}`, {
      headers: authHeaderForToken(token),
    });
    if (res.ok) return true;
    if (res.status === 404) return false;
    return true;
  } catch {
    return true;
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
  // react-router recreates setSearchParams as the location/search params change.
  // Depending on it (in the effect below, or in setRunId's useCallback) meant a
  // new identity on navigation/render → the effect re-ran → it validated the run
  // (GET /api/runs/{id}) and set state → re-render → a new identity again: an
  // endless request loop that saturated the browser's connection pool and
  // starved every other page. Read it through a ref so the effect stays keyed on
  // its real inputs and setRunId/clearRunId stay STABLE for every consumer.
  const setSearchParamsRef = useRef(setSearchParams);
  setSearchParamsRef.current = setSearchParams;
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
      setSearchParamsRef.current((prev) => {
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

    // Commit the canonical candidate OPTIMISTICALLY so downstream run-scoped
    // fetches (DiscoveryRunContext, opportunities, …) start immediately instead
    // of waiting a full round-trip for validation — this removes the blocking
    // first hop of the cold-load waterfall. Validate concurrently and only
    // correct course on a DEFINITIVE 404 (stale / cross-org id): drop it and fall
    // back to the org's latest run. A transient error keeps the optimistic id.
    // (DiscoveryRunContext independently clears the id if its own fetch 404s, so
    // a bad id is dropped even if this validation is slow.)
    setActiveRunId(candidate);
    validateRunId(candidate, token).then((valid) => {
      if (cancelled) return;
      if (!valid) {
        if (fromUrl) clearUrlRunId();
        void selectLatestForOrg();
      }
    });

    return () => {
      cancelled = true;
    };
  }, [urlRunId, token]);

  const setRunId = useCallback(
    (id: string | null) => {
      const nextId = id && isCanonicalRunId(id) ? id : null;
      _setRunId(nextId);
      try {
        if (nextId) localStorage.setItem(LS_KEY, nextId);
        else localStorage.removeItem(LS_KEY);
      } catch {}

      setSearchParamsRef.current((prev) => {
        const next = new URLSearchParams(prev);
        if (nextId) next.set("runId", nextId);
        else next.delete("runId");
        return next;
      }, { replace: true });
    },
    // Stable: setSearchParams is read through a ref (see above), so setRunId —
    // and therefore clearRunId and the context value — keep a constant identity.
    []
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
