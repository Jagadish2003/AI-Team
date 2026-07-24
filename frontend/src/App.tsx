import { Fragment, Suspense, lazy } from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { ConnectorProvider } from "./context/ConnectorContext";
import { NetworkProfileProvider } from "./context/NetworkProfileContext";
import { SourceIntakeProvider } from "./context/SourceIntakeContext";
import { RunProvider } from "./context/RunContext";
import { DiscoveryRunProvider } from "./context/DiscoveryRunContext";
import { PartialResultsProvider } from "./context/PartialResultsContext";
import { NormalizationProvider } from "./context/NormalizationContext";
import { ToastProvider } from "./components/common/Toast";
import { AnalystReviewProvider } from "./context/AnalystReviewContext";
import { EvidenceProvider } from "./context/EvidenceContext";
import { ThemeProvider } from "./context/ThemeContext";
import { AuthProvider, useAuth } from "./context/AuthContext";
import { DataCacheProvider } from "./lib/dataCache";
import AuthGuard from "./components/auth/AuthGuard";
import LoadingPanel from "./components/common/LoadingPanel";
import PrefetchWorkspaceData from "./components/common/PrefetchWorkspaceData";

// Route components are lazy-loaded so opening any page ships only that page's
// chunk (plus shared vendor), not the JS for all pages up front. The Suspense
// boundary around <Routes> shows a lightweight fallback while a chunk loads.
const LoginPage = lazy(() => import("./pages/LoginPage"));
const RegisterPage = lazy(() => import("./pages/RegisterPage"));
const PendingApprovalPage = lazy(() => import("./pages/PendingApprovalPage"));
const AcceptInvitePage = lazy(() => import("./pages/AcceptInvitePage"));
const OAuthCallbackPage = lazy(() => import("./pages/OAuthCallbackPage"));
const ForgotPasswordPage = lazy(() => import("./pages/ForgotPasswordPage"));
const ResetPasswordPage = lazy(() => import("./pages/ResetPasswordPage"));

