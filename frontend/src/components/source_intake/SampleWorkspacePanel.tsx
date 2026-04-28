import React from 'react';
import { LayoutDashboard, Check, ChevronRight, Database } from 'lucide-react';
import Button from '../common/Button';

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
    <div className="w-full max-w-md rounded-xl border border-border bg-panel p-6 flex flex-col">
      <div className="mb-5">
        <h2 className="mb-1 text-xl font-semibold text-text">Start Fresh</h2>
        <p className="text-xs leading-relaxed text-muted">
          Use a safe sample workspace for demo/evaluation (no customer data required).
        </p>
      </div>

      <div className="mb-5 flex flex-col items-center rounded-lg border border-dashed border-border bg-bg/20 px-6 py-8 text-center">
        <LayoutDashboard className="text-muted/60" size={48} strokeWidth={1.5} />
      </div>

      <div className="mb-5 rounded-lg border border-border bg-bg/5 p-4">
        <div className="mb-4 flex items-center gap-2 text-white/90">
          <Database size={14} className="text-accent" />
          <span className="text-sm font-semibold">Sample sources</span>
        </div>

        <ul className="space-y-4">
          {[
            'ServiceNow (Incidents + CMDB)',
            'Jira & Confluence (Issues + Runbooks)',
            'Microsoft 365 (Inbox + Teams)'
          ].map((text, i) => (
            <li key={i} className="flex items-start gap-3">
              <Check size={14} className="mt-0.5 shrink-0 text-accent" />
              <span className="text-[13px] leading-snug text-slate-300">{text}</span>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex flex-col items-center">
        <Button
          className="flex w-full items-center justify-center gap-2 py-3 text-sm shadow-lg shadow-accent-500/10"
          onClick={onEnable}
          disabled={enabled}
        >
          {enabled ? 'Sample Workspace Enabled' : 'Start Fresh (Sample Data)'}
          <ChevronRight size={16} />
        </Button>

        <button
          onClick={onLearnMore}
          className="mt-4 text-[11px] font-medium text-muted transition-colors hover:text-white"
        >
          Learn more about sample data
        </button>
      </div>

      <div className="flex flex-1 items-center justify-center">
        <p className="text-center text-[10px] leading-tight text-muted/60">
          Sample data is isolated and can be removed at any time from your settings.
        </p>
      </div>
    </div>
  );
}