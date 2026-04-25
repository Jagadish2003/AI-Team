import React from "react";

export function RunRequiredEmptyState({ onStart }: { onStart: () => void }) {
  return (
    <div className="flex items-center justify-center h-[75vh]">
      <div className="rounded-xl border border-white/20 bg-panel p-8 py-12 text-center shadow-xl shadow-black/20">
        <h2 className="text-xl font-semibold text-text mb-4">
          No discovery run selected
        </h2>
        <p className="text-sm text-muted mb-6 leading-relaxed">
          This screen is tied to a specific discovery run. Start a run from the{" "}
          <span className="font-medium text-text">Discovery Run</span> page to continue.
        </p>
        <button
          onClick={onStart}
          className="px-6 py-2.5 text-sm font-medium text-white bg-accent rounded-lg hover:opacity-90 hover:scale-[1.02] active:scale-[0.98] transition-all duration-200">
          Go to Discovery Run
        </button>
      </div>
    </div>
  );
}