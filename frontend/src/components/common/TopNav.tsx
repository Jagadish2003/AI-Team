import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { User, ChevronDown } from 'lucide-react';
import { useRunContext } from "../../context/RunContext";
import logo from "../../../images/AgentIQ-logo.svg";
// added

const items = [
  { to: '/integration-hub', label: 'Integration Hub', runScoped: false },
  { to: '/source-intake', label: 'Source Intake', runScoped: false },

  //  run-scoped screens
  { to: '/discovery-run', label: 'Discovery Run', runScoped: true },
  { to: '/partial-results', label: 'Evidence Collection', runScoped: true },
  { to: '/normalization', label: 'Normalization', runScoped: true }, 
  { to: '/analyst-review', label: 'Analyst Review', runScoped: true },
  { to: '/opportunity-map', label: 'Opportunity Map', runScoped: true },
  { to: '/pilot-roadmap', label: 'Pilot Roadmap', runScoped: true },
  { to: '/executive-report', label: 'Executive Report', runScoped: true },
];

export default function TopNav() {
  const loc = useLocation();
  const { runId } = useRunContext();

  return (
    <div className="sticky top-0 z-40 border-b border-border bg-bgheader shadow-[0_2px_8px_rgba(0,0,0,0.15)] backdrop-blur">
      <div className="mx-auto flex w-full items-center px-6 py-3">

        {/* Brand */}
        <div className="flex items-center gap-1 shrink-0">
          <div className="h-7 w-7 rounded-md"/>
          {/* Added Logo and changed header color */}
          <div className="text-[22px] font-semibold text-text"><img src={logo} alt="AgentIQ Logo" className="w-auto"/></div>
        </div>

        {/* Nav items */}
        <div className="ml-auto flex items-center gap-1">
          {items.map((i, idx) => {
            const isActive = loc.pathname === i.to;

            // Preserve runId only for run-scoped pages
            const to = i.runScoped && runId
              ? `${i.to}?runId=${runId}`
              : i.to;

            return (
              <React.Fragment key={i.to}>
                <Link
                  to={to}
                  className={`whitespace-nowrap rounded-md px-2.5 py-1.5 font-medium transition-colors ${
                    isActive
                      ? 'border-navborder border-t-2 text-text bg-gradient-to-b from-activenav '
                      : 'text-muted hover:bg-navhover hover:text-text'
                  }`}
                  style={{ fontSize: '14px', padding: '6px 12px', borderRadius: '100px' }}
                >
                  {i.label}
                </Link>
              </React.Fragment>
            );
          })}
          {/* Profile */}
          <div className="flex h-7 w-7 cursor-pointer items-center justify-center rounded-full transition-colors hover:bg-navhover hover:text-text">
            <User className="h-4 w-4 text-slate-400" />
          </div>
        </div>
      </div>
    </div>
  );
}