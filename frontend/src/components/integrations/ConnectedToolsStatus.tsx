import React, { useState } from 'react';
import { ChevronDown, CircleCheck } from 'lucide-react';
import { Connector } from '../../types/connector';

function ToolPill({ name }: { name: string }) {
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5 rounded-full bg-accent/10 px-2.5 py-1 text-xs font-medium text-text">
      <CircleCheck size={13} strokeWidth={2.2} className="shrink-0 text-accent" />
      <span className="truncate">{name}</span>
    </span>
  );
}

function ConnectedCountPill({ count }: { count: number }) {
  return (
    <span className="integration-connected-count-pill inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs leading-none">
      <CircleCheck size={14} strokeWidth={2.1} className="shrink-0" />
      {count} Connected
    </span>
  );
}

export default function ConnectedToolsStatus({
  connectors,
}: {
  connectors: Connector[];
}) {
  const [open, setOpen] = useState(false);
  const connected = connectors.filter((connector) => connector.status === 'connected');

  if (connected.length === 0) return null;

  if (connected.length <= 3) {
    return (
      <div className="flex max-w-full shrink-0 flex-wrap items-center justify-end gap-1.5">
        {connected.map((connector) => (
          <ToolPill key={connector.id} name={connector.name} />
        ))}
        <ConnectedCountPill count={connected.length} />
      </div>
    );
  }

  return (
    <div className="flex max-w-full shrink-0 items-center justify-end gap-1.5">
      <div className="relative shrink-0">
        <button
          type="button"
          onClick={() => setOpen((current) => !current)}
          className="inline-flex items-center gap-2 rounded-full border border-accent/45 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-text transition-colors hover:border-accent/60 hover:bg-accent/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
          aria-expanded={open}
        >
          Connected Sources: {String(connected.length).padStart(2, '0')}
          <ChevronDown
            size={15}
            strokeWidth={2.3}
            className={`text-accent transition-transform ${open ? 'rotate-180' : ''}`}
          />
        </button>

        {open && (
          <div className="absolute right-0 z-30 mt-2 w-48 rounded-lg border border-border/45 bg-panel px-3 py-2 shadow-lg">
            {connected.map((connector, index) => (
              <div
                key={connector.id}
                className={`flex items-center gap-2 px-1 py-2 text-sm text-text ${
                  index > 0 ? 'border-t border-border/35' : ''
                }`}
              >
                <CircleCheck size={13} strokeWidth={2.2} className="shrink-0 text-accent" />
                <span className="truncate">{connector.name}</span>
              </div>
            ))}
          </div>
        )}
      </div>
      <ConnectedCountPill count={connected.length} />
    </div>
  );
}
