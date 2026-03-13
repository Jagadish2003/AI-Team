import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { User, ChevronDown } from 'lucide-react';

const items = [
  { to: '/integration-hub', label: 'Integration Hub' },
  { to: '/source-intake', label: 'Source Intake' },
  { to: '/discovery-run', label: 'Discovery Run' },
  { to: '/normalization', label: 'Normalization' },
  { to: '/partial-results', label: 'Partial Results' },
  { to: '/analyst-review', label: 'Analyst Review' },
  { to: '/opportunity-map', label: 'Opportunity Map' },
  { to: '/reports', label: 'Reports' },
];

export default function TopNav() {
  const loc = useLocation();
  return (
    <div className="sticky top-0 z-40 border-b border-border bg-bg/80 shadow-[0_2px_8px_rgba(0,0,0,0.15)] backdrop-blur">
      <div className="mx-auto flex w-full items-center px-6 py-3">

        {/* Brand */}
        <div className="flex items-center gap-2 shrink-0">
          <div className="h-7 w-7 rounded-md bg-accent/20" />
          <div className="text-[22px] font-semibold text-text">AgentIQ</div>
        </div>

        {/* Nav items */}
        <div className="ml-auto flex items-center gap-1">
          {items.map((i, idx) => {
            const active = loc.pathname === i.to;
            return (
              <React.Fragment key={i.to}>
                <Link
                  to={i.to}
                  className={`whitespace-nowrap rounded-md px-2.5 py-1.5 font-medium transition-colors ${
                    active
                      ? 'bg-panel2 text-text'
                      : 'text-muted hover:bg-panel2 hover:text-text'
                  }`}
                  style={{ fontSize: '14px' }}
                >
                  {i.label}
                </Link>
                {/* Divider after every item except the last */}
                {idx < items.length - 1 && (
                  <span className="h-4 w-px bg-muted/30" />
                )}
              </React.Fragment>
            );
          })}

          {/* Single divider before Administrator */}
          <span className="h-4 w-px bg-muted/30 mx-1" />

          <button
            className="flex cursor-pointer items-center gap-1 rounded-md px-2.5 py-1.5 font-medium text-muted transition-colors hover:bg-panel2 hover:text-text"
            style={{ fontSize: '14px' }}
          >
            Administrator
            <ChevronDown size={14} strokeWidth={2.5} className="text-slate-400" />
          </button>

          <span className="h-4 w-px bg-muted/30 mx-1" />

          <div className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-full transition-colors hover:bg-panel2 hover:text-text">
            <User className="h-4 w-4 text-slate-400" />
          </div>
        </div>

      </div>
    </div>
  );
}