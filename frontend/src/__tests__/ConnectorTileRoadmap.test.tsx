/**
 * ConnectorTileRoadmap.test.tsx — R191-R1 T5 (AT-726)
 *
 * The customer-facing "Coming soon" roadmap labelling is WITHDRAWN from the
 * Integration Hub, behind `showRoadmapComingSoonLabels` (config/releaseFlags.ts).
 *
 * The point of these tests is that withdrawing a LABEL did not withdraw the
 * anchor-on-shipped HONESTY rule:
 *   - with the flag off (shipped default) a roadmap tile shows its ordinary
 *     status badge, but is STILL non-connectable — because every roadmap
 *     connector sits outside ConnectorTile's ENABLED_CONNECTOR_IDS product gate;
 *   - with the flag on the original T5 labelling comes back verbatim, so this is
 *     a reversible presentation change rather than a deleted feature.
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

// Mutable release flag: the component reads it during render, so flipping this
// between tests exercises both the withdrawn and the restored labelling.
const flags = vi.hoisted(() => ({ comingSoon: false }));
vi.mock("../config/releaseFlags", () => ({
  get showRoadmapComingSoonLabels() {
    return flags.comingSoon;
  },
  showRelease2ArcAUi: false,
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

beforeEach(() => {
  vi.clearAllMocks();
  flags.comingSoon = false;
});

describe("ConnectorTile — roadmap labelling withdrawn (default)", () => {
  it("shows no 'Coming soon' labelling on a roadmap tile", () => {
    renderTile(roadmapSap());
    expect(screen.queryByTestId("connector-roadmap-badge")).not.toBeInTheDocument();
    expect(screen.queryByText(/coming soon/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /coming soon/i })).not.toBeInTheDocument();
  });

  it("keeps the roadmap tile non-connectable on the product gate", () => {
    // The honesty guarantee that must survive removing the label: SAP is not in
    // ENABLED_CONNECTOR_IDS, so its action is still disabled — with the ordinary
    // unavailable reason rather than roadmap copy.
    renderTile(roadmapSap());
    const btn = screen.getByRole("button", { name: /connect/i });
    expect(btn).toBeDisabled();
    expect(btn.closest("[title]")?.getAttribute("title") ?? "").toMatch(
      /currently unavailable/i,
    );
  });

  it("states no roadmap or release-target copy anywhere on the tile", () => {
    const { container } = render(
      <ConnectorTile
        connector={roadmapSap() as any}
        icon={<span>ic</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={vi.fn()}
        onReconnect={vi.fn()}
      />,
    );
    expect(container.innerHTML).not.toMatch(/roadmap/i);
    expect(container.innerHTML).not.toMatch(/2\.0\.1/);
  });

  it("leaves a shipped connector connectable", () => {
    renderTile(shippedJira());
    expect(screen.queryByTestId("connector-roadmap-badge")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect/i })).toBeEnabled();
  });
});

describe("ConnectorTile — roadmap labelling restored by flag", () => {
  beforeEach(() => {
    flags.comingSoon = true;
  });

  it("renders SAP as a non-connectable 'Coming soon' tile", () => {
    renderTile(roadmapSap());
    expect(screen.getByTestId("connector-roadmap-badge")).toHaveTextContent("Coming soon");
    const btn = screen.getByRole("button", { name: /coming soon/i });
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
