import React from 'react';

export default function TabsHeader({ tab, onTab }: { tab: 'Entities' | 'Opportunities'; onTab: (t: 'Entities' | 'Opportunities') => void; }) {
  const base = 'px-4 py-2 text-sm font-semibold border-b-2';
  return (
    <div className="flex gap-2">
      <button className={`${base} ${tab === 'Entities' ? 'border-accent text-text' : 'border-transparent text-muted hover:text-text'}`} onClick={() => onTab('Entities')}>
        Entities
      </button>
      <button className={`${base} ${tab === 'Opportunities' ? 'border-accent text-text' : 'border-transparent text-muted hover:text-text'}`} onClick={() => onTab('Opportunities')}>
        Opportunities (preview)
      </button>
    </div>
  );
}
