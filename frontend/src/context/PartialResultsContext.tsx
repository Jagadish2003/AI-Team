import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { EvidenceReview, ExtractedEntity, EntityType, ReviewDecision } from '../types/partialResults';
import { fetchEvidence, fetchEntities } from '../api/runApi';
import { postEvidenceDecision } from '../api/evidenceApi';
import { useRunContext } from './RunContext';
import { useDiscoveryRunContext } from './DiscoveryRunContext';
import { isRunNotFoundError, runScopedErrorMessage } from '../utils/apiErrors';

type PartialResultsContextValue = {
  entities: ExtractedEntity[];
  evidence: EvidenceReview[];
  selectedEvidenceId: string | null;

  selectedEntityIds: string[];
  entityTypes: Record<EntityType, boolean>;
  queryEntities: string;
  queryEvidence: string;
  sourceFilter: string;
  activeTab: 'Entities' | 'Opportunities';

  saveDraftEnabled: boolean;
  setSaveDraftEnabled: (v: boolean) => void;

  selectEvidence: (id: string) => void;
  toggleEntity: (id: string) => void;
  setEntityTypeEnabled: (t: EntityType, v: boolean) => void;
  setQueryEntities: (v: string) => void;
  setQueryEvidence: (v: string) => void;
  setSourceFilter: (v: string) => void;
  setActiveTab: (v: 'Entities' | 'Opportunities') => void;

  approveSelected: () => boolean;
  rejectSelected: () => boolean;
  clearSelection: () => void;

  goPrev: () => void;
  goNext: () => void;
  canPrev: boolean;
  canNext: boolean;

  filteredEntities: ExtractedEntity[];
  filteredEvidence: EvidenceReview[];
  selectedEvidence: EvidenceReview | null;
  sources: string[];
  countsByType: Record<EntityType, number>;

  positionLabel: string;
  loading: boolean;
  error: string | null;
  refetch: () => void;
};

const Ctx = createContext<PartialResultsContextValue | null>(null);

function hasMaterializedArtifacts(status: string | undefined): boolean {
  const normalized = status?.toLowerCase();
  return normalized === 'complete' || normalized === 'completed' || normalized === 'partial';
}

const defaultTypes: Record<EntityType, boolean> = {
  Application: true,
  Workflow: true,
  Service: true,
  Role: false,
  DataObject: false,
  Other: false
};

