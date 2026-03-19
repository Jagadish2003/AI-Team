import React, { createContext, useContext, useMemo, useState } from 'react';
import rowsMock from '../data/mockMappings.json';
import permsMock from '../data/mockPermissions.json';
import confMock from '../data/mockConfidenceExplanation.json';
import { MappingRow, PermissionRequirement, ConfidenceExplanation } from '../types/normalization';

export type Tab = 'MAPPED' | 'UNMAPPED' | 'AMBIGUOUS';
type SortMode = 'Confidence High→Low' | 'Source A→Z';

type NormalizationContextValue = {
  rows: MappingRow[];
  permissions: PermissionRequirement[];
  confidence: ConfidenceExplanation;

  activeTab: Tab;
  setActiveTab: (t: Tab) => void;

  search: string;
  setSearch: (v: string) => void;

  sourceFilter: string;
  setSourceFilter: (v: string) => void;

  entityFilter: string;
  setEntityFilter: (v: string) => void;

  sortMode: SortMode;
  setSortMode: (v: SortMode) => void;

  selectedRowId: string | null;
  setSelectedRowId: (id: string) => void;

  sources: string[];
  entities: string[];
  counts: Record<Tab, number>;
  filteredRows: MappingRow[];
  selectedRow: MappingRow | null;
  relevantPermissions: PermissionRequirement[];
};

const Ctx = createContext<NormalizationContextValue | null>(null);

export function NormalizationProvider({ children }: { children: React.ReactNode }) {
  const [rows] = useState<MappingRow[]>(rowsMock as unknown as MappingRow[]);
  const [permissions] = useState<PermissionRequirement[]>(permsMock as unknown as PermissionRequirement[]);
  const [confidence] = useState<ConfidenceExplanation>(confMock as unknown as ConfidenceExplanation);

  const [activeTab, setActiveTab] = useState<Tab>('MAPPED');
  const [search, setSearch] = useState('');
  const [sourceFilter, setSourceFilter] = useState('All Sources');
  const [entityFilter, setEntityFilter] = useState('All Entities');
  const [sortMode, setSortMode] = useState<SortMode>('Confidence High→Low');
  const [selectedRowId, setSelectedRowId] = useState<string | null>(rows[0]?.id ?? null);

  const sources = useMemo(() => ['All Sources', ...Array.from(new Set(rows.map(r => r.sourceSystem))).sort()], [rows]);
  const entities = useMemo(() => ['All Entities', ...Array.from(new Set(rows.map(r => r.commonEntity))).sort()], [rows]);

  const counts = useMemo(() => {
    const c: Record<Tab, number> = { MAPPED: 0, UNMAPPED: 0, AMBIGUOUS: 0 };
    for (const r of rows) c[r.status as Tab] += 1;
    return c;
  }, [rows]);

  const selectedRow = useMemo(() => rows.find(r => r.id === selectedRowId) ?? null, [rows, selectedRowId]);

  const relevantPermissions = useMemo(() => {
    if (!selectedRow) return [];
    return permissions.filter(p => p.sourceSystem === selectedRow.sourceSystem);
  }, [permissions, selectedRow]);

  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();

    // ✅ Split comma-joined strings to support multi-select from SourcesEntitiesPanel
    const activeSources = sourceFilter === 'All Sources' ? [] : sourceFilter.split(',');
    const activeEntities = entityFilter === 'All Entities' ? [] : entityFilter.split(',');

    let list = rows
      .filter(r => r.status === activeTab)
      .filter(r => activeSources.length === 0 || activeSources.includes(r.sourceSystem))
      .filter(r => activeEntities.length === 0 || activeEntities.includes(r.commonEntity))
      .filter(r => !q || r.sourceField.toLowerCase().includes(q) || r.commonField.toLowerCase().includes(q));

    if (sortMode === 'Confidence High→Low') {
      const score = (c: string) => c === 'HIGH' ? 3 : c === 'MEDIUM' ? 2 : 1;
      list = list.slice().sort((a, b) => score(b.confidence) - score(a.confidence));
    } else {
      list = list.slice().sort((a, b) => a.sourceSystem.localeCompare(b.sourceSystem));
    }

    return list;
  }, [rows, activeTab, search, sourceFilter, entityFilter, sortMode]);

  const value = useMemo(() => ({
    rows, permissions, confidence,
    activeTab, setActiveTab,
    search, setSearch,
    sourceFilter, setSourceFilter,
    entityFilter, setEntityFilter,
    sortMode, setSortMode,
    selectedRowId, setSelectedRowId,
    sources, entities, counts, filteredRows, selectedRow, relevantPermissions,
  }), [
    rows, permissions, confidence, activeTab, search, sourceFilter, entityFilter, sortMode, selectedRowId,
    sources, entities, counts, filteredRows, selectedRow, relevantPermissions,
  ]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useNormalizationContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useNormalizationContext must be used inside NormalizationProvider');
  return ctx;
}