/**
 * PickerSkeleton — shared loading skeleton for the Integration Hub connector
 * pickers (Salesforce products, Jira projects, Slack/Teams channels, Confluence
 * spaces, SharePoint sites, GitHub repos).
 *
 * Mirrors the picker layout — a header row (title + "per workspace" tag), two
 * description lines, a few option-row placeholders, and the save button — so the
 * real content fills the same space instead of popping in under the panel.
 */
import React from 'react';
import { Skeleton } from '../common/Skeleton';

interface Props {
  /** Number of option-row placeholders to show (default 3). */
  rows?: number;
  /** Accessible label for the busy region. */
  label?: string;
}

export default function PickerSkeleton({ rows = 3, label = 'Loading…' }: Props) {
  return (
    <div className="mt-4" aria-busy="true" aria-label={label}>
      {/* Header row: title + per-workspace tag */}
      <div className="mb-2 flex items-center justify-between">
        <Skeleton className="h-4 w-40" />
        <Skeleton className="h-3 w-24" />
      </div>

      {/* Description (two lines) */}
      <Skeleton className="mb-1 h-3 w-full" />
      <Skeleton className="mb-3 h-3 w-3/4" />

      {/* Option rows */}
      <div className="space-y-1.5">
        {Array.from({ length: rows }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full rounded-lg" />
        ))}
      </div>

      {/* Save button */}
      <Skeleton className="mt-3 h-9 w-full rounded-lg" />
    </div>
  );
}
