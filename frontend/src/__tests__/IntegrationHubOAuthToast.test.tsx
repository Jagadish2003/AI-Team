/**
 * CS-2 / AT-327 (T5) — IntegrationHubPage shows a toast after the OAuth flow.
 *
 * OAuthCallbackPage navigates to /integration-hub with navigation state:
 *   { justConnected: <connectorId> }  on success
 *   { oauthError:    <errorCode>   }  on failure
 * IntegrationHubPage must read that state on mount and surface a toast:
 *   T5-AC1 success toast when justConnected present
 *   T5-AC2 error toast when oauthError present
 *   T5-AC3 success toast names the connector
 *   T5-AC4 error toast shows the OAuth error code
 *
 * Run:
 *   npx vitest run src/__tests__/IntegrationHubOAuthToast.test.tsx
 */
import React from "react";
import { render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import IntegrationHubPage from "../pages/IntegrationHubPage";
import { useConnectorContext } from "../context/ConnectorContext";
import { useToast } from "../components/common/Toast";

vi.mock("../context/ConnectorContext", () => ({ useConnectorContext: vi.fn() }));
vi.mock("../components/common/Toast", () => ({ useToast: vi.fn() }));

// Stub heavy presentational children — the toast effect is what we exercise.
vi.mock("../components/common/PageShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("../components/integrations/ConnectorGroupSection", () => ({ default: () => <div /> }));
vi.mock("../components/integrations/RightPanel", () => ({ default: () => <div /> }));

const mockUseConnector = vi.mocked(useConnectorContext);
const mockUseToast = vi.mocked(useToast);
const push = vi.fn();

const SALESFORCE = { id: "salesforce", name: "Salesforce", status: "connected" };

beforeEach(() => {
  vi.clearAllMocks();
  mockUseToast.mockReturnValue({ push } as any);
  mockUseConnector.mockReturnValue({
    recommended: [SALESFORCE],
    standard: [],
    selectedConnectorId: null,
    selectConnector: vi.fn(),
    connectConnector: vi.fn(),
    configureSync: vi.fn(),
    loading: false,
    error: null,
    refetch: vi.fn(),
  } as any);
});

function renderWithState(state: unknown) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: "/integration-hub", state }]}>
      <IntegrationHubPage />
    </MemoryRouter>
  );
}

describe("IntegrationHubPage OAuth toast (AT-327 T5)", () => {
  it("shows a success toast naming the connector when justConnected is set (T5-AC1/AC3)", async () => {
    renderWithState({ justConnected: "salesforce" });
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("Salesforce connected successfully", "success")
    );
  });

  it("shows an error toast with the OAuth error code when oauthError is set (T5-AC2/AC4)", async () => {
    renderWithState({ oauthError: "access_denied" });
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("Connection failed: access_denied", "error")
    );
  });

  it("does not toast on a normal page load (no OAuth state)", async () => {
    renderWithState(null);
    await waitFor(() => {}); // allow effects to flush
    expect(push).not.toHaveBeenCalled();
  });

  it("falls back to the connector id when the connector is not in the list", async () => {
    renderWithState({ justConnected: "workday" });
    await waitFor(() =>
      expect(push).toHaveBeenCalledWith("workday connected successfully", "success")
    );
  });
});
