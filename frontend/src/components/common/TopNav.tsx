import React, { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Menu, User, Zap } from "lucide-react";
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
  { to: "/integration-hub", label: "Integration Hub", runScoped: false },
  // T41-8: Source Intake removed from nav. Route /source-intake redirects to
  // /integration-hub for backward compatibility. Configuration merged into
  // Integration Hub right panel.

  //  run-scoped screens
  { to: "/discovery-run", label: "Discovery Run", runScoped: true },
  // { to: "/partial-results", label: "Evidence Collection", runScoped: true }, // Hidden — Sprint 5.1
  {
    to: "/source-intelligence",
    label: "Source Intelligence",
    runScoped: true,
    sfOnly: false,
  },
  {
    to: "/opportunity-review",
    label: "Opportunity Review",
    runScoped: true,
    sfOnly: false,
  },
  { to: "/pilot-roadmap", label: "Agent Roadmap", runScoped: true },
  {
    to: "/agentforce-blueprint",
    label: "Agentforce Blueprint",
    runScoped: true,
    sfOnly: true,
  },
  { to: "/executive-report", label: "Executive Report", runScoped: true },
] satisfies NavItem[];

export default function TopNav() {
  const loc = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const { runId } = useRunContext();
  const { all: connectors } = useConnectorContext();
  const salesforceConnected = connectors.some(
    (c) => c.id === "salesforce" && c.status === "connected",
  );

  useEffect(() => {
    setMenuOpen(false);
  }, [loc.pathname]);

  return (
    <div className="sticky top-0 z-40 h-[70px] w-full border-b border-border bg-bgheader shadow-[0_2px_8px_rgba(0,0,0,0.15)] backdrop-blur">
      <div className="flex h-full w-full items-center gap-4 px-5">
        {/* Brand */}
        <div className="flex shrink-0 items-center">
          <img src={logo} alt="AgentIQ Logo" className="h-[43px] w-auto" />
        </div>

        {/* Nav items */}
        <nav
          aria-label="Primary"
          className="hidden flex-1 items-center justify-end gap-1.5 overflow-x-auto px-2 lg:flex"
          style={{ scrollbarWidth: "none" }}
        >
          {items.map((i) => {
            const isActive = loc.pathname === i.to;

            // Preserve runId only for run-scoped pages
            const to = i.runScoped && runId ? `${i.to}?runId=${runId}` : i.to;

            return (
              <React.Fragment key={i.to}>
                <Link
                  to={to}
                  aria-current={isActive ? "page" : undefined}
                  className={`shrink-0 whitespace-nowrap rounded-full px-3 pb-1.5 pt-1 font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 ${
                    isActive
                      ? "border-t-2 border-navborder bg-gradient-to-b from-activenav text-textwhite"
                      : "text-textwhite/70 hover:bg-navhover hover:text-textwhite"
                  }`}
                  style={{
                    fontSize: "13px",
                    lineHeight: "18px",
                  }}
                >
                  {i.label}
                  {i.sfOnly && !salesforceConnected && (
                    <Zap
                      size={12}
                      className="ml-1 inline-block shrink-0 text-amber-400"
                      aria-label="Requires Salesforce"
                    />
                  )}
                </Link>
              </React.Fragment>
            );
          })}
        </nav>

        <div className="ml-auto flex shrink-0 items-center gap-2 lg:ml-0">
          <button
            type="button"
            title="Menu"
            aria-label="Open navigation menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-buttonbg text-textwhite/80 transition-colors hover:bg-navhover hover:text-textwhite focus:outline-none focus:ring-2 focus:ring-accent/50 lg:hidden"
          >
            <Menu className="h-5 w-5" />
          </button>

          <button
            type="button"
            title="Profile"
            aria-label="Profile"
            className="flex h-9 w-9 items-center justify-center rounded-full text-textwhite/75 transition-colors hover:bg-navhover hover:text-textwhite"
          >
            <User className="h-5 w-5" />
          </button>
        </div>
      </div>

      {menuOpen && (
        <nav
          aria-label="Mobile navigation"
          className="absolute left-0 right-0 top-[70px] border-b border-border bg-bgheader/95 px-4 py-3 shadow-lg backdrop-blur lg:hidden"
        >
          <div className="grid gap-1">
            {items.map((i) => {
              const isActive = loc.pathname === i.to;
              const to = i.runScoped && runId ? `${i.to}?runId=${runId}` : i.to;

              return (
                <Link
                  key={i.to}
                  to={to}
                  aria-current={isActive ? "page" : undefined}
                  onClick={() => setMenuOpen(false)}
                  className={`flex items-center justify-between rounded-md border px-3 py-2 text-sm font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 ${
                    isActive
                      ? "border-accent/40 bg-activenav/80 text-textwhite"
                      : "border-transparent text-textwhite/75 hover:border-border hover:bg-navhover hover:text-textwhite"
                  }`}
                >
                  <span>{i.label}</span>
                  {i.sfOnly && !salesforceConnected && (
                    <Zap
                      size={13}
                      className="shrink-0 text-amber-400"
                      aria-label="Requires Salesforce"
                    />
                  )}
                </Link>
              );
            })}
          </div>
        </nav>
      )}
    </div>
  );
}
