import React from 'react';

type Tab = 'MAPPED' | 'UNMAPPED' | 'AMBIGUOUS';

export default function MappingTabs({
  active,
  counts,
  onTab
}: {
  active: Tab;
  counts: Record<Tab, number>;
  onTab: (t: Tab) => void;
}) {
  const tab = (id: Tab, label: string) => (
    <button
      key={id}
      onClick={() => onTab(id)}
      className={`w-full rounded-md border px-3 py-2.5 text-sm font-semibold transition-colors ${
        active === id
          ? 'border-accent/60 bg-panel2 text-text'
          : 'border-border bg-bg/20 text-muted hover:border-accent/50 hover:text-accent'
      }`}
    >
      {label} ({counts[id]})
    </button>
  );

  return (
    <div className="grid grid-cols-3 gap-3">
      {tab('MAPPED', 'Mapped')}
      {tab('UNMAPPED', 'Unmapped')}
      {tab('AMBIGUOUS', 'Ambiguous')}
    </div>
  );
}
