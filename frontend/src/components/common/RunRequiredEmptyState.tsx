import React from "react";
import { InfoPanel } from "./InfoPanel";

type RunRequiredEmptyStateProps = {
  onStart: () => void;
  pageTitle?: React.ReactNode;
  pageDescription?: React.ReactNode;
};

export function RunRequiredEmptyState({
  onStart,
  pageTitle,
  pageDescription,
}: RunRequiredEmptyStateProps) {
  return (
    <>
      {pageTitle && (
        <div className="mb-4">
          <div className="text-2xl font-semibold text-text">{pageTitle}</div>
          {pageDescription && (
            <div className="mt-1 text-sm text-muted">{pageDescription}</div>
          )}
        </div>
      )}
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
    </>
  );
}
