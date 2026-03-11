import React from 'react';
import Button from '../common/Button';
import { ExtractedEntity, EntityType } from '../../types/partialResults';

export default function EntitiesSidebar({
  entities,
  countsByType,
  enabledTypes,
  onTypeToggle,
  query,
  onQuery,
  selectedEntityIds,
  onToggleEntity,
  onClear
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
  const typeList: EntityType[] = ['Application','Workflow','Service','Role','DataObject','Other'];

  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-semibold text-text">Extracted Entities</div>

      <div className="mt-3">
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search entities…"
          className="w-full rounded-md border border-border bg-bg/30 px-3 py-2 text-sm text-text placeholder:text-muted focus:outline-none focus:border-[#00B4B4]"
        />
      </div>

      <div className="mt-4">
        <div className="text-xs font-semibold text-text">Type</div>
        <div className="mt-2 space-y-2 text-sm text-muted">
          {typeList.map(t => (
            <label key={t} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <input type="checkbox" className="accent-blue-500" checked={enabledTypes[t]} onChange={(e) => onTypeToggle(t, e.target.checked)} />
                {t}
              </span>
              <span className="text-xs text-muted">({countsByType[t] ?? 0})</span>
            </label>
          ))}
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between">
        <div className="text-xs font-semibold text-text">Top Entities</div>
        <Button variant="ghost" onClick={onClear} className="text-xs">Clear</Button>
      </div>

      <div className="mt-2 space-y-2">
        {entities.slice(0, 12).map(e => {
          const selected = selectedEntityIds.includes(e.id);
          return (
            <div
              key={e.id}
              onClick={() => onToggleEntity(e.id)}
              className={`cursor-pointer rounded-lg border p-2 ${
                selected ? 'border-accent/60 bg-panel2' : 'border-border bg-bg/20 hover:bg-panel2'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold text-text">{e.name}</div>
                  <div className="text-xs text-muted">{e.type} · {e.mentionCount} mentions</div>
                </div>
                <div className="text-xs text-muted">{e.confidence}</div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
