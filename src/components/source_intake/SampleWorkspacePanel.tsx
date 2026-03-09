import React from 'react';
import Button from '../common/Button';
import { Check } from 'lucide-react';

function CheckItem({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted">
      <Check size={16} className="shrink-0 text-muted" />
      <span>{children}</span>
    </div>
  );
}

export default function SampleWorkspacePanel({
  enabled,
  onEnable,
  onLearnMore
}: {
  enabled: boolean;
  onEnable: () => void;
  onLearnMore: () => void;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      {/* Title */}
      <div className="text-[13px] font-medium text-text">
        Or Start Fresh (Sample Workspace)
      </div>

      {/* Description */}
      <div className="mt-2 text-xs leading-5 text-muted">
        Load one-click sample data to preview 

        discovery features.
      </div>

      {/* Sample Sources block */}
      <div className="mt-4 rounded-xl border border-border bg-bg/30 p-4">
        <div className="space-y-1.5">
          <CheckItem>Sample Sources:</CheckItem>
          <CheckItem>ServiceNow</CheckItem>
          <CheckItem>Jira &amp; Confluence</CheckItem>
          <CheckItem>Microsoft 365</CheckItem>
        </div>
      </div>

      {/* Start Fresh button */}
      <div className="mt-5">
        <Button className="w-full" onClick={onEnable}>
          {enabled ? 'Sample Workspace Enabled' : 'Start Fresh (Sample Data)'}
        </Button>
      </div>

      {/* Learn More */}
      <div className="mt-2 text-center">
        <button
          onClick={onLearnMore}
          className="text-[11px] text-muted hover:text-text"
        >
          Learn More
        </button>
      </div>
    </div>
  );
}