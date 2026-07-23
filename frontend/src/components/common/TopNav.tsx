import React, { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { KeyRound, LogOut, Menu, Moon, Settings, Sparkles, Sun, User, Zap } from "lucide-react";
import { useRunContext } from "../../context/RunContext";
import { useConnectorContext } from "../../context/ConnectorContext";
import { useTheme } from "../../context/ThemeContext";
import { useAuthOptional } from "../../context/AuthContext";
import { useOnboardingOptional } from "../../context/OnboardingContext";
import { useOrgName } from "../../context/LicenseContext";
import { profileNameFromEmail } from "../../utils/profileName";
import { isViewerRole } from "../../utils/roles";
import {
  getBlueprintLabel,
  isSalesforceConnected,
} from "../../utils/blueprintNaming";

type NavItem = {
  to: string;
  label: string;
  runScoped: boolean;
  sfOnly?: boolean;
  /** Hidden from viewers — the destination is an analyst+ write workflow, so a
   * viewer has nothing actionable there. Filtered out in the nav render below. */
  analystOnly?: boolean;
};

const items = [
  { to: "/integration-hub", label: "Integration Hub", runScoped: false },
  // T41-8: Source Intake removed from nav. Route /source-intake redirects to
  // /integration-hub for backward compatibility. Configuration merged into
  // Integration Hub right panel.
  { to: "/stack-builder", label: "Stack Builder", runScoped: false, analystOnly: true },
  { to: "/run-health", label: "Run Health", runScoped: false, analystOnly: true },
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
  {
    to: "/agentforce-blueprint",
    label: "Agent Blueprint",
    runScoped: true,
    sfOnly: true,
  },
  { to: "/executive-report", label: "Executive Report", runScoped: true },
] satisfies NavItem[];

export default function TopNav() {
  const loc = useLocation();
  const navigate = useNavigate();
  const [menuOpen, setMenuOpen] = useState(false);
  const [profileOpen, setProfileOpen] = useState(false);
  const profileMenuRef = useRef<HTMLDivElement | null>(null);
  const { runId } = useRunContext();
  const { all: connectors } = useConnectorContext();
  const { theme, setTheme } = useTheme();
  // useAuthOptional (not useAuth): TopNav is also rendered by page-level tests
  // that don't mount an AuthProvider. In the real app it's always inside one.
  const auth = useAuthOptional();
  // Optional too: the onboarding provider only exists inside AuthGuard, so the
  // "Replay product tour" entry appears in the real app but is silently absent
  // in isolated tests that render a page without it.
  const onboarding = useOnboardingOptional();

  // Profile tooltip uses the user's email local part, e.g.
  // "srivani@dwp.com" -> "Srivani's Profile".
  const profileName = profileNameFromEmail(auth?.user?.email);
  const profileTitle = profileName ? `${profileName}'s Profile` : "Profile";

  // R17-D4 Addendum A §2 / T13 — organisation display name resolved once from the
  // license (T12), shown as the workspace label in the header for every role. It
  // updates on key paste with no restart and shows a neutral default before a key
  // is installed (AC15/AC16).
  const orgName = useOrgName();

  async function handleLogout() {
    setProfileOpen(false);
    try {
      await auth?.logout();
    } finally {
      navigate("/login", { replace: true });
    }
  }

  const salesforceConnected = isSalesforceConnected(connectors);
  const blueprintLabel = getBlueprintLabel(salesforceConnected);

  // Viewers get a read-only experience: analyst-only destinations (e.g. Stack
  // Builder) are removed from the nav entirely. Only an explicit "viewer" role
  // hides them — analyst/owner (and the claim-less dev token) keep the full nav,
  // and the backend still enforces the boundary regardless of what the nav shows.
  const isViewer = isViewerRole(auth?.user?.role);
  const visibleItems = items.filter((i) => !(i.analystOnly && isViewer));

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
      <div className="flex h-full w-full items-center gap-2 px-4 2xl:gap-3">
        {/* Logo + workspace label. This group is flex-1 min-w-0: it absorbs the
            row's slack (pushing the nav to the right, preserving the original
            brand-left / nav-right layout) AND, when space is tight, it is the
            element that shrinks — so a long org name ellipsizes instead of
            stealing width the nav needs. The logo itself never shrinks. */}
        <div className="flex min-w-0 flex-1 items-center">
          <img
            src={theme === "dark" ? "/Logo-Dark.svg" : "/Logo-Light.svg"}
            alt="AgentIQ Logo"
            className="h-[43px] w-auto shrink-0"
          />
          {/* Workspace label — the organisation name resolved from the license
              (R17-D4 Addendum A §2 / T13). min-w-0 + truncate lets an arbitrarily
              long org name ellipsize within the flex-1 group (full name on hover
              via title). No max-w: the group's width bounds it, so on wide
              viewports the whole name shows and on narrow ones it truncates. */}
          <span
            data-testid="org-name-label"
            title={orgName}
            className="ml-3 hidden min-w-0 truncate border-l border-border pl-3 text-sm font-semibold text-navtext md:inline-block"
          >
            {orgName}
          </span>
        </div>

        {/* Primary nav. NOT flex-1: it keeps its natural content width so all
            items are always fully visible — the flex-1 brand group above yields
            space instead. min-w-0 + overflow-x-auto is a last-resort scroll for
            viewports too narrow to fit every item (default scroll position keeps
            the first item, Integration Hub, visible). */}
        <nav
          aria-label="Primary"
          className="hidden min-w-0 shrink items-center gap-0.5 overflow-x-auto px-1 lg:flex xl:gap-1"
          style={{ scrollbarWidth: "none" }}
        >
          {visibleItems.map((i) => {
            const isActive = loc.pathname === i.to;
            const to = i.runScoped && runId ? `${i.to}?runId=${runId}` : i.to;
            const label = i.sfOnly ? blueprintLabel : i.label;

            return (
              <Link
                key={i.to}
                to={to}
                aria-current={isActive ? "page" : undefined}
                className={`shrink-0 whitespace-nowrap rounded-full border-t-2 px-2.5 py-1.5 text-[13px] font-medium leading-[18px] transition-colors focus:outline-none focus:ring-2 focus:ring-accent/50 xl:px-3 2xl:text-[14px] ${
                  isActive
                    ? "border-navborder bg-gradient-to-b from-activenav text-navtext"
                    : "border-transparent text-navtext/70 hover:bg-navhover hover:text-navtext"
                }`}
              >
                {label}
                {i.sfOnly && !salesforceConnected && (
                  <Zap
                    size={13}
                    className="ml-1 inline-block shrink-0 text-amber-400"
                    aria-label="Requires Salesforce"
                  />
                )}
              </Link>
            );
          })}
        </nav>

        <div className="flex shrink-0 items-center gap-3">
          <button
            type="button"
            title="Menu"
            aria-label="Open navigation menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
            className="flex h-7 w-7 items-center justify-center rounded-full border border-border text-navtext/75 transition-colors hover:bg-navhover hover:text-navtext focus:outline-none focus:ring-2 focus:ring-accent/50 lg:hidden"
          >
            <Menu className="h-5 w-5" />
          </button>

          <div ref={profileMenuRef} className="group relative">
            <button
              type="button"
              aria-haspopup="menu"
              aria-expanded={profileOpen}
              aria-label={profileTitle}
              onClick={() => setProfileOpen((open) => !open)}
              className="flex h-7 w-7 items-center justify-center rounded-full border border-border text-navtext/75 transition-colors hover:bg-navhover hover:text-navtext focus:outline-none focus:ring-2 focus:ring-accent/50"
            >
              <User className="h-5 w-5" />
            </button>

            {/*
             * Themed profile tooltip (replaces the unstyleable native title).
             * Matches the Settings dropdown surface and stays hidden while the
             * dropdown is open so the two don't overlap.
             */}
            {!profileOpen && (
              <span
                role="tooltip"
                className="pointer-events-none absolute right-0 top-full z-50 mt-1.5 whitespace-nowrap rounded-[4px] border border-border bg-panel px-3 py-1.5 text-xs font-medium text-text opacity-0 shadow-xl transition-opacity duration-150 group-hover:opacity-100"
              >
                {profileTitle}
              </span>
            )}

            {profileOpen && (
              <div
                className="absolute right-0 top-11 w-64 rounded-lg border border-border bg-panel p-1 text-text shadow-xl"
                role="menu"
              >
                <button
                  type="button"
                  onClick={() => {
                    setProfileOpen(false);
                    navigate("/settings");
                  }}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text transition-colors hover:bg-navhover focus:outline-none focus:ring-2 focus:ring-accent/50"
                  role="menuitem"
                >
                  <Settings className="h-4 w-4" />
                  Settings
                </button>

                {/* Reopen the first-login product tour on demand. Only shown
                    when the onboarding provider is present (real app). */}
                {onboarding && (
                  <button
                    type="button"
                    onClick={() => {
                      setProfileOpen(false);
                      onboarding.replay();
                    }}
                    className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text transition-colors hover:bg-navhover focus:outline-none focus:ring-2 focus:ring-accent/50"
                    role="menuitem"
                  >
                    <Sparkles className="h-4 w-4" />
                    Replay product tour
                  </button>
                )}

                {/* LIC-1 / T8 — Owner-only admin License page. Analyst/Viewer
                    never see this entry; the route + data endpoints are also
                    Owner-gated, so this just makes the page discoverable. */}
                {auth?.user?.role === "owner" && (
                  <button
                    type="button"
                    onClick={() => {
                      setProfileOpen(false);
                      navigate("/license");
                    }}
                    className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text transition-colors hover:bg-navhover focus:outline-none focus:ring-2 focus:ring-accent/50"
                    role="menuitem"
                  >
                    <KeyRound className="h-4 w-4" />
                    License
                  </button>
                )}

                <div
                  className="flex w-full items-center gap-2 rounded-md px-3 py-1 text-sm text-text transition-colors hover:bg-navhover"
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

                <button
                  type="button"
                  onClick={handleLogout}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-text transition-colors hover:bg-navhover focus:outline-none focus:ring-2 focus:ring-accent/50"
                  role="menuitem"
                >
                  <LogOut className="h-4 w-4" />
                  Logout
                </button>
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
            {visibleItems.map((i) => {
              const isActive = loc.pathname === i.to;
              const to = i.runScoped && runId ? `${i.to}?runId=${runId}` : i.to;
              const label = i.sfOnly ? blueprintLabel : i.label;

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
                  <span>{label}</span>
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
