/**
 * CS-2 / AT-329 (T7) — End-to-end OAuth Connect smoke test.
 *
 * Drives the wired flow through the REAL components (ConnectorProvider,
 * ToastProvider, IntegrationHubPage, OAuthCallbackPage, ConnectorTile) with only
 * the service layer (network boundary) and the provider hop mocked — a real
 * Salesforce login cannot run in CI, so the provider's redirect back to
 * /oauth/callback is simulated. The live leg is covered by the manual procedure
 * documented in the PR.
 *
 *   T7-AC1 — clicking Connect initiates OAuth (auth-url → browser redirect)
 *   T7-AC2 — provider returns to /oauth/callback
 *   T7-AC3 — connector data refreshed + redirect back to Integration Hub
 *   T7-AC4 — Integration Hub shows the appropriate success/error notification
 *   T7-AC5 — connector tile shows "Connected" after success
 *   T7-AC6 — the flow runs without frontend errors (no console.error)
 *
 * Run:
 *   npx vitest run src/__tests__/CS2_OAuthSmokeE2E.test.tsx
 */
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import IntegrationHubPage from "../pages/IntegrationHubPage";
import OAuthCallbackPage from "../pages/OAuthCallbackPage";
import { ConnectorProvider } from "../context/ConnectorContext";
import { ToastProvider } from "../components/common/Toast";
import {
  fetchConnectors,
  connectConnectorApi,
  configureSyncApi,
  fetchTokenStatus,
} from "../services/staticApi";

vi.mock("../services/staticApi", () => ({
  fetchConnectors: vi.fn(),
  connectConnectorApi: vi.fn(),
  configureSyncApi: vi.fn(),
  fetchTokenStatus: vi.fn(),
}));

// PageShell renders TopNav (needs RunProvider) — page chrome unrelated to the
// OAuth flow. Stub it to the title + children so the flow components (tiles,
// toast, context, callback) stay real while avoiding unrelated providers.
vi.mock("../components/common/PageShell", () => ({
  default: ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div>
      <h1>{title}</h1>
      {children}
    </div>
  ),
}));

// RightPanel is the supplementary right-hand chrome (pulls in SourceConfigPanel /
// SourceIntakeContext). Not part of the Connect flow — stub it so the test stays
// focused on the connector tiles, toast, and callback wiring.
vi.mock("../components/integrations/RightPanel", () => ({ default: () => <div /> }));

const mockFetchConnectors = vi.mocked(fetchConnectors);
const mockConnectApi = vi.mocked(connectConnectorApi);
const mockTokenStatus = vi.mocked(fetchTokenStatus);

function salesforce(status: "disconnected" | "connected") {
  return {
    id: "salesforce",
    name: "Salesforce",
    category: "CRM",
    tier: "recommended",
    recommendedRank: 1,
    status,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Accounts", "Cases"],
    signalStrength: 94,
  };
}

function renderApp(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ToastProvider>
        <ConnectorProvider>
          <Routes>
            <Route path="/integration-hub" element={<IntegrationHubPage />} />
            <Route path="/oauth/callback" element={<OAuthCallbackPage />} />
          </Routes>
        </ConnectorProvider>
      </ToastProvider>
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockConnectApi.mockResolvedValue(undefined as any);
  (configureSyncApi as any).mockResolvedValue(undefined);
  mockTokenStatus.mockResolvedValue({ status: "valid" } as any);
});

afterEach(() => vi.restoreAllMocks());

describe("CS-2 OAuth Connect — end-to-end smoke (AT-329 T7)", () => {
  it("Connect on the Salesforce tile initiates the OAuth flow (T7-AC1)", async () => {
    mockFetchConnectors.mockResolvedValue([salesforce("disconnected")] as any);

    renderApp("/integration-hub");

    const tile = (await screen.findByText("Salesforce")).closest(".connector-card");
    expect(tile).not.toBeNull();
    const connectBtn = within(tile as HTMLElement).getByRole("button", { name: "Connect" });
    fireEvent.click(connectBtn);

    // The tile triggers the real OAuth initiation (connectConnectorApi → auth-url
    // → window.location redirect, unit-tested in connectConnectorApi.test.ts).
    await waitFor(() => expect(mockConnectApi).toHaveBeenCalledWith("salesforce"));
  });

  it("provider return → refetch → Integration Hub → success toast → Connected tile (T7-AC2..AC6)", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    // Token is now stored, so the connector list reports connected (T7-AC3/AC5).
    mockFetchConnectors.mockResolvedValue([salesforce("connected")] as any);

    // T7-AC2: the browser lands on /oauth/callback after the provider round-trip.
    renderApp("/oauth/callback?status=success&connected=salesforce");

    // T7-AC4: success notification naming the connector.
    expect(
      await screen.findByText("Salesforce connected successfully")
    ).toBeInTheDocument();

    // T7-AC3: landed back on Integration Hub and connector data was refreshed
    // (provider mount fetch + OAuthCallbackPage refetch ⇒ ≥2 calls).
    expect(await screen.findByText("Integration Hub")).toBeInTheDocument();
    expect(mockFetchConnectors.mock.calls.length).toBeGreaterThanOrEqual(2);

    // T7-AC5: the Salesforce tile shows the Connected badge. ("Salesforce" also
    // appears in the group's connected-tools summary, so pick the tile card.)
    const names = await screen.findAllByText("Salesforce");
    const tile = names.map(n => n.closest(".connector-card")).find(Boolean) as HTMLElement;
    expect(tile).toBeTruthy();
    expect(within(tile).getByText("Connected")).toBeInTheDocument();

    // T7-AC6: no frontend errors during the flow.
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("provider failure → error toast with the OAuth error code (T7-AC4 error path)", async () => {
    mockFetchConnectors.mockResolvedValue([salesforce("disconnected")] as any);

    renderApp("/oauth/callback?status=error&code=access_denied");

    expect(
      await screen.findByText("Connection failed: access_denied")
    ).toBeInTheDocument();
    // Returned to Integration Hub.
    expect(await screen.findByText("Integration Hub")).toBeInTheDocument();
  });
});
