import React from 'react';
import { Check } from 'lucide-react';

export default function ReadySourcesSummary({
  connectedNames,
  fileCount,
  sampleEnabled
}: {
  connectedNames: string[];
  fileCount: number;
  sampleEnabled: boolean;
}) {
  const connectedItems = connectedNames.length ? connectedNames.slice(0, 2) : [];
  const fileItem = fileCount > 0 ? `${fileCount} files` : null;
  const sampleItem = sampleEnabled ? 'Sample workspace' : null;

  const items = [
    ...connectedItems,
    ...(fileItem ? [fileItem] : []),
    ...(sampleItem ? [sampleItem] : [])
  ];

  const total = connectedNames.length + fileCount + (sampleEnabled ? 1 : 0);

  return (
    <div className="rounded-xl border border-border bg-panel p-3 text-sm text-muted">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <span className="font-semibold text-text">Ready to discover from:</span>

        {items.length ? (
          items.map((item, index) => (
            <div key={`${item}-${index}`} className="flex items-start gap-2 text-sm text-muted">
              <Check size={16} className="mt-[2px] shrink-0 text-muted" />
              <span>{item}</span>
            </div>
          ))
        ) : (
          <span>No sources yet</span>
        )}

        <span className="text-text">{total} sources total</span>
      </div>
    </div>
  );
}