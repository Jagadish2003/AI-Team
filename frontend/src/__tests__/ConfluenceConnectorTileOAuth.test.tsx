/**
 * R17-A2 / AT-464 (T7) — Confluence catalog tile drives the real OAuth connect flow.
 *
 * Before this story the Confluence tile was a dead-end placeholder: Confluence
 * was not in ConnectorTile's ENABLED_CONNECTOR_IDS, so its Connect button was
 * disabled. The real Atlassian OAuth backend (AT-462 Confluence config) and the
 * generic auth-url → provider redirect → callback flow already exist; this story
 * enables Confluence into that flow.
 *
 * Guards:
 *   1. The Confluence tile is enabled and its Connect button fires onPrimary
 *      (which the page wires to connectConnector → the OAuth flow).
 *   2. connectConnectorApi('confluence') initiates the real OAuth flow — GET
 *      /api/connectors/confluence/auth-url then a browser redirect to Atlassian's
 *      consent screen.
 *
 * Run:
 *   npx vitest run src/__tests__/ConfluenceConnectorTileOAuth.test.tsx
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectConnectorApi } from "../services/staticApi";

vi.mock("../services/staticApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/staticApi")>();
  return { ...actual, fetchTokenStatus: vi.fn() };
});

import ConnectorTile from "../components/integrations/ConnectorTile";

function confluenceDisconnected() {
  return {
    id: "confluence",
    name: "Confluence",
    category: "Docs / knowledge",
    tier: "standard" as const,
    status: "not_configured" as const,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Spaces", "Pages"],
    signalStrength: 60,
  };
}

// UI gate (July 2026): only Salesforce/ServiceNow/Jira are connectable from the
// Integration Hub for now. Confluence's Atlassian OAuth backend stays wired
// (AT-462/AT-464) — only the tile's Connect button is disabled until Confluence
// is re-added to ConnectorTile's ENABLED_CONNECTOR_IDS.
describe("Confluence tile Connect is UI-disabled (hub allowlist = SF/SNOW/Jira)", () => {
  it("renders a disabled Connect button for Confluence that never fires onPrimary", () => {
    const onPrimary = vi.fn();
    render(
      <ConnectorTile
        connector={confluenceDisconnected() as any}
        icon={<span>CF</span>}
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

describe("AT-464 — connecting Confluence initiates the real Atlassian OAuth flow", () => {
  const realLocation = window.location;

  beforeEach(() => {
    delete (window as any).location;
    (window as any).location = { href: "" };
  });
  afterEach(() => {
    (window as any).location = realLocation;
    vi.restoreAllMocks();
  });

  it("calls GET /api/connectors/confluence/auth-url and redirects the browser", async () => {
    const authUrl =
      "https://auth.atlassian.com/authorize?scope=read:confluence-content.all%20offline_access&state=nonce";
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ auth_url: authUrl, connector_id: "confluence" }),
      text: async () => "",
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchMock);

    await connectConnectorApi("confluence");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = (fetchMock as any).mock.calls[0];
    expect(String(calledUrl)).toContain("/api/connectors/confluence/auth-url");
    expect(init?.method ?? "GET").toBe("GET");
    expect(String(calledUrl).endsWith("/connect")).toBe(false);
    expect(window.location.href).toBe(authUrl);
  });
});
