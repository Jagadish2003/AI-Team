/**
 * R17-A2 / AT-464 (T7) — SharePoint catalog tile drives the real OAuth connect flow.
 *
 * Before this story the SharePoint tile was a dead-end placeholder: SharePoint
 * was not in ConnectorTile's ENABLED_CONNECTOR_IDS, so its Connect button was
 * disabled. The real Microsoft Graph OAuth backend (AT-462 SharePoint config,
 * reusing the Teams Graph app) and the generic auth-url → provider redirect →
 * callback flow already exist; this story enables SharePoint into that flow.
 *
 * Guards:
 *   1. The SharePoint tile is enabled and its Connect button fires onPrimary.
 *   2. connectConnectorApi('sharepoint') initiates the real OAuth flow — GET
 *      /api/connectors/sharepoint/auth-url then a browser redirect to Microsoft's
 *      consent screen.
 *
 * Run:
 *   npx vitest run src/__tests__/SharePointConnectorTileOAuth.test.tsx
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

function sharepointDisconnected() {
  return {
    id: "sharepoint",
    name: "SharePoint",
    category: "Docs / knowledge",
    tier: "standard" as const,
    status: "not_configured" as const,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Sites", "Documents"],
    signalStrength: 55,
  };
}

describe("AT-464 — SharePoint tile is enabled for the OAuth connect flow", () => {
  it("renders an enabled Connect button for SharePoint and fires onPrimary", () => {
    const onPrimary = vi.fn();
    render(
      <ConnectorTile
        connector={sharepointDisconnected() as any}
        icon={<span>SP</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={onPrimary}
      />,
    );

    const btn = screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);

    fireEvent.click(btn);
    expect(onPrimary).toHaveBeenCalledTimes(1);
  });
});

describe("AT-464 — connecting SharePoint initiates the real Microsoft Graph OAuth flow", () => {
  const realLocation = window.location;

  beforeEach(() => {
    delete (window as any).location;
    (window as any).location = { href: "" };
  });
  afterEach(() => {
    (window as any).location = realLocation;
    vi.restoreAllMocks();
  });

  it("calls GET /api/connectors/sharepoint/auth-url and redirects the browser", async () => {
    const authUrl =
      "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?scope=offline_access%20Sites.Read.All&state=nonce";
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ auth_url: authUrl, connector_id: "sharepoint" }),
      text: async () => "",
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchMock);

    await connectConnectorApi("sharepoint");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = (fetchMock as any).mock.calls[0];
    expect(String(calledUrl)).toContain("/api/connectors/sharepoint/auth-url");
    expect(init?.method ?? "GET").toBe("GET");
    expect(String(calledUrl).endsWith("/connect")).toBe(false);
    expect(window.location.href).toBe(authUrl);
  });
});
