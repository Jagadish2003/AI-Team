/**
 * IntegrationHubConnectOnce.test.tsx
 *
 * The Integration Hub "Connect" button is one-shot, matching the Stack Builder
 * "Start discovery" button: the first click starts the OAuth round-trip, and
 * while that is in flight the button is disabled and reads "Connecting…".
 * Connecting mints a one-time OAuth state nonce and then redirects the browser,
 * so a second click must never reach the initiation call.
 *
 * Run:
 *   npx vitest run src/__tests__/IntegrationHubConnectOnce.test.tsx
 */
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import IntegrationHubPage from "../pages/IntegrationHubPage";
import ConnectorTile from "../components/integrations/ConnectorTile";
import { ConnectorProvider } from "../context/ConnectorContext";
import { DataCacheProvider } from "../lib/dataCache";
import { ToastProvider } from "../components/common/Toast";
import { connectConnectorApi, configureSyncApi, fetchTokenStatus } from "../services/staticApi";

vi.mock("../services/staticApi", () => ({
  fetchConnectors: vi.fn(),
  connectConnectorApi: vi.fn(),
  configureSyncApi: vi.fn(),
  fetchTokenStatus: vi.fn(),
}));

// PageShell renders TopNav (needs RunProvider) — page chrome unrelated to the
// connect flow. RightPanel pulls in SourceIntakeContext; its own next-best
// Connect button is covered by the tile assertions here.
vi.mock("../components/common/PageShell", () => ({
  default: ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div>
      <h1>{title}</h1>
      {children}
    </div>
  ),
}));
vi.mock("../components/integrations/RightPanel", () => ({ default: () => <div /> }));

const mockConnectApi = vi.mocked(connectConnectorApi);

function salesforce() {
  return {
    id: "salesforce",
    name: "Salesforce",
    category: "CRM",
    tier: "recommended",
    recommendedRank: 1,
    status: "disconnected",
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Accounts", "Cases"],
    signalStrength: 94,
  };
}

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.clearAllMocks();
  (configureSyncApi as any).mockResolvedValue(undefined);
  vi.mocked(fetchTokenStatus).mockResolvedValue({ status: "connected" } as any);

  // ConnectorContext loads the list via a raw fetch('/api/connectors'); other
  // incidental calls get a benign empty-OK response.
  fetchSpy = vi.fn((input: unknown) => {
    const url = typeof input === "string" ? input : (input as { url?: string })?.url ?? "";
    if (url.includes("/api/connectors")) {
      return Promise.resolve({ ok: true, status: 200, json: async () => [salesforce()] });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function renderHub() {
  return render(
    <MemoryRouter initialEntries={["/integration-hub"]}>
      <ToastProvider>
        <DataCacheProvider>
          <ConnectorProvider>
            <Routes>
              <Route path="/integration-hub" element={<IntegrationHubPage />} />
            </Routes>
          </ConnectorProvider>
        </DataCacheProvider>
      </ToastProvider>
    </MemoryRouter>,
  );
}

async function salesforceTile() {
  const names = await screen.findAllByText("Salesforce");
  return names.map((n) => n.closest(".connector-card")).find(Boolean) as HTMLElement;
}

describe("Integration Hub Connect — one click only", () => {
  it("disables the button and shows 'Connecting…' while the flow is in flight", async () => {
    // Never resolves: the real flow leaves the page on success, so the button
    // must stay busy for as long as the call is outstanding.
    mockConnectApi.mockImplementation(() => new Promise<void>(() => {}));

    renderHub();
    const tile = await salesforceTile();
    fireEvent.click(within(tile).getByRole("button", { name: "Connect" }));

    const busy = await within(tile).findByRole("button", { name: /connecting/i });
    expect(busy).toBeDisabled();
    expect(within(tile).queryByRole("button", { name: "Connect" })).not.toBeInTheDocument();
  });

  it("initiates the OAuth flow once even when clicked repeatedly", async () => {
    mockConnectApi.mockImplementation(() => new Promise<void>(() => {}));

    renderHub();
    const tile = await salesforceTile();
    const btn = within(tile).getByRole("button", { name: "Connect" });

    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);

    await waitFor(() => expect(mockConnectApi).toHaveBeenCalledWith("salesforce"));
    expect(mockConnectApi).toHaveBeenCalledTimes(1);
  });

  it("does not leave a busy button behind when the flow could not be started", async () => {
    mockConnectApi.mockRejectedValue(new Error("auth-url failed"));

    renderHub();
    const tile = await salesforceTile();
    fireEvent.click(within(tile).getByRole("button", { name: "Connect" }));

    // A failed connect surfaces the hub's error state (existing behaviour), and
    // the in-flight flag is released — so no "Connecting…" button is left on
    // screen once the tiles render again.
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /connecting/i })).not.toBeInTheDocument();
  });
});

describe("ConnectorTile — connecting prop", () => {
  it("renders a disabled 'Connecting…' action and swallows further clicks", () => {
    const onPrimary = vi.fn();
    render(
      <DataCacheProvider>
        <ConnectorTile
          connector={salesforce() as any}
          icon={<span>ic</span>}
          selected={false}
          onSelect={vi.fn()}
          onPrimary={onPrimary}
          connecting
        />
      </DataCacheProvider>,
    );

    const btn = screen.getByRole("button", { name: /connecting/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onPrimary).not.toHaveBeenCalled();
  });

  it("renders the normal Connect action when not connecting", () => {
    render(
      <DataCacheProvider>
        <ConnectorTile
          connector={salesforce() as any}
          icon={<span>ic</span>}
          selected={false}
          onSelect={vi.fn()}
          onPrimary={vi.fn()}
        />
      </DataCacheProvider>,
    );

    expect(screen.getByRole("button", { name: "Connect" })).toBeEnabled();
  });
});
