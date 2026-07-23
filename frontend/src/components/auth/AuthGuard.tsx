/**
 * AUTH-1 / AT-238 — Route protection guard.
 *
 * Behaviour:
 *   - While AuthContext is resolving a /api/auth/me refresh (`loading`):
 *       render a loading panel so the redirect does not fire prematurely.
 *   - When no authenticated session exists (`!isAuthenticated`):
 *       redirect to /login (replace — no back-button loop).
 *   - When authenticated:
 *       render <Outlet /> so nested protected routes render normally.
 *
 * Usage (React Router v6 layout-route pattern):
 *
 *   <Route element={<AuthGuard />}>
 *     <Route path="/integration-hub" element={<IntegrationHubPage />} />
 *     <Route path="/discovery-run"   element={<DiscoveryRunPage />} />
 *     ...
 *   </Route>
 *
 * Public routes (/login, /register, /accept-invite) must be placed OUTSIDE
 * this wrapper so they remain accessible without a session.
 *
 * AC14 — unauthenticated access to any guarded route redirects to /login.
 * AC15 — authenticated users reach protected pages normally.
 */
import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "../../context/AuthContext";
import { LicenseProvider } from "../../context/LicenseContext";
import { OnboardingProvider } from "../../context/OnboardingContext";
import OnboardingModal from "../onboarding/OnboardingModal";
import LicenseBanner from "../common/LicenseBanner";
import LoadingPanel from "../common/LoadingPanel";

export default function AuthGuard() {
  const { isAuthenticated, loading } = useAuth();

  // Still resolving the in-session /api/auth/me refresh — hold rendering
  // until we know whether the session is valid.  This prevents a flash
  // redirect to /login on the frame immediately after a successful login.
  if (loading) {
    return (
      <LoadingPanel
        title="Verifying session…"
        subtitle="Checking your authentication status."
      />
    );
  }

  // No valid session → send to login.  replace=true so the guarded path
  // does not sit in browser history (pressing Back would loop back here).
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  // Authenticated — render the matched child route, wrapped so the global
  // license expiry banner (LIC-1 / T9) shows on every authenticated page from a
  // single shared status fetch. LicenseProvider stays mounted across child
  // route changes (layout-route element), so status is not refetched per page.
  //
  // OnboardingProvider wraps this authenticated subtree so the first-login
  // product tour can layer over the already-rendered dashboard (<Outlet/>) and
  // shared chrome (TopNav "Replay product tour") can reach it. It is a pure
  // overlay: it never navigates, fetches, or touches auth/routing.
  return (
    <OnboardingProvider>
      <LicenseProvider>
        <LicenseBanner />
        <Outlet />
      </LicenseProvider>
      <OnboardingModal />
    </OnboardingProvider>
  );
}
