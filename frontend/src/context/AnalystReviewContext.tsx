import React, { createContext, useContext, useState, useCallback } from 'react';
import { OpportunityCandidate, Decision, ReviewAuditEvent } from '../types/analystReview';
import rawData from '../data/mockOpportunities.json';

const initialAudit: ReviewAuditEvent[] = [
  { id: 'a1', tsLabel: 'Just now', action: 'Architect Override rationale updated by @jsmith', by: 'jsmith' },
  { id: 'a2', tsLabel: '2m ago',   action: 'Opportunity Rejected: Optimize SAP Approvals Workflow', by: 'jsmith' },
  { id: 'a3', tsLabel: '5m ago',   action: 'Review session started', by: 'system' },
];

interface AnalystReviewContextValue {
  opportunities: OpportunityCandidate[];
  selectedId: string | null;
  selected: OpportunityCandidate | null;
  select: (id: string) => void;
  setDecision: (d: Decision) => { ok: boolean; error?: string };
  setOverrideText: (v: string) => void;
  setOverrideReason: (v: string) => void;
  toggleLock: () => void;
  saveOverride: () => { ok: boolean; error?: string };
  audit: ReviewAuditEvent[];
}

const AnalystReviewContext = createContext<AnalystReviewContextValue | null>(null);

export function AnalystReviewProvider({ children }: { children: React.ReactNode }) {
  const [opportunities, setOpportunities] = useState<OpportunityCandidate[]>(
    rawData as OpportunityCandidate[]
  );
  const [selectedId, setSelectedId] = useState<string | null>(rawData[0]?.id ?? null);
  const [audit, setAudit] = useState<ReviewAuditEvent[]>(initialAudit);

  const selected = opportunities.find(o => o.id === selectedId) ?? null;

  const addAudit = (action: string) => {
    setAudit(prev => [{
      id: String(Date.now()),
      tsLabel: 'Just now',
      action,
      by: 'jsmith',
    }, ...prev]);
  };

  const updateSelected = (fn: (o: OpportunityCandidate) => OpportunityCandidate) => {
    setOpportunities(prev => prev.map(o => o.id === selectedId ? fn(o) : o));
  };

  const setDecision = useCallback((d: Decision): { ok: boolean; error?: string } => {
    if (!selected) return { ok: false, error: 'No opportunity selected' };

    if (selected.decision !== 'UNREVIEWED') {
      return { ok: false, error: "Decision finalized, can't revert" };
    }

    updateSelected(o => ({ ...o, decision: d }));
    addAudit(`Opportunity ${d === 'APPROVED' ? 'Approved' : 'Rejected'}: ${selected?.title}`);

    return { ok: true };
  }, [selectedId, selected]);

  const setOverrideText = useCallback((v: string) => {
    updateSelected(o => ({ ...o, override: { ...o.override, rationaleOverride: v } }));
  }, [selectedId]);

  const setOverrideReason = useCallback((v: string) => {
    updateSelected(o => ({ ...o, override: { ...o.override, overrideReason: v } }));
  }, [selectedId]);

  const toggleLock = useCallback(() => {
    updateSelected(o => ({ ...o, override: { ...o.override, isLocked: !o.override.isLocked } }));
    addAudit('Override lock toggled');
  }, [selectedId]);

  const saveOverride = useCallback((): { ok: boolean; error?: string } => {
    if (!selected) return { ok: false, error: 'No opportunity selected' };
    const hasOverride = selected.override.rationaleOverride.trim().length > 0;
    const hasReason = selected.override.overrideReason.trim().length > 0;
    if (hasOverride && !hasReason) return { ok: false, error: 'Override reason is required.' };
    updateSelected(o => ({ ...o, override: { ...o.override, updatedAt: new Date().toISOString() } }));
    addAudit('Architect Override rationale updated by @jsmith');
    return { ok: true };
  }, [selected, selectedId]);

  return (
    <AnalystReviewContext.Provider value={{
      opportunities, selectedId, selected,
      select: setSelectedId,
      setDecision, setOverrideText, setOverrideReason,
      toggleLock, saveOverride, audit,
    }}>
      {children}
    </AnalystReviewContext.Provider>
  );
}

export function useAnalystReviewContext() {
  const ctx = useContext(AnalystReviewContext);
  if (!ctx) throw new Error('useAnalystReviewContext must be used inside AnalystReviewProvider');
  return ctx;
}