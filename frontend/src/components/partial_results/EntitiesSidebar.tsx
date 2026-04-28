import React from 'react';
import Button from '../common/Button';
import { ExtractedEntity, EntityType } from '../../types/partialResults';
import { Search } from 'lucide-react';

function confidenceClass(value: string) {
  const cls =
    value === 'HIGH' ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300'
    : value === 'MEDIUM' ? 'border-amber-500/50 bg-amber-500/15 text-amber-200'
    : 'border-red-500/50 bg-red-500/15 text-red-300';
  return `rounded-full border px-2 py-0.5 text-[10px] font-semibold tracking-wide whitespace-nowrap ${cls}`;
}

export default function EntitiesSidebar({
  entities, countsByType, enabledTypes, onTypeToggle,
  query, onQuery, selectedEntityIds, onToggleEntity, onClear
}: {
  entities: ExtractedEntity[];
  countsByType: Record<EntityType, number>;
  enabledTypes: Record<EntityType, boolean>;
  onTypeToggle: (t: EntityType, v: boolean) => void;
  query: string;
  onQuery: (v: string) => void;
  selectedEntityIds: string[];
  onToggleEntity: (id: string) => void;
  onClear: () => void;
}) {
  const typeList: EntityType[] = ['Application', 'Workflow', 'Service', 'Role', 'DataObject', 'Other'];

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xl font-semibold text-text pb-3">Extracted Entities</div>

      {/* Search */}
      <div className="relative">
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search entities…"
          className="w-full rounded-md border border-border bg-bg/50 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted hover:border-[#0D55D7]/50 transition-colors focus:outline-none focus:border-[#0D55D7] focus:ring-2 focus:ring-[#0D55D7]/50 appearance-none"
        />
        <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted pointer-events-none" />
      </div>

      {/* Entity Types */}
      <div className="mt-4">
        <div className="text-md font-semibold text-text">Entity Types</div>
        <div className="mt-2 space-y-2 text-sm text-muted">
          {typeList.map((t) => (
            <label key={t} className="flex items-center justify-between gap-2 cursor-pointer">
              <span className="flex items-center gap-2">
                <input
                  type="checkbox"
                  className="accent-[#0D55D7]"
                  checked={enabledTypes[t]}
                  onChange={(e) => onTypeToggle(t, e.target.checked)}
                />
                {t}
              </span>
              <span className="text-xs text-muted">({countsByType[t] ?? 0})</span>
            </label>
          ))}
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between">
        <div className="text-md font-semibold text-text">Top Entities</div>
        <Button variant="ghost" onClick={onClear} className="text-xs">Clear</Button>
      </div>

      <div className="mt-2 max-h-[320px] overflow-y-auto space-y-2 pr-1 mb-2">
        {entities.slice(0, 12).map((e) => {
          const selected = selectedEntityIds.includes(e.id);
          return (
            <div
              key={e.id}
              onClick={() => onToggleEntity(e.id)}
              className={`cursor-pointer rounded-lg border p-2 ${
                selected 
                  ? 'border-accent/60 bg-activenav' 
                  : 'border-border bg-bg/20 hover:bg-panel2'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0 space-y-1">
                  <div className="truncate text-sm font-semibold text-text">{e.name}</div>
                  <div className="text-xs text-muted">{e.type} · {e.mentionCount} mentions</div>
                </div>
                <span className={confidenceClass(e.confidence)}>{e.confidence}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}