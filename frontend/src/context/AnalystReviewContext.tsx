import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useRunContext } from "./RunContext";
import {
  fetchAudit,
  fetchOpportunities,
  postOpportunityDecision,
  postOpportunityOverride,
} from "../api/analystReviewApi";
import type { OpportunityCandidate, ReviewAuditEvent } from "../types/analystReview";
import type { Decision } from "../types/common";

type AnalystReviewContextValue = {
  loading: boolean;
  error: string | null;
  refetch: () => void;

  opportunities: OpportunityCandidate[];
  selectedId: string | null;
  select: (id: string | null) => void;

  audit: ReviewAuditEvent[];

  setDecision: (oppId: string, decision: Decision) => Promise<{ ok: boolean; error?: string }>;
  saveOverride: (
    oppId: string,
    rationaleOverride: string,
    overrideReason: string,
    isLocked: boolean
  ) => Promise<{ ok: boolean; error?: string }>;
};

const Ctx = createContext<AnalystReviewContextValue | null>(null);

function nowLabel(): string {
  const d = new Date();
  const dd = String(d.getDate()).padStart(2, "0");
  const mon = d.toLocaleString("en-GB", { month: "short" });
  const yyyy = d.getFullYear();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${dd} ${mon} ${yyyy}, ${hh}:${mm}`;
}

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(16).slice(2, 8)}${Date.now().toString(16).slice(-4)}`;
}

export function AnalystReviewProvider({ children }: { children: React.ReactNode }) {
  const { runId } = useRunContext();

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState(0);

  const [opportunities, setOpportunities] = useState<OpportunityCandidate[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [audit, setAudit] = useState<ReviewAuditEvent[]>([]);

  const refetch = useCallback(() => setFetchCount((c) => c + 1), []);
  const select = useCallback((id: string | null) => setSelectedId(id), []);

  useEffect(() => {
    if (!runId) return;
    let cancelled = false;

    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [opps, aud] = await Promise.all([fetchOpportunities(runId), fetchAudit(runId)]);
        if (cancelled) return;
        setOpportunities(opps);
        setAudit(aud);
        setSelectedId((prev) => (prev && opps.some((o) => o.id === prev) ? prev : (opps[0]?.id ?? null)));
      } catch (e: any) {
        if (cancelled) return;
        setError(e?.message ?? "Failed to load analyst review data");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [runId, fetchCount]);

  const setDecision = useCallback(
    async (oppId: string, decision: Decision) => {
      if (!runId) return { ok: false, error: "No run selected" };

      const before = opportunities;
      setOpportunities((prev) => prev.map((o) => (o.id === oppId ? { ...o, decision } : o)));
      setAudit((prev) => [
        { id: uid("aud"), tsLabel: nowLabel(), action: decision, by: "Architect", opportunityId: oppId },
        ...prev,
      ]);

      try {
        const updated = await postOpportunityDecision(runId, oppId, decision);
        setOpportunities((prev) => prev.map((o) => (o.id === oppId ? updated : o)));
        return { ok: true };
      } catch (e: any) {
        setOpportunities(before);
        setAudit((prev) => prev.filter((a) => !(a.opportunityId === oppId && a.action === decision && a.by === "Architect")));
        return { ok: false, error: e?.message ?? "Failed to save decision" };
      }
    },
    [runId, opportunities]
  );

  const saveOverride = useCallback(
    async (oppId: string, rationaleOverride: string, overrideReason: string, isLocked: boolean) => {
      if (!runId) return { ok: false, error: "No run selected" };
      // Only require a reason when rationale text is actually provided
      if (rationaleOverride.trim().length > 0 && overrideReason.trim().length === 0) {
        return { ok: false, error: "Override reason is required when rationale override is provided." };
      }

      const before = opportunities;
      setOpportunities((prev) =>
        prev.map((o) =>
          o.id === oppId
            ? { ...o, override: { isLocked, rationaleOverride, overrideReason, updatedAt: new Date().toISOString() } }
            : o
        )
      );
      setAudit((prev) => [
        { id: uid("aud"), tsLabel: nowLabel(), action: "OVERRIDE_SAVED", by: "Architect", opportunityId: oppId },
        ...prev,
      ]);

      try {
        const updated = await postOpportunityOverride(runId, oppId, { rationaleOverride, overrideReason, isLocked });
        setOpportunities((prev) => prev.map((o) => (o.id === oppId ? updated : o)));
        return { ok: true };
      } catch (e: any) {
        setOpportunities(before);
        return { ok: false, error: e?.message ?? "Failed to save override" };
      }
    },
    [runId, opportunities]
  );

  const value = useMemo(
    () => ({ loading, error, refetch, opportunities, selectedId, select, audit, setDecision, saveOverride }),
    [loading, error, refetch, opportunities, selectedId, select, audit, setDecision, saveOverride]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAnalystReviewContext() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAnalystReviewContext must be used within AnalystReviewProvider");
  return v;
}
