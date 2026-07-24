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
            This screen is tied to a specific discovery run. Connect a source in the{" "}
            <span className="font-medium text-text">Integration Hub</span>, then start a
            discovery run to continue.
          </>
        }
        actionLabel="Go to Integration Hub"
        onAction={onStart}
      />
    </>
  );
}
