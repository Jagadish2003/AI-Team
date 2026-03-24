import React, { useMemo, useState } from 'react';
import { Search, AlertTriangle, Check, ChevronDown } from 'lucide-react';
import { useNormalizationContext } from '../../context/NormalizationContext';

type Props = {
  selectedSources: Set<string>;
  setSelectedSources: React.Dispatch<React.SetStateAction<Set<string>>>;
  selectedEntities: Set<string>;
  setSelectedEntities: React.Dispatch<React.SetStateAction<Set<string>>>;
};

const TealCheckbox = ({ checked, hovered }: { checked: boolean; hovered: boolean }) => (
  <span style={{
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
    width: 16, height: 16, minWidth: 16, borderRadius: 3, flexShrink: 0,
    transition: 'all 0.15s ease',
    backgroundColor: checked ? (hovered ? '#5eead4' : '#2dd4bf') : 'transparent',
    border: `2px solid ${checked ? (hovered ? '#5eead4' : '#2dd4bf') : (hovered ? '#cbd5e1' : '#4b5563')}`,
  }}>
    {checked && (
      <svg viewBox="0 0 10 8" style={{ width: 9, height: 7 }} fill="none"
        stroke="#0f172a" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="1,4 3.5,6.5 9,1" />
      </svg>
    )}
  </span>
);

export default function SourcesEntitiesPanel({ selectedSources, setSelectedSources, selectedEntities, setSelectedEntities }: Props) {
  const { rows, sources, entities, setSourceFilter, setEntityFilter } = useNormalizationContext();
  const [search, setSearch] = useState('');
  const [entitiesExpanded, setEntitiesExpanded] = useState(true);
  const[hoveredSource, setHoveredSource] = useState<string | null>(null);
  const [hoveredEntity, setHoveredEntity] = useState<string | null>(null);

  const applyFilter = (set: Set<string>, empty: string) =>
    set.size === 0 ? empty : Array.from(set).join(',');

  const toggleItem = (
    name: string,
    selected: Set<string>,
    setSelected: React.Dispatch<React.SetStateAction<Set<string>>>,
    setFilter: (v: string) => void,
    empty: string
  ) => {
    const next = new Set(selected);
    next.has(name) ? next.delete(name) : next.add(name);
    setSelected(next);
    setFilter(applyFilter(next, empty));
  };

  const sourceItems = useMemo(() => sources
    .filter(s => s !== 'All Sources')
    .map(s => {
      const raw = s || '';
      return {
        id: raw ? raw.toLowerCase().replace(/\s+/g, '-') : 'blank-source', 
        name: s,
        displayName: raw ? raw : '—',
        status: rows.some(r => r.sourceSystem === s && r.status !== 'MAPPED') ? 'warning' : 'ok' as 'warning' | 'ok',
      };
    }), [rows, sources]);

  const entityItems = useMemo(() => entities
    .filter(e => e !== 'All Entities')
    .map(e => {
      const raw = e || '';
      return {
        id: raw ? raw.toLowerCase().replace(/\s+/g, '-') : 'blank-entity', 
        name: e,
        displayName: raw ? raw : '—', // ✅ If empty or null, display '—'
        count: rows.filter(r => (selectedSources.size === 0 || selectedSources.has(r.sourceSystem)) && r.commonEntity === e).length,
      };
    }), [entities, rows, selectedSources]);

  const filteredSources = useMemo(() => {
    const q = search.trim().toLowerCase();
    return q ? sourceItems.filter(s => s.displayName.toLowerCase().includes(q)) : sourceItems;
  }, [search, sourceItems]);

  const Row = ({ id, onClick, onEnter, onLeave, children }: any) => (
    <div key={id} onClick={onClick} onMouseEnter={onEnter} onMouseLeave={onLeave}
      className="flex items-center rounded-md px-2 py-2 text-sm cursor-pointer w-full transition-colors duration-150">
      {children}
    </div>
  );

  return (
    <div className="rounded-xl border border-border bg-panel p-4 h-full flex flex-col gap-3">
      <div className="text-xl font-semibold text-text">Sources &amp; Entities</div>

      <div className="relative">
        <input type="text" placeholder="Search sources..." value={search}
          onChange={e => setSearch(e.target.value)}
          className="w-full rounded-md border border-border bg-bg/30 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:bg-bg/50 hover:border-accent/50 transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 appearance-none" />
        <Search className="absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted pointer-events-none" />
      </div>

      <div className="flex flex-col gap-0.5">
        <div className="flex items-center justify-between px-1 mb-1">
          <p className="text-xs font-semibold uppercase tracking-widest text-muted">Sources</p>
          {selectedSources.size > 0 && (
            <button onClick={() => { setSelectedSources(new Set()); setSourceFilter('All Sources'); }}
              className="text-xs text-muted hover:text-text transition-colors">Clear</button>
          )}
        </div>
        {filteredSources.map(s => (
          <Row key={s.id}
            onClick={() => toggleItem(s.name, selectedSources, setSelectedSources, setSourceFilter, 'All Sources')}
            onEnter={() => setHoveredSource(s.name)} onLeave={() => setHoveredSource(null)}>
            <span className="flex items-center gap-2 min-w-0 flex-1">
              <TealCheckbox checked={selectedSources.has(s.name)} hovered={hoveredSource === s.name} />
              <span className="text-text truncate select-none">{s.displayName}</span>
            </span>
            {s.status === 'warning'
              ? <AlertTriangle size={14} className="text-amber-400 shrink-0" />
              : <Check size={13} strokeWidth={2.5} className="text-emerald-400 shrink-0" />}
          </Row>
        ))}
        {filteredSources.length === 0 && <div className="px-2 py-2 text-sm text-muted">No sources match your search.</div>}
      </div>

      <div className="border-t border-border" />

      <div className="flex flex-col gap-0.5">
        <button type="button" onClick={() => setEntitiesExpanded(v => !v)}
          className="flex items-center justify-between w-full px-1 mb-1 text-muted hover:text-text transition-colors">
          <span className="text-xs font-semibold uppercase tracking-widest">Common Entities</span>
          <ChevronDown size={13} strokeWidth={2} style={{ transform: entitiesExpanded ? 'rotate(0deg)' : 'rotate(-90deg)', transition: 'transform 0.2s ease' }} />
        </button>
        {entitiesExpanded && entityItems.map(e => (
          <Row key={e.id}
            onClick={() => toggleItem(e.name, selectedEntities, setSelectedEntities, setEntityFilter, 'All Entities')}
            onEnter={() => setHoveredEntity(e.name)} onLeave={() => setHoveredEntity(null)}>
            <span className="flex items-center gap-2 flex-1 min-w-0">
              <TealCheckbox checked={selectedEntities.has(e.name)} hovered={hoveredEntity === e.name} />
              <span className="text-text select-none truncate">{e.displayName}</span>
            </span>
            <span className="rounded-full border border-border px-2 py-0.5 text-xs text-muted font-medium">{e.count}</span>
          </Row>
        ))}
      </div>
    </div>
  );
}