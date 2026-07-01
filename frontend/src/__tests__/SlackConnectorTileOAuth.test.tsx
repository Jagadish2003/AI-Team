/**
 * R16-A2 / AT-422 (T7) — Slack catalog tile drives the real OAuth connect flow.
 *
 * Before this story the Slack tile was a dead end: Slack was not in
 * ConnectorTile's ENABLED_CONNECTOR_IDS, so its Connect button was disabled
 * ("Connecting new sources is currently unavailable"). The real OAuth backend
 * (AT-420 Slack config) and the generic auth-url → provider redirect flow
 * (CS-2 / AT-323) already existed; this story enables Slack into that flow.
 *
 * These tests guard two things:
 *   1. The Slack tile is enabled and its Connect button fires onPrimary
 *      (which the page wires to connectConnector → the OAuth flow).
 *   2. connectConnectorApi('slack') initiates the real OAuth flow — GET
 *      /api/connectors/slack/auth-url then a browser redirect — and never the
 *      old POST /connect stub.
 *
 * Run:
 *   npx vitest run src/__tests__/SlackConnectorTileOAuth.test.tsx
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { connectConnectorApi } from "../services/staticApi";

// ConnectorTile fetches token-status only for connected+enabled tiles; mock the
// boundary so a disconnected Slack tile renders without a real network call.
vi.mock("../services/staticApi", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/staticApi")>();
  return { ...actual, fetchTokenStatus: vi.fn() };
});

import ConnectorTile from "../components/integrations/ConnectorTile";

function slackDisconnected() {
  return {
    id: "slack",
    name: "Slack",
    category: "Comms · Ops",
    tier: "standard" as const,
    status: "disconnected" as const,
    configured: false,
    metrics: [],
    lastSynced: "—",
    reads: ["Channels", "Threads"],
    signalStrength: 79,
  };
}

// A connector that is NOT in ConnectorTile's ENABLED_CONNECTOR_IDS allowlist, so
// its Connect button must stay disabled. (Teams used to serve this role; it is
// now OAuth-enabled per AT-436, so a still-unwired connector is used instead.)
function nonOauthDisconnected() {
  return { ...slackDisconnected(), id: "workday", name: "Workday" };
}

describe("AT-422 — Slack tile is enabled for the OAuth connect flow", () => {
  it("renders an enabled Connect button for Slack and fires onPrimary", () => {
    const onPrimary = vi.fn();
    render(
      <ConnectorTile
        connector={slackDisconnected() as any}
        icon={<span>SL</span>}
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

  it("leaves a non-OAuth connector (Workday) disabled — allowlist stays scoped", () => {
    render(
      <ConnectorTile
        connector={nonOauthDisconnected() as any}
        icon={<span>WD</span>}
        selected={false}
        onSelect={vi.fn()}
        onPrimary={vi.fn()}
      />,
    );
    const btn = screen.getByRole("button", { name: "Connect" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });
});

describe("AT-422 — connecting Slack initiates the real OAuth flow", () => {
  const realLocation = window.location;

  beforeEach(() => {
    delete (window as any).location;
    (window as any).location = { href: "" };
  });
  afterEach(() => {
    (window as any).location = realLocation;
    vi.restoreAllMocks();
  });

  it("calls GET /api/connectors/slack/auth-url and redirects the browser", async () => {
    const authUrl =
      "https://slack.com/oauth/v2/authorize?scope=channels:read,channels:history&state=nonce";
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ auth_url: authUrl, connector_id: "slack" }),
      text: async () => "",
    })) as unknown as typeof fetch;
    vi.stubGlobal("fetch", fetchMock);

    await connectConnectorApi("slack");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [calledUrl, init] = (fetchMock as any).mock.calls[0];
    expect(String(calledUrl)).toContain("/api/connectors/slack/auth-url");
    expect(init?.method ?? "GET").toBe("GET");
    expect(String(calledUrl).endsWith("/connect")).toBe(false);
    // Browser is redirected to the Slack provider login (real OAuth).
    expect(window.location.href).toBe(authUrl);
  });
});
