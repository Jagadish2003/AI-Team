import React from "react";

export function RunRequiredEmptyState({ onStart }: { onStart: () => void }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-6">
      <div className="text-lg font-semibold text-text">No discovery run selected</div>
      <div className="mt-2 text-sm text-muted">
        Start a discovery run to view run-scoped results (events, evidence, opportunities).
      </div>
      <button
        className="mt-4 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white hover:opacity-90"
        onClick={onStart}
      >
        Start Discovery Run
      </button>
    </div>
  );
}
