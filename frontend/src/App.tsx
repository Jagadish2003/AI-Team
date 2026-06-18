import { Routes, Route, Navigate } from "react-router-dom";
import { ConnectorProvider } from "./context/ConnectorContext";
import { SourceIntakeProvider } from "./context/SourceIntakeContext";
import { RunProvider } from "./context/RunContext";
import { DiscoveryRunProvider } from "./context/DiscoveryRunContext";
import { PartialResultsProvider } from "./context/PartialResultsContext";
import { NormalizationProvider } from "./context/NormalizationContext";
import { ToastProvider } from "./components/common/Toast";
import { AnalystReviewProvider } from "./context/AnalystReviewContext";
import { EvidenceProvider } from "./context/EvidenceContext";
import { ThemeProvider } from "./context/ThemeContext";
import { AuthProvider } from "./context/AuthContext";
import AuthGuard from "./components/auth/AuthGuard";

import LoginPage from "./pages/LoginPage";
import RegisterPage from "./pages/RegisterPage";
import AcceptInvitePage from "./pages/AcceptInvitePage";
import OAuthCallbackPage from "./pages/OAuthCallbackPage";

import IntegrationHubPage from "./pages/IntegrationHubPage";
import DiscoveryRunPage from "./pages/DiscoveryRunPage";
import OpportunityReviewPage from "./pages/OpportunityReviewPage";
import SourceIntelligencePage from "./pages/SourceIntelligencePage";
import PilotRoadmapPage from "./pages/PilotRoadmapPage";
import BlueprintPage from "./pages/BlueprintPage";
import ExecutiveReportPage from "./pages/ExecutiveReportPage";
import StackBuilderPage from "./pages/StackBuilderPage";
import SettingsPage from "./pages/SettingsPage";

export default function App() {
  return (
    <ThemeProvider>
      <ToastProvider>
        {/*
         * AUTH-1 / AT-238: AuthProvider must wrap all routes so both AuthGuard
         * and public auth pages can access the shared AuthContext.
         */}
        <AuthProvider>
          <ConnectorProvider>
            <RunProvider>
              <SourceIntakeProvider>
                <DiscoveryRunProvider>
                  <PartialResultsProvider>
                    <NormalizationProvider>
                      <AnalystReviewProvider>
                        <EvidenceProvider>
                          <Routes>
                            {/*
                             * ── Public routes ────────────────────────────────
                             * /login, /register, /accept-invite are accessible
                             * without a session.  Page components are delivered
                             * by AT-239 (LoginPage, RegisterPage, AcceptInvitePage).
                             * Route entries are pre-wired here so AuthGuard's
                             * redirect target exists as soon as AT-238 lands.
                             */}
                            <Route path="/login" element={<LoginPage />} />
                            <Route path="/register" element={<RegisterPage />} />
                            <Route path="/accept-invite" element={<AcceptInvitePage />} />

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
                                element={<PilotRoadmapPage />}
                              />
                              <Route
                                path="/agent-blueprint"
                                element={<BlueprintPage />}
                              />
                              <Route
                                path="/executive-report"
                                element={<ExecutiveReportPage />}
                              />
                              <Route path="/settings" element={<SettingsPage />} />
                              <Route
                                path="*"
                                element={<Navigate to="/integration-hub" replace />}
                              />
                            </Route>
                          </Routes>
                        </EvidenceProvider>
                      </AnalystReviewProvider>
                    </NormalizationProvider>
                  </PartialResultsProvider>
                </DiscoveryRunProvider>
              </SourceIntakeProvider>
            </RunProvider>
          </ConnectorProvider>
        </AuthProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
