import React from "react";
import { InfoPanel } from "./InfoPanel";

export function RunRequiredEmptyState({ onStart }: { onStart: () => void }) {
  return (
    <InfoPanel
      title="No discovery run selected"
      message={
        <>
          This screen is tied to a specific discovery run. Start a run from the{" "}
          <span className="font-medium text-text">Discovery Run</span> page to continue.
        </>
      }
      actionLabel="Go to Discovery Run"
      onAction={onStart}
    />
  );
}
