/**
 * ConnectorTileRoadmap.test.tsx — R191-R1 T5 (AT-726)
 *
 * A roadmap connector (SAP/D365 and any tile whose ingestion does not ship yet)
 * renders as a non-connectable "Coming — <target>" tile in the Integration Hub,
 * regardless of role. A shipped connector is unaffected.
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
  it("renders SAP as a non-connectable 'Coming — 2.0.1' tile", () => {
    renderTile(roadmapSap());
    // Roadmap badge shows the committed target.
    expect(screen.getByTestId("connector-roadmap-badge")).toHaveTextContent("Coming — 2.0.1");
    // The action button is labelled and disabled — never "Connect".
    const btn = screen.getByRole("button", { name: /coming . 2\.0\.1/i });
    expect(btn).toBeDisabled();
    expect(screen.queryByRole("button", { name: /^connect$/i })).not.toBeInTheDocument();
  });

  it("renders an unscheduled roadmap tile as 'Coming soon'", () => {
    renderTile(roadmapUnscheduled());
    expect(screen.getByTestId("connector-roadmap-badge")).toHaveTextContent("Coming soon");
    expect(screen.getByRole("button", { name: /coming soon/i })).toBeDisabled();
  });

  it("leaves a shipped connector connectable", () => {
    renderTile(shippedJira());
    expect(screen.queryByTestId("connector-roadmap-badge")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect/i })).toBeEnabled();
  });
});
