/**
 * R17-D4 Addendum A / T11 (AT-506) — Integration Hub disables connect at the
 * licensed system limit and shows the current used/licensed count.
 *
 * Covers:
 *   - ConnectorTile: at the limit, a NEW (not-connected) system's Connect button
 *     is disabled with the "contact CloudFulcrum" tooltip and never fires
 *     onPrimary (AC10); an already-connected system stays actionable
 *     (forward-only, AC12); no block when under the limit.
 *   - IntegrationHubPage: renders the systems-used vs systems-licensed count and
 *     the at-limit notice from GET /api/license/limits (AC14 / AC10).
 *
 * Run:
 *   npx vitest run src/__tests__/IntegrationHubLicenseLimit.test.tsx
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// ConnectorTile fetches token-status only for connected+enabled tiles; mock the
// boundary so tiles render without a real network call.
vi.mock("../services/staticApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/staticApi")>();
  return { ...actual, fetchTokenStatus: vi.fn().mockResolvedValue({ status: "connected" }) };
});

// Page-level mocks — exercise the banner wiring without the real context/network.
vi.mock("../context/ConnectorContext", () => ({ useConnectorContext: vi.fn() }));
vi.mock("../components/common/Toast", () => ({ useToast: vi.fn() }));
vi.mock("../api/licenseApi", () => ({ fetchLicenseLimits: vi.fn() }));
vi.mock("../components/common/PageShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("../components/integrations/ConnectorGroupSection", () => ({ default: () => <div /> }));
vi.mock("../components/integrations/RightPanel", () => ({ default: () => <div /> }));

import ConnectorTile from "../components/integrations/ConnectorTile";
import IntegrationHubPage from "../pages/IntegrationHubPage";
import { useConnectorContext } from "../context/ConnectorContext";
import { useToast } from "../components/common/Toast";
import { fetchLicenseLimits } from "../api/licenseApi";

const LIMIT_MSG = "Your license covers 3 systems. Contact CloudFulcrum to add more.";

function salesforce(over: Record<string, unknown> = {}) {
  return {
    id: "salesforce",
    name: "Salesforce",
    category: "CRM",
    tier: "recommended" as const,
    status: "disconnected" as const,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Cases", "Accounts"],
    signalStrength: 88,
    ...over,
  };
}

// ── ConnectorTile — the connect gate (AC10 / AC12) ──────────────────────────
describe("ConnectorTile license limit (AT-506)", () => {
  it("disables Connect for a NEW system at the limit and never fires onPrimary (AC10)", () => {
    const onPrimary = vi.fn();
    render(
      <ConnectorTile
        connector={salesforce() as any}
        icon={<span>SF</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={onPrimary}
        connectBlocked
        connectBlockMessage={LIMIT_MSG}
      />,
    );
    const btn = screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toBe(LIMIT_MSG);

    fireEvent.click(btn);
    expect(onPrimary).not.toHaveBeenCalled();
  });

  it("leaves an already-connected system actionable at the limit (forward-only, AC12)", async () => {
    const onPrimary = vi.fn();
    render(
      <ConnectorTile
        connector={salesforce({ status: "connected", configured: true }) as any}
        icon={<span>SF</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={onPrimary}
        connectBlocked
        connectBlockMessage={LIMIT_MSG}
      />,
    );
    // Connected + configured tile action is "View data" — never gated by the limit.
    const btn = (await screen.findByRole("button", { name: "View data" })) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    expect(onPrimary).toHaveBeenCalledTimes(1);
  });

  it("enables Connect for a new system when under the limit", () => {
    const onPrimary = vi.fn();
    render(
      <ConnectorTile
        connector={salesforce() as any}
        icon={<span>SF</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={onPrimary}
        connectBlocked={false}
      />,
    );
    const btn = screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    fireEvent.click(btn);
    expect(onPrimary).toHaveBeenCalledTimes(1);
  });
});

// ── IntegrationHubPage — the count + notice display (AC14 / AC10) ────────────
describe("IntegrationHubPage license usage (AT-506)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useToast).mockReturnValue({ push: vi.fn() } as any);
    vi.mocked(useConnectorContext).mockReturnValue({
      recommended: [salesforce()],
      standard: [],
      selectedConnectorId: null,
      selectConnector: vi.fn(),
      connectConnector: vi.fn(),
      configureSync: vi.fn(),
      loading: false,
      error: null,
      refetch: vi.fn(),
    } as any);
  });

  it("shows systems-used vs systems-licensed and the at-limit notice (AC14 / AC10)", async () => {
    vi.mocked(fetchLicenseLimits).mockResolvedValue({
      systemsUsed: 3,
      systemsLicensed: 3,
      unlimited: false,
      canConnectMore: false,
    });

    render(
      <MemoryRouter initialEntries={["/integration-hub"]}>
        <IntegrationHubPage />
      </MemoryRouter>,
    );

    expect((await screen.findByTestId("license-usage-count")).textContent).toBe("3 of 3");
    expect(screen.getByTestId("license-at-limit").textContent).toBe(LIMIT_MSG);
  });

  it("shows the count with headroom and no notice when under the limit (AC14)", async () => {
    vi.mocked(fetchLicenseLimits).mockResolvedValue({
      systemsUsed: 2,
      systemsLicensed: 6,
      unlimited: false,
      canConnectMore: true,
    });

    render(
      <MemoryRouter initialEntries={["/integration-hub"]}>
        <IntegrationHubPage />
      </MemoryRouter>,
    );

    expect((await screen.findByTestId("license-usage-count")).textContent).toBe("2 of 6");
    expect(screen.queryByTestId("license-at-limit")).toBeNull();
  });
});
