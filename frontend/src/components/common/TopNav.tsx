import React from 'react';
import { Link, useLocation } from 'react-router-dom';
import { User, Zap } from 'lucide-react';
import { useRunContext } from "../../context/RunContext";
import { useConnectorContext } from "../../context/ConnectorContext";
import logo from "../../../images/AgentIQ-logo.svg";

type NavItem = {
  to: string;
  label: string;
  runScoped: boolean;
  sfOnly?: boolean;
};

const items = [
  { to: '/integration-hub', label: 'Integration Hub', runScoped: false },
  { to: '/source-intake', label: 'Source Intake', runScoped: false },

  //  run-scoped screens
  { to: '/discovery-run', label: 'Discovery Run', runScoped: true },
  { to: '/partial-results', label: 'Evidence Collection', runScoped: true },
{ to: '/analyst-review', label: 'Analyst Review', runScoped: true },
  { to: '/opportunity-map', label: 'Opportunity Map', runScoped: true },
  { to: '/agentforce-blueprint', label: 'Agentforce Blueprint', runScoped: true, sfOnly: true },
  { to: '/executive-report', label: 'Executive Report', runScoped: true },
] satisfies NavItem[];

export default function TopNav() {
  const loc = useLocation();
  const { runId } = useRunContext();
  const { all: connectors } = useConnectorContext();
  const salesforceConnected = connectors.some(
    (c) => c.id === 'salesforce' && c.status === 'connected',
  );

  return (
    <div className="sticky top-0 z-40 h-[58px] w-full border-b border-border bg-bgheader shadow-[0_2px_8px_rgba(0,0,0,0.15)] backdrop-blur">
      <div className="flex h-full w-full items-center gap-2 px-2">

        {/* Brand */}
        <div className="flex shrink-0 items-center pl-4">
          <img src={logo} alt="AgentIQ Logo" className="h-[32px] w-auto" />
        </div>

        {/* Nav items */}
        <div className="flex flex-1 items-center justify-end gap-1 overflow-x-auto px-2" style={{ scrollbarWidth: 'none' }}>
          {items.map((i) => {
            const isActive = loc.pathname === i.to;

            // Preserve runId only for run-scoped pages
            const to = i.runScoped && runId
              ? `${i.to}?runId=${runId}`
              : i.to;

            return (
              <React.Fragment key={i.to}>
                <Link
                  to={to}
                  className={`shrink-0 whitespace-nowrap font-medium transition-colors ${
                    isActive
                      ? 'border-navborder border-t-2 text-textwhite bg-gradient-to-b from-activenav '
                      : 'text-textwhite/70 hover:bg-navhover hover:text-textwhite'
                  }`}
                  style={{ fontSize: '11.5px', padding: '4px 12px', borderRadius: '100px' }}
                >
                  {i.label}
                  {i.sfOnly && !salesforceConnected && (
                    <Zap
                      size={10}
                      className="ml-1 inline-block shrink-0 text-amber-400"
                      aria-label="Requires Salesforce"
                    />
                  )}
                </Link>
              </React.Fragment>
            );
          })}
        </div>

        <div className="flex shrink-0 items-center pr-2">
          <button
            type="button"
            title="Profile"
            aria-label="Profile"
            className="flex h-7 w-7 items-center justify-center rounded-full text-textwhite/75 transition-colors hover:bg-navhover hover:text-textwhite"
          >
            <User className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
