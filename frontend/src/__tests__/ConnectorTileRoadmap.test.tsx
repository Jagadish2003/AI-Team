/**
 * ConnectorTileRoadmap.test.tsx — R191-R1 T5 (AT-726)
 *
 * A roadmap connector (SAP/D365 and any tile whose ingestion does not ship yet)
 * renders with the old disabled grey Connect posture in the Integration Hub,
 * without a visible "Coming soon" label. A shipped connector is unaffected.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import ConnectorTile from "../components/integrations/ConnectorTile";

vi.mock("../services/staticApi", () => ({
  fetchTokenStatus: vi.fn().mockResolvedValue({ status: "connected" }),
}));

// Analyst so the Connect action would otherwise be enabled — proving the
// disabled state comes from the roadmap flag, not the role gate.
vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({ user: { email: "srivani@dwp.com", role: "analyst" } }),
}));

function roadmapSap() {
  return {
    id: "sap",
    name: "SAP",
    category: "ERP · Process",
    tier: "standard" as const,
    status: "disconnected" as const,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Change Documents"],
    signalStrength: 76,
    roadmap: true,
    roadmapTarget: "2.0.1",
  };
}

function roadmapUnscheduled() {
  return { ...roadmapSap(), id: "notion", name: "Notion", roadmapTarget: "unscheduled" };
}

function shippedJira() {
  return {
    id: "jira",
    name: "Jira",
    category: "Issue tracking",
    tier: "standard" as const,
    status: "disconnected" as const,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Issues"],
    signalStrength: 70,
    roadmap: false,
    roadmapTarget: null,
  };
}

function renderTile(connector: unknown) {
  const onPrimary = vi.fn();
  render(
    <ConnectorTile
      connector={connector as any}
      icon={<span>ic</span>}
      selected={false}
      onSelect={vi.fn()}
      onPrimary={onPrimary}
      onReconnect={vi.fn()}
    />,
  );
  return { onPrimary };
}

beforeEach(() => vi.clearAllMocks());

describe("ConnectorTile — roadmap (AT-726)", () => {
  it("renders SAP as a disabled Connect tile without a Coming soon label", () => {
    renderTile(roadmapSap());
    expect(screen.queryByTestId("connector-roadmap-badge")).not.toBeInTheDocument();
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: /^connect$/i });
    expect(btn).toBeDisabled();
  });

  it("renders an unscheduled roadmap tile as disabled Connect", () => {
    renderTile(roadmapUnscheduled());
    expect(screen.queryByTestId("connector-roadmap-badge")).not.toBeInTheDocument();
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^connect$/i })).toBeDisabled();
  });

  it("leaves a shipped connector connectable", () => {
    renderTile(shippedJira());
    expect(screen.queryByTestId("connector-roadmap-badge")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect/i })).toBeEnabled();
  });
});
