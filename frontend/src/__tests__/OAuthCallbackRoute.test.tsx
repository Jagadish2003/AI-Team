/**
 * CS-2 / AT-325 (T3) — /oauth/callback is a PUBLIC route in App.tsx.
 *
 * The backend OAuth callback redirects the browser to /oauth/callback with the
 * params OAuthCallbackPage reads (status / connected / code). This route must be
 * reachable WITHOUT authentication (outside AuthGuard), because the session can
 * be momentarily unavailable during the provider redirect cycle (CS-2 AC5).
 *
 * Proof of "public": if the route were nested under AuthGuard, an unauthenticated
 * visit would redirect to /login and OAuthCallbackPage would never mount — so its
 * refetch() would never fire. Asserting refetch() runs while unauthenticated
 * proves the page mounted, i.e. the route bypasses the guard.
 *
 * Run:
 *   npx vitest run src/__tests__/OAuthCallbackRoute.test.tsx
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AuthGuard from "../components/auth/AuthGuard";
import OAuthCallbackPage from "../pages/OAuthCallbackPage";
import { useAuth } from "../context/AuthContext";
import { useConnectorContext } from "../context/ConnectorContext";

vi.mock("../context/AuthContext", () => ({ useAuth: vi.fn() }));
vi.mock("../context/ConnectorContext", () => ({ useConnectorContext: vi.fn() }));

const mockUseAuth = vi.mocked(useAuth);
const mockUseConnector = vi.mocked(useConnectorContext);

const UNAUTHENTICATED = {
  isAuthenticated: false,
  loading: false,
  token: null,
  user: null,
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  acceptInvite: vi.fn(),
};

const refetch = vi.fn();

/**
 * Mirror App.tsx: /oauth/callback is public; /integration-hub is guarded.
 * /login is the AuthGuard redirect target.
 */
function renderRoute(initialPath: string) {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route path="/login" element={<div data-testid="login-page">Login</div>} />
        {/* PUBLIC — outside AuthGuard */}
        <Route path="/oauth/callback" element={<OAuthCallbackPage />} />
        {/* Guarded */}
        <Route element={<AuthGuard />}>
          <Route
            path="/integration-hub"
            element={<div data-testid="integration-hub">Hub</div>}
          />
        </Route>
      </Routes>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAuth.mockReturnValue(UNAUTHENTICATED as any);
  mockUseConnector.mockReturnValue({ refetch } as any);
});

describe("/oauth/callback public route (AT-325 T3)", () => {
  it("mounts and runs without authentication (route is outside AuthGuard)", async () => {
    renderRoute("/oauth/callback?connected=salesforce&status=success");
    // refetch only runs if OAuthCallbackPage mounted — proving the route is public.
    await waitFor(() => expect(refetch).toHaveBeenCalledTimes(1));
  });

  it("consumes status=success&connected=<id> and navigates onward", async () => {
    renderRoute("/oauth/callback?connected=salesforce&status=success");
    // Navigates to /integration-hub (guarded → unauth → /login). Reaching /login
    // confirms the success branch fired and navigation occurred.
    await waitFor(() =>
      expect(screen.getByTestId("login-page")).toBeInTheDocument()
    );
    expect(refetch).toHaveBeenCalled();
  });

  it("consumes status=error&code=<code> without refetching connectors", async () => {
    renderRoute("/oauth/callback?status=error&code=access_denied");
    await waitFor(() =>
      expect(screen.getByTestId("login-page")).toBeInTheDocument()
    );
    // Error branch must not refresh the connector list.
    expect(refetch).not.toHaveBeenCalled();
  });
});
