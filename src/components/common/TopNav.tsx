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
  { to: '/reports', label: 'Reports' }
];

export default function TopNav() {
  const loc = useLocation();
  return (
    <div className="sticky top-0 z-40 border-b border-border bg-bg/80 shadow-[0_2px_8px_rgba(0,0,0,0.15)] backdrop-blur">
      <div className="mx-auto flex w-full items-center px-6 py-3">
        <div className="ml-3 flex items-center gap-2">
          <div className="h-7 w-7 rounded-md bg-accent/20" />
          <div className="text-[24px] font-semibold text-text">AgentIQ</div>
        </div>
        <div className="ml-auto flex items-center text-sm">
          {items.map((i) => {
            const active = loc.pathname === i.to;
            return (
              <div key={i.to} className="mr-4 border-r border-muted/40 pr-4 last:mr-0 last:border-r-0 last:pr-0">
                <Link
                  to={i.to}
                  className={`rounded-md px-3 py-2 ${
                    active ? 'bg-panel2 text-text' : 'text-muted hover:bg-panel2 hover:text-text'
                  }`}
                >
                  {i.label}
                </Link>
              </div>
            );
          })}
          <div className="mr-4 border-r border-muted/40 pr-4 flex cursor-pointer items-center gap-1 rounded-md px-3 py-2 text-muted hover:bg-panel2 hover:text-text">
            Administrator
            <ChevronDown size={16} strokeWidth={2.5} className="text-slate-400" />
          </div>
          <div className="flex h-8 w-8 cursor-pointer items-center justify-center rounded-full hover:bg-panel2 hover:text-text">
            <User className="h-5 w-5 text-slate-400" />
          </div>
        </div>
      </div>
    </div>
  );
}