const IntegrationHubPage = lazy(() => import("./pages/IntegrationHubPage"));
const DiscoveryRunPage = lazy(() => import("./pages/DiscoveryRunPage"));
const OpportunityReviewPage = lazy(() => import("./pages/OpportunityReviewPage"));
const SourceIntelligencePage = lazy(() => import("./pages/SourceIntelligencePage"));
const BlueprintPage = lazy(() => import("./pages/BlueprintPage"));
const ExecutiveReportPage = lazy(() => import("./pages/ExecutiveReportPage"));
const StackBuilderPage = lazy(() => import("./pages/StackBuilderPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const LicensePage = lazy(() => import("./pages/LicensePage"));
const RunHealthDashboardPage = lazy(() => import("./pages/RunHealthDashboardPage"));

function PilotRoadmapRedirect() {
  const location = useLocation();
  return <Navigate to={`/agentforce-blueprint${location.search}${location.hash}`} replace />;
}

/**
 * Remounts the entire data-provider subtree whenever the authenticated identity
 * changes (login / logout / accept-invite), keyed on the in-session JWT. This
 * gives a clean per-user slate WITHOUT a full document reload: every provider —
 * including the mount-only ones (SourceIntake, Normalization, AnalystReview,
 * PartialResults, Evidence, DiscoveryRun) that do not react to the token —
 * unmounts and refetches under the new user's token, so no previous org's data
 * survives. The key is "anonymous" while logged out. Replaces the post-login
 * hardRedirect() full reload that used to force this remount (see LoginPage /
 * AcceptInvitePage). Must render INSIDE AuthProvider (reads useAuth) and AROUND
 * the data providers; the Router lives above <App/> in main.tsx, so useNavigate
 * and history survive the remount.
 */
function SessionBoundary({ children }: { children: React.ReactNode }) {
  const { token } = useAuth();
  return <Fragment key={token ?? "anonymous"}>{children}</Fragment>;
}

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        {/*
         * AUTH-1 / AT-238: AuthProvider must wrap all routes so both AuthGuard
         * and public auth pages can access the shared AuthContext.
         */}
        <AuthProvider>
          <SessionBoundary>
          {/* Shared data cache at the top of the per-user keyed subtree: its Map
              is discarded on user change (SessionBoundary remount), and it backs
              cross-page reactivity (useResource / invalidate). */}
          <DataCacheProvider>
          {/* Cross-user freshness comes from the cache's focus + interval
              revalidation (see DataCacheProvider); the current user's own actions
              refresh instantly via cache.invalidate at each mutation site. */}
          <NetworkProfileProvider>
          <ConnectorProvider>
            <RunProvider>
              <SourceIntakeProvider>
                <DiscoveryRunProvider>
                  <PartialResultsProvider>
                    <NormalizationProvider>
                      <AnalystReviewProvider>
                        <EvidenceProvider>
                          {/* Warms this user's whole workspace into the shared
                              cache after login, so every page renders from cache
                              with no waiting. Inside the run + analyst-review
                              providers because it warms per-opportunity data. */}
                          <PrefetchWorkspaceData />
                          <Suspense fallback={<LoadingPanel />}>
                          <Routes>
                            {/*
                             * ── Public routes ────────────────────────────────
                             * /login, /register, /accept-invite are accessible
                             * without a session.  Page components are delivered
                             * by AT-239 (LoginPage, RegisterPage, AcceptInvitePage).
                             * Route entries are pre-wired here so AuthGuard's
                             * redirect target exists as soon as AT-238 lands.
                             *
                             * CS-3 adds /forgot-password and /reset-password as
                             * public routes too: a user who cannot sign in must be
                             * able to request and complete a password reset.
                             */}
                            <Route path="/login" element={<LoginPage />} />
                            <Route path="/register" element={<RegisterPage />} />
                            {/* AUTH-2 T6: static post-registration confirmation.
                                Public — the registrant has no session at this
                                point (registration issues no JWT). */}
                            <Route path="/pending-approval" element={<PendingApprovalPage />} />
                            <Route path="/accept-invite" element={<AcceptInvitePage />} />
                            <Route path="/forgot-password" element={<ForgotPasswordPage />} />
                            <Route path="/reset-password" element={<ResetPasswordPage />} />

                            {/*
                             * CS-2 / AT-325: OAuth callback landing page. PUBLIC
                             * (outside AuthGuard) — the backend redirects the
                             * browser here after the provider round-trip, and the
                             * session may be momentarily unavailable during the
                             * redirect cycle. OAuthCallbackPage refetches the
                             * connector list and routes back to Integration Hub.
                             */}
                            <Route
                              path="/oauth/callback"
                              element={<OAuthCallbackPage />}
                            />

                            {/*
                             * ── Protected routes ─────────────────────────────
                             * AuthGuard checks the session before rendering any
                             * child route.  Unauthenticated requests redirect to
                             * /login (AC14).  Authenticated requests render the
                             * matched page normally (AC15).
                             */}
                            <Route element={<AuthGuard />}>
                              <Route
                                path="/"
                                element={<Navigate to="/integration-hub" replace />}
                              />
                              <Route
                                path="/integration-hub"
                                element={<IntegrationHubPage />}
                              />
                              <Route
                                path="/source-intake"
                                // T41-8: Source Intake merged into Integration Hub.
                                // Redirect preserved for backward compatibility.
                                element={<Navigate to="/integration-hub" replace />}
                              />
                              <Route
                                path="/stack-builder"
                                element={<StackBuilderPage />}
                              />
                              <Route
                                path="/run-health"
                                element={<RunHealthDashboardPage />}
                              />
                              <Route
                                path="/discovery-run"
                                element={<DiscoveryRunPage />}
                              />
                              {/* Evidence Collection hidden — Sprint 5.1
                              <Route
                                path="/partial-results"
                                element={<PartialResultsPage />}
                              /> */}
                              <Route
                                path="/normalization"
                                element={
                                  <Navigate to="/source-intelligence" replace />
                                }
                              />
                              <Route
                                path="/source-intelligence"
                                element={<SourceIntelligencePage />}
                              />
                              <Route
                                path="/analyst-review"
                                element={
                                  <Navigate to="/opportunity-review" replace />
                                }
                              />
                              <Route
                                path="/opportunity-map"
                                element={
                                  <Navigate to="/opportunity-review" replace />
                                }
                              />
                              <Route
                                path="/opportunity-review"
                                element={<OpportunityReviewPage />}
                              />
                              <Route
                                path="/pilot-roadmap"
                                // Agent Roadmap now lives at the top of the
                                // connector-aware Blueprint page. Redirect preserved for
                                // old links and Executive Report deep links.
                                element={<PilotRoadmapRedirect />}
                              />
                              <Route
                                path="/agentforce-blueprint"
                                element={<BlueprintPage />}
                              />
                              <Route
                                path="/agent-blueprint"
                                // Legacy Blueprint route; the customer-facing name is
                                // resolved from the connected source system.
                                element={
                                  <Navigate to="/agentforce-blueprint" replace />
                                }
                              />
                              <Route
                                path="/executive-report"
                                element={<ExecutiveReportPage />}
                              />
                              <Route path="/settings" element={<SettingsPage />} />
                              {/* LIC-1 / T8 — Owner-only admin License page. */}
                              <Route path="/license" element={<LicensePage />} />
                              <Route
                                path="*"
                                element={<Navigate to="/integration-hub" replace />}
                              />
                            </Route>
                          </Routes>
                          </Suspense>
                        </EvidenceProvider>
                      </AnalystReviewProvider>
                    </NormalizationProvider>
                  </PartialResultsProvider>
                </DiscoveryRunProvider>
              </SourceIntakeProvider>
            </RunProvider>
          </ConnectorProvider>
          </NetworkProfileProvider>
          </DataCacheProvider>
          </SessionBoundary>
        </AuthProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
