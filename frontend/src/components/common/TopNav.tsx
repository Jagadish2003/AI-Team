import React, { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { LogOut, Menu, Moon, Settings, Sun, User, Zap } from "lucide-react";
import { useRunContext } from "../../context/RunContext";
import { useConnectorContext } from "../../context/ConnectorContext";
import { useTheme } from "../../context/ThemeContext";

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
  { to: "/stack-builder", label: "Stack Builder", runScoped: false },
  // Run-scoped screens
  { to: "/discovery-run", label: "Discovery Run", runScoped: true },
  // { to: "/partial-results", label: "Evidence Collection", runScoped: true }, // Hidden - Sprint 5.1
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
    to: "/agent-blueprint",
    label: "Agent Blueprint",
    runScoped: true,
    sfOnly: true,
  },
  { to: "/executive-report", label: "Executive Report", runScoped: true },
] satisfies NavItem[];

export default function TopNav() {
  const loc = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const profileMenuRef = useRef<HTMLDivElement | null>(null);
  const { runId } = useRunContext();
  const { all: connectors } = useConnectorContext();
  const { theme, setTheme } = useTheme();
  const upcomingSprintMessage = "It will be implemented in upcoming sprints";

  const salesforceConnected = connectors.some(
    (c) => c.id === "salesforce" && c.status === "connected",
  );

  useEffect(() => {
    setMenuOpen(false);
    setProfileOpen(false);
  }, [loc.pathname]);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      if (!profileMenuRef.current?.contains(event.target as Node)) {
        setProfileOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

  return (
    <div className="sticky top-0 z-40 h-[70px] w-full border-b border-border bg-bgheader shadow-[0_2px_8px_rgba(0,0,0,0.15)] backdrop-blur">
      <div className="flex h-full w-full items-center gap-4 px-5">
        <div className="flex shrink-0 items-center">
          <img
            src={theme === "dark" ? "/Logo-Dark.svg" : "/Logo-Light.svg"}
            alt="AgentIQ Logo"
            className="h-[43px] w-auto"
          />
        </div>

        <nav
          aria-label="Primary"
          className="hidden flex-1 items-center justify-end gap-1.5 overflow-x-auto px-2 lg:flex"
          style={{ scrollbarWidth: "none" }}
        >
          {items.map((i) => {
            const isActive = loc.pathname === i.to;
            const to = i.runScoped && runId ? `${i.to}?runId=${runId}` : i.to;

            return (
              <Link
                key={i.to}
                to={to}
                aria-current={isActive ? "page" : undefined}
                className={`shrink-0 whitespace-nowrap rounded-full px-3 pb-1.5 pt-1 font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 ${
                  isActive
                    ? "border-t-2 border-navborder bg-gradient-to-b from-activenav text-navtext"
                    : "text-navtext/70 hover:bg-navhover hover:text-navtext"
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
            );
          })}
        </nav>

        <div className="ml-auto flex shrink-0 items-center gap-3 lg:ml-0">
          <button
            type="button"
            title="Menu"
            aria-label="Open navigation menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            className="flex h-9 w-9 items-center justify-center rounded-full border border-border bg-buttonbg text-navtext/80 transition-colors hover:bg-navhover hover:text-navtext focus:outline-none focus:ring-2 focus:ring-accent/50 lg:hidden"
          >
            <Menu className="h-5 w-5" />
          </button>

          <div ref={profileMenuRef} className="relative">
            <button
              type="button"
              title="Profile"
              aria-haspopup="menu"
              aria-expanded={profileOpen}
              aria-label="User profile"
              onClick={() => setProfileOpen((open) => !open)}
              className="flex h-7 w-7 items-center justify-center rounded-full border border-border text-navtext/75 transition-colors hover:bg-navhover hover:text-navtext focus:outline-none focus:ring-2 focus:ring-accent/50"
            >
              <User className="h-5 w-5" />
            </button>

            {profileOpen && (
              <div
                className="absolute right-0 top-11 w-64 rounded-lg border border-border bg-panel p-1 text-text shadow-xl"
                role="menu"
              >
                <div className="group profile-dropdown-item relative">
                  <button
                    type="button"
                    aria-disabled="true"
                    className="flex w-full cursor-not-allowed items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-muted opacity-60"
                    role="menuitem"
                  >
                    <Settings className="h-4 w-4" />
                    Profile settings
                  </button>
                  <div className="profile-menu-tooltip pointer-events-none absolute left-1/2 top-full z-20 mt-1 w-max max-w-[calc(100vw-2rem)] whitespace-nowrap rounded-md border border-border bg-panel px-2 py-1 text-xs text-text shadow-lg">
                    {upcomingSprintMessage}
                  </div>
                </div>

                <div
                  className="flex w-full items-center gap-3 rounded-md px-3 py-1 text-sm text-text transition-colors hover:bg-navhover"
                  role="menuitem"
                >
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center text-muted">
                    {theme === "light" ? (
                      <Sun className="h-4 w-4" />
                    ) : (
                      <Moon className="h-4 w-4" />
                    )}
                  </span>
                  <span className="shrink-0">Theme</span>
                  <div
                    className="ml-auto inline-flex h-7 w-24 items-center rounded-full border border-border bg-bg p-0.5 text-[11px] font-medium text-muted transition-colors hover:border-white focus-within:border-white"
                    role="group"
                    aria-label="Theme preference"
                  >
                    <button
                      type="button"
                      onClick={() => setTheme("light")}
                      className={`flex h-full flex-1 items-center justify-center rounded-full border transition ${
                        theme === "light"
                          ? "border-white bg-panel text-text shadow-sm"
                          : "border-transparent text-muted hover:border-white hover:text-text"
                      }`}
                      aria-pressed={theme === "light"}
                    >
                      Light
                    </button>
                    <button
                      type="button"
                      onClick={() => setTheme("dark")}
                      className={`flex h-full flex-1 items-center justify-center rounded-full border transition ${
                        theme === "dark"
                          ? "border-white bg-panel text-text shadow-sm"
                          : "border-transparent text-muted hover:border-white hover:text-text"
                      }`}
                      aria-pressed={theme === "dark"}
                    >
                      Dark
                    </button>
                  </div>
                </div>

                <div className="group profile-dropdown-item relative">
                  <button
                    type="button"
                    aria-disabled="true"
                    className="flex w-full cursor-not-allowed items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-muted opacity-60"
                    role="menuitem"
                  >
                    <LogOut className="h-4 w-4" />
                    Logout
                  </button>
                  <div className="profile-menu-tooltip pointer-events-none absolute left-1/2 top-full z-20 mt-1 w-max max-w-[calc(100vw-2rem)] whitespace-nowrap rounded-md border border-border bg-panel px-2 py-1 text-xs text-text shadow-lg">
                    {upcomingSprintMessage}
                  </div>
                </div>
              </div>
            )}
          </div>
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
                      ? "border-accent/40 bg-activenav/80 text-navtext"
                      : "border-transparent text-navtext/75 hover:border-border hover:bg-navhover hover:text-navtext"
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