export function PartialResultsProvider({ children }: { children: React.ReactNode }) {
  const { runId, clearRunId } = useRunContext();
  const { run } = useDiscoveryRunContext();
  const runStatus = run?.status?.toLowerCase();
  const [entities, setEntities] = useState<ExtractedEntity[]>([]);
  const [evidence, setEvidence] = useState<EvidenceReview[]>([]);
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState(0);
  const refetch = useCallback(() => setFetchCount((c) => c + 1), []);

  useEffect(() => {
    if (!runId) {
      setEntities([]);
      setEvidence([]);
      setSelectedEvidenceId(null);
      setLoading(false);
      setError(null);
      return;
    }
    if (!hasMaterializedArtifacts(runStatus)) {
      setEntities([]);
      setEvidence([]);
      setSelectedEvidenceId(null);
      setLoading(runStatus !== 'failed');
      setError(runStatus === 'failed' ? 'Discovery run failed before evidence was generated.' : null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const [ev, en] = await Promise.all([fetchEvidence(runId), fetchEntities(runId)]);
        if (cancelled) return;
        setEvidence(ev);
        setEntities(en);
        setSelectedEvidenceId(ev[0]?.id ?? null);
      } catch (e: any) {
        if (cancelled) return;
        if (isRunNotFoundError(e)) {
          clearRunId();
          return;
        }
        setError(runScopedErrorMessage(e, 'Failed to load partial results'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [runId, runStatus, fetchCount, clearRunId]);

  const [selectedEntityIds, setSelectedEntityIds] = useState<string[]>([]);
  const [entityTypes, setEntityTypes] = useState(defaultTypes);
  const [queryEntities, setQueryEntities] = useState('');
  const [queryEvidence, setQueryEvidence] = useState('');
  const [sourceFilter, setSourceFilter] = useState<string>('All Sources');
  const [activeTab, setActiveTab] = useState<'Entities' | 'Opportunities'>('Entities');
  const [saveDraftEnabled, setSaveDraftEnabled] = useState(true);

  const sources = useMemo(() => {
    const set = new Set(evidence.map(e => e.source));
    return ['All Sources', ...Array.from(set).sort()];
  }, [evidence]);

  const countsByType = useMemo(() => {
    const counts = { Application:0, Workflow:0, Service:0, Role:0, DataObject:0, Other:0 } as Record<EntityType, number>;
    for (const e of entities) counts[e.type] = (counts[e.type] ?? 0) + 1;
    return counts;
  }, [entities]);

  const filteredEntities = useMemo(() => {
    const q = queryEntities.trim().toLowerCase();
    return entities.filter(e => entityTypes[e.type] && (!q || e.name.toLowerCase().includes(q)));
  }, [entities, entityTypes, queryEntities]);

  const filteredEvidence = useMemo(() => {
    const q = queryEvidence.trim().toLowerCase();
    return evidence
      .filter(ev => sourceFilter === 'All Sources' || ev.source === sourceFilter)
      .filter(ev => !q || ev.title.toLowerCase().includes(q) || ev.snippet.toLowerCase().includes(q))
      .filter(ev => selectedEntityIds.length === 0 || selectedEntityIds.some(id => ev.entities.includes(id)));
  }, [evidence, queryEvidence, sourceFilter, selectedEntityIds]);

  const selectedEvidence = useMemo(() => evidence.find(e => e.id === selectedEvidenceId) ?? null, [evidence, selectedEvidenceId]);

  const currentIndex = useMemo(() => {
    if (!selectedEvidenceId) return -1;
    return filteredEvidence.findIndex(e => e.id === selectedEvidenceId);
  }, [filteredEvidence, selectedEvidenceId]);

  const canPrev = currentIndex > 0;
  const canNext = currentIndex >= 0 && currentIndex < filteredEvidence.length - 1;

  const positionLabel = useMemo(() => {
    if (!filteredEvidence.length || currentIndex < 0) return `0 of ${filteredEvidence.length}`;
    return `${currentIndex + 1} of ${filteredEvidence.length}`;
  }, [filteredEvidence.length, currentIndex]);

  const selectEvidence = useCallback((id: string) => setSelectedEvidenceId(id), []);

  const toggleEntity = useCallback((id: string) => {
    setSelectedEntityIds(prev => prev.includes(id) ? prev.filter(x => x !== id) : [...prev, id]);
  }, []);

  const setEntityTypeEnabled = useCallback((t: EntityType, v: boolean) => {
    setEntityTypes(prev => ({ ...prev, [t]: v }));
  }, []);

  const clearSelection = useCallback(() => setSelectedEntityIds([]), []);

  const goPrev = useCallback(() => {
    if (currentIndex <= 0) return;
    const prev = filteredEvidence[currentIndex - 1];
    if (prev) setSelectedEvidenceId(prev.id);
  }, [currentIndex, filteredEvidence]);

  const goNext = useCallback(() => {
    if (currentIndex < 0) return;
    const nxt = filteredEvidence[currentIndex + 1];
    if (nxt) setSelectedEvidenceId(nxt.id);
  }, [currentIndex, filteredEvidence]);

  const goNextUnreviewed = useCallback((fromIndex: number) => {
    const next = filteredEvidence.slice(fromIndex + 1).find(e => e.decision === 'UNREVIEWED');
    if (next) setSelectedEvidenceId(next.id);
    else {
      const first = filteredEvidence.find(e => e.decision === 'UNREVIEWED');
      if (first) setSelectedEvidenceId(first.id);
    }
  }, [filteredEvidence]);

  const setDecisionForSelected = useCallback((decision: ReviewDecision) => {
    if (!selectedEvidenceId || !runId) return false;

    const currentEvidence = evidence.find(ev => ev.id === selectedEvidenceId);
    if (!currentEvidence) return false;

    if (currentEvidence.decision !== 'UNREVIEWED') {
      return false;
    }

    // Optimistic local update
    setEvidence(prev => prev.map(ev => ev.id === selectedEvidenceId ? { ...ev, decision } : ev));

    // Persist to backend so decision survives page refresh
    postEvidenceDecision(runId, selectedEvidenceId, decision as any).catch(() => {
      // Roll back on failure
      setEvidence(prev => prev.map(ev => ev.id === selectedEvidenceId ? { ...ev, decision: 'UNREVIEWED' } : ev));
    });

    const idx = currentIndex;
    if (idx >= 0) goNextUnreviewed(idx);

    return true;
  }, [selectedEvidenceId, runId, evidence, currentIndex, goNextUnreviewed]);

  const approveSelected = useCallback(() => setDecisionForSelected('APPROVED'), [setDecisionForSelected]);
  const rejectSelected = useCallback(() => setDecisionForSelected('REJECTED'), [setDecisionForSelected]);

  const value = useMemo(() => ({
    entities,
    evidence,
    selectedEvidenceId,
    selectedEntityIds,
    entityTypes,
    queryEntities,
    queryEvidence,
    sourceFilter,
    activeTab,
    saveDraftEnabled,
    setSaveDraftEnabled,
    selectEvidence,
    toggleEntity,
    setEntityTypeEnabled,
    setQueryEntities,
    setQueryEvidence,
    setSourceFilter,
    setActiveTab,
    approveSelected,
    rejectSelected,
    clearSelection,
    goPrev,
    goNext,
    canPrev,
    canNext,
    filteredEntities,
    filteredEvidence,
    selectedEvidence,
    sources,
    countsByType,
    positionLabel,
    loading,
    error,
    refetch,
  }), [
    entities, evidence, selectedEvidenceId, selectedEntityIds, entityTypes, queryEntities, queryEvidence, sourceFilter, activeTab,
    saveDraftEnabled, filteredEntities, filteredEvidence, selectedEvidence, sources, countsByType, positionLabel,
    selectEvidence, toggleEntity, setEntityTypeEnabled, approveSelected, rejectSelected, clearSelection, goPrev, goNext,
    canPrev, canNext, loading, error, refetch,
  ]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function usePartialResultsContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('usePartialResultsContext must be used inside PartialResultsProvider');
  return ctx;
}
