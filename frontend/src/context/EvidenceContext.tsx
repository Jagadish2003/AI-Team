import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRunContext } from "./RunContext";
import { fetchEvidence, postEvidenceDecision } from "../api/evidenceApi";
import type { EvidenceReview } from "../types/evidence";
import type { Decision } from "../types/common";
import { isRunNotFoundError, runScopedErrorMessage } from "../utils/apiErrors";

type EvidenceContextValue = {
  loading: boolean;
  error: string | null;
  refetch: () => void;

  evidence: EvidenceReview[];
  setEvidenceDecision: (evidenceId: string, decision: Decision) => Promise<{ ok: boolean; error?: string }>;
};

const Ctx = createContext<EvidenceContextValue | null>(null);

export function EvidenceProvider({ children }: { children: React.ReactNode }) {
  const { runId, clearRunId } = useRunContext();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState(0);
  const [evidence, setEvidence] = useState<EvidenceReview[]>([]);

  const refetch = useCallback(() => setFetchCount((c) => c + 1), []);

  useEffect(() => {
    if (!runId) {
      setEvidence([]);
      setLoading(false);
      setError(null);
      return;
    }
    let cancelled = false;

    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await fetchEvidence(runId);
        if (!cancelled) setEvidence(data);
      } catch (e: any) {
        if (cancelled) return;
        if (isRunNotFoundError(e)) {
          clearRunId();
          return;
        }
        setError(runScopedErrorMessage(e, "Failed to load evidence"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [runId, fetchCount, clearRunId]);

  const setEvidenceDecision = useCallback(
    async (evidenceId: string, decision: Decision) => {
      if (!runId) return { ok: false, error: "No run selected" };

      const before = evidence;
      setEvidence((prev) => prev.map((e) => (e.id === evidenceId ? { ...e, decision } : e)));

      try {
        const updated = await postEvidenceDecision(runId, evidenceId, decision);
        setEvidence((prev) => prev.map((e) => (e.id === evidenceId ? updated : e)));
        return { ok: true };
      } catch (e: any) {
        setEvidence(before);
        return { ok: false, error: e?.message ?? "Failed to save evidence decision" };
      }
    },
    [runId, evidence]
  );

  const value = useMemo(
    () => ({ loading, error, refetch, evidence, setEvidenceDecision }),
    [loading, error, refetch, evidence, setEvidenceDecision]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useEvidenceContext() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useEvidenceContext must be used within EvidenceProvider");
  return v;
}
