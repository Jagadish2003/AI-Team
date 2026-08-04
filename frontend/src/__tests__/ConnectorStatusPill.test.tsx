/**
 * ConnectorStatusPill.test.tsx
 *
 * The Integration Hub status pill is derived from the SAME gate as the connect
 * action, not from the raw catalog status:
 *
 *   - connect action DISABLED (connector outside ENABLED_CONNECTOR_IDS) →
 *     "Not configured". "Disconnected" would imply a connection the user could
 *     restore, and the action that would restore it is disabled.
 *   - connect action ENABLED → honestly "Connected" or "Disconnected".
 *
 * The rule lives in connectorEnablement.ts so the tile, its detail panel and the
 * hero card cannot disagree about the same connector.
 *
 * Also pins that the neutral pills carry the connected pill's padding/weight, so
 * a tile's pill does not change size as its status changes.
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";

import ConnectorTile from "../components/integrations/ConnectorTile";
import Badge from "../components/common/Badge";
// Raw CSS import (vite/client types this as a string) so the stylesheet can be
// asserted directly — jsdom does not apply styles.css to rendered nodes.
import stylesheet from "../styles.css?raw";
import {
  connectorBadgeStatus,
  isConnectorEnabled,
} from "../components/integrations/connectorEnablement";

vi.mock("../services/staticApi", () => ({
  fetchTokenStatus: vi.fn().mockResolvedValue({ status: "connected" }),
}));

vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({ user: { email: "srivani@dwp.com", role: "analyst" } }),
}));

function connector(over: Record<string, unknown> = {}) {
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
    ...over,
  };
}

function renderTile(c: unknown) {
  render(
    <ConnectorTile
      connector={c as any}
      icon={<span>ic</span>}
      selected={false}
      onSelect={vi.fn()}
      onPrimary={vi.fn()}
      onReconnect={vi.fn()}
    />,
  );
}

beforeEach(() => vi.clearAllMocks());

describe("connectorBadgeStatus — the rule", () => {
  it("reports Not configured for a connector whose connect action is disabled", () => {
    // SAP / Dynamics 365 / Zendesk are not in ENABLED_CONNECTOR_IDS.
    for (const id of ["sap", "dynamics365", "zendesk", "notion", "gitlab"]) {
      const c = connector({ id });
      expect(isConnectorEnabled(c)).toBe(false);
      expect(connectorBadgeStatus(c)).toBe("not_configured");
    }
  });

  it("reports Connected / Disconnected for a connector whose action is enabled", () => {
    expect(connectorBadgeStatus(connector({ id: "jira", status: "connected" }))).toBe(
      "connected",
    );
    expect(connectorBadgeStatus(connector({ id: "jira", status: "disconnected" }))).toBe(
      "disconnected",
    );
  });

  it("normalises every other catalog status onto the two honest states", () => {
    // An enabled connector is never left on an ambiguous catalog value.
    for (const status of ["not_connected", "not_configured", "coming_soon"]) {
      expect(connectorBadgeStatus(connector({ id: "slack", status }))).toBe("disconnected");
    }
  });

  it("treats a multi-scope cloud connector as enabled (it onboards in the panel)", () => {
    const aws = connector({ id: "aws_events", multiScope: true, status: "connected" });
    expect(isConnectorEnabled(aws)).toBe(true);
    expect(connectorBadgeStatus(aws)).toBe("connected");
  });
});

describe("ConnectorTile — status pill", () => {
  it("shows Not configured on a disabled tile, not Disconnected", () => {
    renderTile(connector());
    expect(screen.getByText("Not configured")).toBeInTheDocument();
    expect(screen.queryByText("Disconnected")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect/i })).toBeDisabled();
  });

  it("shows Disconnected on an enabled but unconnected tile", () => {
    renderTile(connector({ id: "jira", name: "Jira" }));
    expect(screen.getByText("Disconnected")).toBeInTheDocument();
    expect(screen.queryByText("Not configured")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /connect/i })).toBeEnabled();
  });

  it("shows Connected on a connected tile", () => {
    renderTile(connector({ id: "salesforce", name: "Salesforce", status: "connected" }));
    expect(screen.getByText("Connected")).toBeInTheDocument();
  });
});

describe("Badge — neutral pills match the connected pill's shape", () => {
  it("gives Disconnected and Not configured the shared neutral pill class", () => {
    render(
      <>
        <Badge status="disconnected" />
        <Badge status="not_configured" />
        <Badge status="not_connected" />
      </>,
    );
    for (const label of ["Disconnected", "Not configured", "Not connected"]) {
      expect(screen.getByText(label)).toHaveClass("integration-neutral-status-pill");
    }
  });

  it("keeps the connected pill on its own colour class", () => {
    render(<Badge status="connected" />);
    const pill = screen.getByText("Connected");
    expect(pill).toHaveClass("integration-connected-status-pill");
    expect(pill).not.toHaveClass("integration-neutral-status-pill");
  });

  it("declares the same padding and weight for both pill classes in styles.css", () => {
    // jsdom does not load styles.css, so the class assertions above cannot prove
    // the pills are actually the same size. Read the stylesheet and compare the
    // two declarations directly — that is the property the design requires.
    const css = String(stylesheet ?? "");
    expect(css, "styles.css did not load").not.toBe("");
    const block = (selector: string) => {
      const m = css.match(
        new RegExp(`\\.${selector}\\s*\\{([^}]*)\\}`),
      );
      expect(m, `${selector} missing from styles.css`).toBeTruthy();
      return m![1];
    };
    const decl = (body: string, prop: string) =>
      body.match(new RegExp(`${prop}\\s*:\\s*([^;]+);`))?.[1].trim();

    const connected = block("integration-connected-status-pill");
    const neutral = block("integration-neutral-status-pill");

    expect(decl(neutral, "padding")).toBe(decl(connected, "padding"));
    expect(decl(neutral, "font-weight")).toBe(decl(connected, "font-weight"));
  });
});
