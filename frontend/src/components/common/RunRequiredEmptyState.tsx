import React from "react";

export function RunRequiredEmptyState({ onStart }: { onStart: () => void }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-6">
      <div className="text-lg font-semibold text-text">No discovery run selected</div>
      <div className="mt-2 text-sm text-muted">
        This screen is tied to a specific discovery run. Start a run from <span className="font-semibold">Discovery Run</span> to continue.
      </div>
      <button
        className="mt-4 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white hover:opacity-90"
        onClick={onStart}
      >
        Go to Discovery Run
      </button>
    </div>
  );
}
