import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useRunContext } from "./RunContext";
import {
  fetchAudit,
  fetchOpportunities,
  postOpportunityDecision,
  postOpportunityOverride,
} from "../api/analystReviewApi";
import type { OpportunityCandidate, ReviewAuditEvent } from "../types/analystReview";
import type { Decision } from "../types/common";
import { isRunNotFoundError, runScopedErrorMessage } from "../utils/apiErrors";
import { useDiscoveryRunContext } from "./DiscoveryRunContext";
import { useDataCache } from "../lib/dataCache";
import { cacheKeys } from "../lib/cacheKeys";
import { useRevalidateOnFocus } from "../lib/useRevalidate";

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

function hasMaterializedArtifacts(status: string | undefined): boolean {
  const normalized = status?.toLowerCase();
  return normalized === "complete" || normalized === "completed" || normalized === "partial";
}

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
  const { run, computing } = useDiscoveryRunContext();
  const runStatus = run?.status?.toLowerCase();
  const cache = useDataCache();

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState(0);

  const [opportunities, setOpportunities] = useState<OpportunityCandidate[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [audit, setAudit] = useState<ReviewAuditEvent[]>([]);

  const refetch = useCallback(() => setFetchCount((c) => c + 1), []);
  const select = useCallback((id: string | null) => setSelectedId(id), []);

  // The run whose opportunities we already hold. Distinguishes a FIRST load (show
  // the loading state) from a revalidation of data we already have (must stay on
  // screen). Without this, the focus/interval/push revalidation that keeps the
  // org in sync replaced the whole page with skeletons every time the user
  // switched back to the tab.
  const loadedRunIdRef = useRef<string | null>(null);

  // Clear the previous run's data the moment the active run changes, so a stale
  // opportunity list never shows while the new run's fetch is in flight. Kept
  // separate from the fetch effect below so a status-driven refetch of the SAME
  // run does not blank the list (no flicker).
  useEffect(() => {
    loadedRunIdRef.current = null;
    setOpportunities([]);
    setAudit([]);
    setSelectedId(null);
    setError(null);
  }, [runId]);

  useEffect(() => {
    if (!runId || runStatus === "failed") {
      setLoading(false);
      return;
    }
    // Defer the opportunities/audit fetch until the run has SETTLED (not
    // computing): /opportunities is fetched ONCE the run reaches 100% (this effect
    // re-runs as `computing` flips to false), never polled during the run. While
    // computing, the "preparing" state below drives the view. This removes the
    // during-run /runs/{id}/opportunities + /audit hits.
    if (computing) return;
    let cancelled = false;

    (async () => {
      // Only surface the loading state on the FIRST load of this run. Later runs
      // of this effect are revalidations (status change, decision refresh, and
      // the focus/interval/push org sync) — they must refresh the list silently
      // with the current one still rendered, never swap it for skeletons.
      if (loadedRunIdRef.current !== runId) setLoading(true);
      setError(null);
      try {
        // Fetch as soon as the run id is known — do NOT gate on run status. The
        // /opportunities endpoint returns [] (not 404) for a run that has not
        // materialised yet, so a still-computing run simply yields an empty list
        // here and the "preparing" state is derived from run status below. This
        // removes the status→opportunities waterfall hop (opportunities now load
        // in parallel with run status instead of after it).
        //
        // Opportunities are the critical Viewer+ resource. The audit trail is an
        // owner-only endpoint (RBAC: analysts/viewers get 403), so it is fetched
        // tolerantly — a 403 or any audit failure degrades to an empty trail and
        // must never break the opportunity view for a non-owner (AC2).
        const [opps, aud] = await Promise.all([
          fetchOpportunities(runId),
          fetchAudit(runId).catch(() => [] as ReviewAuditEvent[]),
        ]);
        if (cancelled) return;
        // We now hold this run's data — later fetches revalidate in the
        // background instead of re-showing the loading state.
        loadedRunIdRef.current = runId;
        setOpportunities(opps);
        setAudit(aud);
        setSelectedId((prev) => (prev && opps.some((o) => o.id === prev) ? prev : (opps[0]?.id ?? null)));
      } catch (e: any) {
        if (cancelled) return;
        // A stale/cross-org run id 404s here; DiscoveryRunContext clears it, so
        // stay quiet rather than flashing an error before the id is dropped.
        if (isRunNotFoundError(e)) return;
        setError(runScopedErrorMessage(e, "Failed to load analyst review data"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [runId, runStatus, computing, fetchCount]);

  // Opportunities, decisions and the audit trail are SHARED across the org —
  // several analysts review the same run. Revalidate on tab focus and on a slow
  // tick so another analyst's approve/reject/override appears here without a
  // reload. The fetch replaces the list only on success, so there is no flicker,
  // and the server stays the source of truth for a concurrently-edited decision.
  // Post-run collaborative sync only — never while the run is computing (the
  // completion fetch above covers that, and we don't poll during a run).
  useRevalidateOnFocus(refetch, { enabled: Boolean(runId) && !computing });

  // A run that exists but has not materialised yet (still computing, or status
  // not yet loaded) with no opportunities in hand is "preparing" — surface a
  // loading state, not an empty "no opportunities" view. A failed run surfaces an
  // error. Both are derived from run status, which arrives in PARALLEL with the
  // opportunities fetch above rather than gating it.
  const preparing =
    Boolean(runId) &&
    runStatus !== "failed" &&
    !hasMaterializedArtifacts(runStatus) &&
    opportunities.length === 0;
  const exposedLoading = loading || preparing;
  const exposedError =
    runStatus === "failed"
      ? "Discovery run failed before opportunities were generated."
      : error;

  const setDecision = useCallback(
    async (oppId: string, decision: Decision) => {
      if (!runId) return { ok: false, error: "No run selected" };

      const before = opportunities;
      // P8: decisions stay editable. A change appends a NEW audit event
      // (preserving prior events); a no-op re-click of the same decision does
      // not. The optimistic event mirrors the backend shape (previousDecision +
      // the audit id lets us roll back precisely on failure).
      const previousDecision = opportunities.find((o) => o.id === oppId)?.decision ?? "UNREVIEWED";
      const changed = previousDecision !== decision;
      const optimisticAuditId = uid("aud");

      setOpportunities((prev) => prev.map((o) => (o.id === oppId ? { ...o, decision } : o)));
      if (changed) {
        setAudit((prev) => [
          {
            id: optimisticAuditId,
            tsLabel: nowLabel(),
            tsEpoch: Math.floor(Date.now() / 1000),
            action: decision,
            previousDecision,
            by: "Architect",
            opportunityId: oppId,
          },
          ...prev,
        ]);
      }

      try {
        const updated = await postOpportunityDecision(runId, oppId, decision);
        setOpportunities((prev) => prev.map((o) => (o.id === oppId ? updated : o)));
        // A decision changes run-scoped derived views server-side (roadmap phase
        // assignment, etc.). Invalidate the run scope so the Blueprint roadmap and
        // any other run-scoped consumer refresh instantly — no manual reload.
        cache.invalidate(cacheKeys.runScope(runId));
        cache.invalidate(cacheKeys.learningScope);
        return { ok: true };
      } catch (e: any) {
        setOpportunities(before);
        if (changed) {
          setAudit((prev) => prev.filter((a) => a.id !== optimisticAuditId));
        }
        return { ok: false, error: e?.message ?? "Failed to save decision" };
      }
    },
    [runId, opportunities, cache]
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
        cache.invalidate(cacheKeys.runScope(runId));
        return { ok: true };
      } catch (e: any) {
        setOpportunities(before);
        return { ok: false, error: e?.message ?? "Failed to save override" };
      }
    },
    [runId, opportunities, cache]
  );

  const value = useMemo(
    () => ({ loading: exposedLoading, error: exposedError, refetch, opportunities, selectedId, select, audit, setDecision, saveOverride }),
    [exposedLoading, exposedError, refetch, opportunities, selectedId, select, audit, setDecision, saveOverride]
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAnalystReviewContext() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAnalystReviewContext must be used within AnalystReviewProvider");
  return v;
}
