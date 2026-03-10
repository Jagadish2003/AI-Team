import React from 'react';
import { MonitorCheck, Check, ChevronRight, Info } from 'lucide-react';
import Button from '../common/Button';

export default function ManagedAgentPanel({
  onDownload,
  onGuide
}: {
  onDownload: () => void;
  onGuide: () => void;
}) {
  return (
    <div className="w-full max-w-md rounded-xl border border-border bg-panel p-6">
      <div className="mb-5">
        <h2 className="mb-1 text-xl font-semibold text-text">Launch Managed Agent</h2>
        <p className="text-xs leading-relaxed text-muted">
          For systems that can't expose APIs externally. Install on your gateway VM.
        </p>
      </div>

      <div className="mb-5 flex flex-col items-center rounded-lg border border-dashed border-border bg-bg/20 px-6 py-8 text-center">
        <MonitorCheck className="text-muted/60" size={48} strokeWidth={1.5} />
      </div>

      <div className="mb-5 rounded-lg border border-border bg-bg/5 p-4">
        <div className="mb-4 flex items-center gap-2 text-white/90">
          <Info size={14} className="text-teal-400" />
          <span className="text-sm font-semibold">What it does</span>
        </div>

        <ul className="space-y-4">
          {[
            'Runs inside your network and pulls metadata securely.',
            'Ships only approved evidence to AgentIQ.',
            'Useful for SAP / Oracle / MES and restricted systems.'
          ].map((text, i) => (
            <li key={i} className="flex items-start gap-3">
              <Check size={14} className="mt-0.5 shrink-0 text-teal-400" />
              <span className="text-[13px] leading-snug text-slate-300">{text}</span>
            </li>
          ))}
        </ul>

        <div className="mt-4 border-t border-border/50 pt-4">
          <div className="mb-2 text-[10px] font-bold uppercase tracking-wider text-muted">
            Common restricted sources
          </div>
          <div className="flex flex-wrap gap-2">
            {['SAP', 'MES Systems', 'Oracle'].map((x) => (
              <span
                key={x}
                className="rounded-md border border-slate-700/50 bg-[#0B121E] px-2 py-0.5 text-[10px] text-slate-400"
              >
                {x}
              </span>
            ))}
          </div>
        </div>
      </div>

      <Button
        className="flex w-full items-center justify-center gap-2 py-3 text-sm shadow-lg shadow-teal-500/10"
        onClick={onDownload}
      >
        Download Connector Agent
        <ChevronRight size={16} />
      </Button>

      <button
        onClick={onGuide}
        className="mt-4 w-full text-[11px] font-medium text-muted transition-colors hover:text-white"
      >
        Installation Guide
      </button>
    </div>
  );
}