/**
 * CS-2 (AC6/AC7) — ConnectorTile token-status → Token expired / Reconnect.
 *
 * Guards the fix for the token-status contract mismatch: the backend returns
 * connected | needs_refresh | needs_auth | refresh_failed (AT-77 AC14), not the
 * valid | expired | missing values the CS-2 story doc sketched. The tile must
 * map needs_auth / refresh_failed → "Token expired" badge + "Reconnect" button,
 * and treat connected / needs_refresh as usable (no Reconnect prompt).
 *
 * Run:
 *   npx vitest run src/__tests__/ConnectorTileTokenStatus.test.tsx
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConnectorTile from "../components/integrations/ConnectorTile";
import { fetchTokenStatus, TokenStatus } from "../services/staticApi";

vi.mock("../services/staticApi", () => ({
  fetchTokenStatus: vi.fn(),
}));

const mockTokenStatus = vi.mocked(fetchTokenStatus);

function connectedSalesforce() {
  return {
    id: "salesforce",
    name: "Salesforce",
    category: "CRM",
    tier: "recommended" as const,
    recommendedRank: 1,
    status: "connected" as const,
    configured: true,
    metrics: [],
    lastSynced: "2m ago",
    reads: ["Accounts", "Cases"],
    signalStrength: 94,
  };
}

function renderTile(onPrimary = vi.fn()) {
  render(
    <ConnectorTile
      connector={connectedSalesforce() as any}
      icon={<span>SF</span>}
      selected={false}
      onSelect={vi.fn()}
      onPrimary={onPrimary}
    />
  );
  return { onPrimary };
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe("ConnectorTile — token-status mapping (CS-2 AC6/AC7)", () => {
  it.each<TokenStatus>(["needs_auth", "refresh_failed"])(
    "%s → shows 'Token expired' badge and a Reconnect button (AC7)",
    async (status) => {
      mockTokenStatus.mockResolvedValue({ status });
      const { onPrimary } = renderTile();

      expect(await screen.findByText("Token expired")).toBeInTheDocument();
      const reconnect = await screen.findByRole("button", { name: "Reconnect" });

      // Clicking Reconnect triggers the same OAuth flow via onPrimary (AC7).
      fireEvent.click(reconnect);
      expect(onPrimary).toHaveBeenCalledTimes(1);
    }
  );

  it.each<TokenStatus>(["connected", "needs_refresh"])(
    "%s → no Token expired badge / no Reconnect prompt (AC6)",
    async (status) => {
      mockTokenStatus.mockResolvedValue({ status });
      renderTile();

      // Token status resolved; the action stays on the connected path.
      await waitFor(() => expect(mockTokenStatus).toHaveBeenCalledWith("salesforce"));
      expect(screen.queryByText("Token expired")).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Reconnect" })).not.toBeInTheDocument();
    }
  );
});
