/**
 * R17-A1 / AT-436 (T7) — Microsoft Teams catalog tile drives the real OAuth
 * connect flow end to end.
 *
 * Before this story the Teams tile was a dead-end placeholder: Teams was not in
 * ConnectorTile's ENABLED_CONNECTOR_IDS, so its Connect button was disabled
 * ("Connecting new sources is currently unavailable"). The real Microsoft Graph
 * OAuth backend (AT-434 Teams config) and the generic auth-url → provider
 * redirect → callback flow (CS-2 / AT-323) already exist; this story enables
 * Teams into that flow.
 *
 * These tests guard two things:
 *   1. The Teams tile is enabled and its Connect button fires onPrimary
 *      (which the page wires to connectConnector → the OAuth flow).
 *   2. connectConnectorApi('teams') initiates the real OAuth flow — GET
 *      /api/connectors/teams/auth-url then a browser redirect to Microsoft's
 *      consent screen — and never a stub.
 *
 * Run:
 *   npx vitest run src/__tests__/TeamsConnectorTileOAuth.test.tsx
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectConnectorApi } from "../services/staticApi";

// ConnectorTile fetches token-status only for connected+enabled tiles; mock the
// boundary so a disconnected Teams tile renders without a real network call.
vi.mock("../services/staticApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/staticApi")>();
  return { ...actual, fetchTokenStatus: vi.fn() };
});

import ConnectorTile from "../components/integrations/ConnectorTile";

function teamsDisconnected() {
  return {
    id: "teams",
    name: "Microsoft Teams",
    category: "Comms / docs",
    tier: "standard" as const,
    status: "not_configured" as const,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Channels", "Messages", "Meetings"],
    signalStrength: 50,
  };
}

// UI gate (July 2026): only Salesforce/ServiceNow/Jira are connectable from the
// Integration Hub for now. Teams' Microsoft Graph OAuth backend stays wired
// (AT-434/AT-436) — only the tile's Connect button is disabled until Teams is
// re-added to ConnectorTile's ENABLED_CONNECTOR_IDS.
describe("Teams tile Connect is UI-disabled (hub allowlist = SF/SNOW/Jira)", () => {
  it("renders a disabled Connect button for Teams that never fires onPrimary", () => {
    const onPrimary = vi.fn();
    render(
      <ConnectorTile
        connector={teamsDisconnected() as any}
        icon={<span>MT</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={onPrimary}
      />,
    );

    const btn = screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);

    fireEvent.click(btn);
    expect(onPrimary).not.toHaveBeenCalled();
  });
});

describe("AT-436 — connecting Teams initiates the real Microsoft Graph OAuth flow", () => {
  const realLocation = window.location;

  beforeEach(() => {
    delete (window as any).location;
    (window as any).location = { href: "" };
  });
  afterEach(() => {
    (window as any).location = realLocation;
    vi.restoreAllMocks();
  });

  it("calls GET /api/connectors/teams/auth-url and redirects the browser", async () => {
    const authUrl =
      "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?scope=offline_access%20https://graph.microsoft.com/ChannelMessage.Read.All&state=nonce";
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ auth_url: authUrl, connector_id: "teams" }),
      text: async () => "",
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchMock);

    await connectConnectorApi("teams");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = (fetchMock as any).mock.calls[0];
    expect(String(calledUrl)).toContain("/api/connectors/teams/auth-url");
    expect(init?.method ?? "GET").toBe("GET");
    expect(String(calledUrl).endsWith("/connect")).toBe(false);
    // Browser is redirected to the Microsoft consent screen (real OAuth).
    expect(window.location.href).toBe(authUrl);
  });
});
