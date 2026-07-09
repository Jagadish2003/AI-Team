/**
 * R18-C0 P4 / AT-566 — Integration Hub per-tile disconnect (with confirm).
 *
 * Covers:
 *   - ConnectorTile: a connected tile renders a Disconnect action that fires
 *     onDisconnect; a not-connected tile has none; a viewer never sees it.
 *   - IntegrationHubPage: clicking Disconnect opens a confirmation dialog, and
 *     confirming calls the context disconnect (clearing the credential) and shows
 *     a success toast. Cancelling makes no call.
 *
 * Run:
 *   npx vitest run src/__tests__/IntegrationHubDisconnect.test.tsx
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// ConnectorTile fetches token-status for connected+enabled tiles; mock the
// boundary so tiles render without a real network call.
vi.mock("../services/staticApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/staticApi")>();
  return { ...actual, fetchTokenStatus: vi.fn().mockResolvedValue({ status: "connected" }) };
});

vi.mock("../context/ConnectorContext", () => ({ useConnectorContext: vi.fn() }));
vi.mock("../components/common/Toast", () => ({ useToast: vi.fn() }));
vi.mock("../api/licenseApi", () => ({ fetchLicenseLimits: vi.fn() }));
vi.mock("../components/common/PageShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
// Keep the RightPanel out of the way — the disconnect flow lives in the tiles.
vi.mock("../components/integrations/RightPanel", () => ({ default: () => <div /> }));

import ConnectorTile from "../components/integrations/ConnectorTile";
import IntegrationHubPage from "../pages/IntegrationHubPage";
import { useConnectorContext } from "../context/ConnectorContext";
import { useToast } from "../components/common/Toast";
import { fetchLicenseLimits } from "../api/licenseApi";

function salesforce(over: Record<string, unknown> = {}) {
  return {
    id: "salesforce",
    name: "Salesforce",
    category: "CRM",
    tier: "recommended" as const,
    status: "connected" as const,
    configured: true,
    metrics: [],
    lastSynced: "just now",
    reads: ["Cases", "Accounts"],
    signalStrength: 88,
    ...over,
  };
}

// ── ConnectorTile — the Disconnect action (AC4) ─────────────────────────────
describe("ConnectorTile disconnect (AT-566)", () => {
  it("renders a Disconnect action on a connected tile and fires onDisconnect", () => {
    const onDisconnect = vi.fn();
    render(
      <ConnectorTile
        connector={salesforce() as any}
        icon={<span>SF</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={vi.fn()}
        onDisconnect={onDisconnect}
      />,
    );
    const btn = screen.getByRole("button", { name: "Disconnect Salesforce" });
    fireEvent.click(btn);
    expect(onDisconnect).toHaveBeenCalledTimes(1);
  });

  it("shows no Disconnect action on a not-connected tile", () => {
    render(
      <ConnectorTile
        connector={salesforce({ status: "disconnected", configured: false }) as any}
        icon={<span>SF</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={vi.fn()}
        onDisconnect={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Disconnect Salesforce" })).toBeNull();
  });
});

// ── ConnectorTile — viewers never see Disconnect (analyst+ write) ────────────
describe("ConnectorTile disconnect — viewer gating (AT-566)", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("hides Disconnect for a viewer role", async () => {
    vi.doMock("../context/AuthContext", () => ({
      useAuthOptional: () => ({ user: { role: "viewer" } }),
    }));
    const { default: TileWithViewer } = await import(
      "../components/integrations/ConnectorTile"
    );
    render(
      <TileWithViewer
        connector={salesforce() as any}
        icon={<span>SF</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={vi.fn()}
        onDisconnect={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Disconnect Salesforce" })).toBeNull();
    vi.doUnmock("../context/AuthContext");
  });
});

// ── IntegrationHubPage — confirm dialog → disconnect (AC4) ───────────────────
describe("IntegrationHubPage disconnect flow (AT-566)", () => {
  const push = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useToast).mockReturnValue({ push } as any);
    vi.mocked(fetchLicenseLimits).mockResolvedValue({
      systemsUsed: 1,
      systemsLicensed: 5,
      unlimited: false,
      canConnectMore: true,
    } as any);
  });

  function mockContext(disconnectConnector: () => Promise<void>) {
    vi.mocked(useConnectorContext).mockReturnValue({
      recommended: [salesforce()],
      standard: [],
      selectedConnectorId: "salesforce",
      selectConnector: vi.fn(),
      connectConnector: vi.fn(),
      configureSync: vi.fn(),
      disconnectConnector,
      loading: false,
      error: null,
      refetch: vi.fn(),
    } as any);
  }

  it("opens a confirmation dialog and disconnects on confirm", async () => {
    const disconnectConnector = vi.fn().mockResolvedValue(undefined);
    mockContext(disconnectConnector);

    render(
      <MemoryRouter initialEntries={["/integration-hub"]}>
        <IntegrationHubPage />
      </MemoryRouter>,
    );

    // Open the confirm dialog from the connected Salesforce tile.
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect Salesforce" }));

    const dialog = await screen.findByRole("dialog", { name: "Disconnect connector" });
    expect(dialog).toBeTruthy();

    // Confirm.
    fireEvent.click(screen.getByRole("button", { name: "Disconnect" }));

    await waitFor(() =>
      expect(disconnectConnector).toHaveBeenCalledWith("salesforce"),
    );
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("Salesforce disconnected.", "success"),
    );
  });

  it("does not disconnect when the dialog is cancelled", async () => {
    const disconnectConnector = vi.fn().mockResolvedValue(undefined);
    mockContext(disconnectConnector);

    render(
      <MemoryRouter initialEntries={["/integration-hub"]}>
        <IntegrationHubPage />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByRole("button", { name: "Disconnect Salesforce" }));
    await screen.findByRole("dialog", { name: "Disconnect connector" });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Disconnect connector" })).toBeNull(),
    );
    expect(disconnectConnector).not.toHaveBeenCalled();
  });
});
