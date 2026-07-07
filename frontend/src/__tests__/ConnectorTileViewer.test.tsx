/**
 * ConnectorTileViewer.test.tsx
 *
 * Viewers get a read-only Integration Hub: connecting is an analyst+ write
 * (the connector auth-url / token routes are analyst+), so the Connect action
 * is disabled for a viewer. The read-only "View data" action on an already-
 * connected + configured system stays enabled.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import ConnectorTile from "../components/integrations/ConnectorTile";

vi.mock("../services/staticApi", () => ({
  fetchTokenStatus: vi.fn().mockResolvedValue({ status: "connected" }),
}));

const h: { role: "owner" | "analyst" | "viewer" } = { role: "viewer" };
vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({ user: { email: "likhith@dwp.com", role: h.role } }),
}));

// An enabled, NOT-yet-connected system → its action button is "Connect".
function disconnectedJira() {
  return {
    id: "jira",
    name: "Jira",
    category: "Issue tracking",
    tier: "recommended" as const,
    recommendedRank: 2,
    status: "disconnected" as const,
    configured: false,
    metrics: [],
    lastSynced: "",
    reads: ["Issues"],
    signalStrength: 70,
  };
}

// An enabled, connected + configured system → its action button is "View data".
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
    reads: ["Accounts"],
    signalStrength: 94,
  };
}

function renderTile(connector: unknown) {
  render(
    <ConnectorTile
      connector={connector as any}
      icon={<span>ic</span>}
      selected={false}
      onSelect={vi.fn()}
      onPrimary={vi.fn()}
      onReconnect={vi.fn()}
    />,
  );
}

beforeEach(() => vi.clearAllMocks());

describe("ConnectorTile — viewer read-only Connect", () => {
  it("disables the Connect button for a viewer", () => {
    h.role = "viewer";
    renderTile(disconnectedJira());
    expect(screen.getByRole("button", { name: /connect/i })).toBeDisabled();
  });

  it("enables the Connect button for an analyst", () => {
    h.role = "analyst";
    renderTile(disconnectedJira());
    expect(screen.getByRole("button", { name: /connect/i })).toBeEnabled();
  });

  it("keeps the read-only View data action enabled for a viewer", async () => {
    h.role = "viewer";
    renderTile(connectedSalesforce());
    expect(
      await screen.findByRole("button", { name: /view data/i }),
    ).toBeEnabled();
  });
});
