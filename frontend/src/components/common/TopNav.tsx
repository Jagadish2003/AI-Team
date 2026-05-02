import React from "react";
import { Link, useLocation } from "react-router-dom";
import { User, Zap } from "lucide-react";
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
  { to: "/partial-results", label: "Evidence Collection", runScoped: true },
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
  const { runId } = useRunContext();
  const { all: connectors } = useConnectorContext();
  const salesforceConnected = connectors.some(
    (c) => c.id === "salesforce" && c.status === "connected",
  );

  return (
    <div className="sticky top-0 z-40 h-[70px] w-full border-b border-border bg-bgheader shadow-[0_2px_8px_rgba(0,0,0,0.15)] backdrop-blur">
      <div className="flex h-full w-full items-center gap-4 px-5">
        {/* Brand */}
        <div className="flex shrink-0 items-center">
          <img src={logo} alt="AgentIQ Logo" className="h-[43px] w-auto" />
        </div>

        {/* Nav items */}
        <div
          className="flex flex-1 items-center justify-end gap-1.5 overflow-x-auto px-2"
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
                  className={`shrink-0 whitespace-nowrap font-medium transition-colors ${
                    isActive
                      ? "border-navborder border-t-2 text-textwhite bg-gradient-to-b from-activenav"
                      : "text-textwhite/70 hover:bg-navhover hover:text-textwhite"
                  }`}
                  style={{
                    fontSize: "14px",
                    lineHeight: "18px",
                    padding: "7px 13px",
                    borderRadius: "100px",
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
        </div>

        <div className="flex shrink-0 items-center">
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
    </div>
  );
}
