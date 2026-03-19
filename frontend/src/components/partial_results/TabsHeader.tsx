import React from 'react';

export default function TabsHeader({
  tab,
  onTab
}: {
  tab: 'Entities' | 'Opportunities';
  onTab: (t: 'Entities' | 'Opportunities') => void;
}) {
  return (
    <div className="inline-flex overflow-hidden rounded-sm border border-border bg-panel">
      <button
        onClick={() => onTab('Entities')}
        className={`px-4 py-1.5 text-sm font-medium transition ${
          tab === 'Entities' ? 'bg-white text-gray-800' : 'bg-panel text-muted hover:bg-surface hover:text-text'
        }`}
      >
        Entities
      </button>

      <button
        onClick={() => onTab('Opportunities')}
        className={`border-l border-border px-4 py-1.5 text-sm font-medium transition ${
          tab === 'Opportunities'
            ? 'bg-white text-gray-800'
            : 'bg-panel text-muted hover:bg-surface hover:text-text'
        }`}
      >
        Opportunities (preview)
      </button>
    </div>
  );
}