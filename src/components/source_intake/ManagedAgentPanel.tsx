import React from "react";
import Button from "../common/Button";
import { Check, ChevronRight } from "lucide-react";
 
function CheckItem({ children }) {
  return (
    <div className="flex items-start gap-2 text-sm text-muted">
      <Check size={16} className="mt-[2px] shrink-0 text-muted" />
      <span>{children}</span>
    </div>
  );
}
 
function CircularCheckItem({ children }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted">
      <span className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-border bg-panel2">
        <Check size={10} className="text-muted" />
      </span>
      <span>{children}</span>
    </div>
  );
}
 
export default function ManagedAgentPanel({
  onDownload,
  onGuide
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
 
      <div className="text-sm font-semibold text-text">
        Launch Managed Agent
      </div>
 
      <div className="mx-2 mt-2 rounded-md bg-gradient-to-b from-transparent via-panel2/20 to-transparent">
        <div className="h-px bg-gradient-to-r from-transparent via-border/90 to-transparent" />
 
        <div className="px-3 py-2 text-xs text-muted">
          For systems that can't expose APIs externally. Install on your gateway VM.
        </div>
 
        <div className="h-px bg-gradient-to-r from-transparent via-border/90 to-transparent" />
      </div>
 
      <div className="mt-4 rounded-xl border border-border bg-bg/30 p-4 text-sm text-muted">
 
        <div className="text-sm font-semibold text-text">
          What it does
        </div>
 
        <div className="mt-2 space-y-1">
          <CheckItem>
            Runs inside your network and pulls metadata securely.
          </CheckItem>
 
          <CheckItem>
            Ships only approved evidence to AgentIQ.
          </CheckItem>
 
          <CheckItem>
            Useful for SAP / Oracle / MES and other restricted systems.
          </CheckItem>
        </div>
 
      </div>
 
      <div className="mt-4 text-xs font-semibold text-text">
        Common restricted sources
      </div>
 
      <div className="mt-2 space-y-2">
        <CircularCheckItem>SAP</CircularCheckItem>
        <CircularCheckItem>MES Systems</CircularCheckItem>
        <CircularCheckItem>Oracle</CircularCheckItem>
      </div>
 
      <div className="mt-4">
 
        <div className="mx-2 h-px bg-gradient-to-r from-transparent via-border/90 to-transparent" />
 
        <div className="mt-4 flex flex-col items-stretch gap-2">
 
          {/* Button with Arrow */}
          <Button
            variant="secondary"
            className="w-full flex items-center justify-center gap-2"
            onClick={onDownload}
          >
            Download Connector Agent
            <ChevronRight size={16} />
          </Button>
 
          <button
            type="button"
            className="text-sm text-muted"
            onClick={onGuide}
          >
            Installation guide
          </button>
 
        </div>
 
      </div>
 
    </div>
  );
} 