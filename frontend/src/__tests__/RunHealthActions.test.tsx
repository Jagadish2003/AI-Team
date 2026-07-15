/**
 * R18-C2 T5 — In-context reconnect + checkpoint-reset actions on the Run-Health
 * dashboard Connectors panel.
 *
 * Verifies:
 *  - Reconnect is surfaced for a connector whose auth needs attention and
 *    deep-links into the existing Integration Hub flow (connector identified via
 *    its category).
 *  - Reset checkpoint is surfaced (owner-only) for a stalled checkpoint, requires
 *    confirmation explaining the full-reread impact, calls the existing
 *    owner-only reset endpoint, shows whether a checkpoint existed and was
 *    cleared, and refreshes the panel on success.
 *  - A reset failure shows a clear error and does NOT refresh / falsely resolve.
 *  - Analysts see reconnect but never the owner-only reset. Viewers are denied.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  fetchConnectorHealth: vi.fn(),
  fetchRunHealth: vi.fn(),
  fetchContentHealth: vi.fn(),
  fetchPackHealth: vi.fn(),
  fetchAttentionHealth: vi.fn(),
}));
const ingestion = vi.hoisted(() => ({ resetIngestionCheckpoint: vi.fn() }));
const auth = vi.hoisted(() => ({ role: "owner" as "owner" | "analyst" | "viewer" }));

vi.mock("../api/runHealthApi", () => api);
vi.mock("../api/ingestionApi", () => ingestion);
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { email: "health@example.com", role: auth.role } }),
}));
vi.mock("../components/common/PageShell", () => ({
  default: ({ title, actions, children }: { title: string; actions?: React.ReactNode; children: React.ReactNode }) => (
    <main><h1>{title}</h1>{actions}<div>{children}</div></main>
  ),
}));

import RunHealthDashboardPage from "../pages/RunHealthDashboardPage";

const HOUR = 3600;

// A connector whose auth needs attention AND whose checkpoint is stalled (>24h).
const attentionConnector = {
  connector_id: "servicenow",
  name: "ServiceNow",
  tier: "primary",
  connection_state: "needs_auth",
  auth_mode: "oauth",
  last_successful_ingestion: "2026-07-10T09:45:00Z",
  checkpoint_position: "cursor-42",
  checkpoint_captured_at: "2026-07-10T09:50:00Z",
  checkpoint_age_seconds: 72 * HOUR,
  last_error: "Token expired",
};

// A healthy connector — no actions should appear.
const healthyConnector = {
  connector_id: "jira",
  name: "Jira",
  tier: "secondary",
  connection_state: "connected",
  auth_mode: "oauth",
  last_successful_ingestion: "2026-07-13T09:45:00Z",
  checkpoint_position: "cursor-9",
  checkpoint_captured_at: "2026-07-13T09:50:00Z",
  checkpoint_age_seconds: 600,
  last_error: null,
};

const emptyContent = {
  org_id: "org-health",
  generated_at: "2026-07-13T10:00:00Z",
  indexed_by_source: [],
  chunks_total: 0,
  chunks_embedded: 0,
  pending_embeddings: 0,
  stale_chunks: 0,
  pending_change_events: 0,
  failed_refreshes: 0,
  backfill: {},
  redaction_count: 0,
  skipped: [],
};

function renderPage(path = "/run-health") {
  return render(<MemoryRouter initialEntries={[path]}><RunHealthDashboardPage /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.role = "owner";
  api.fetchConnectorHealth.mockResolvedValue({
    org_id: "org-health",
    connectors: [attentionConnector, healthyConnector],
  });
  api.fetchRunHealth.mockResolvedValue({ org_id: "org-health", runs: [] });
  api.fetchContentHealth.mockResolvedValue(emptyContent);
  api.fetchPackHealth.mockResolvedValue({ run_id: null, packs: [] });
  api.fetchAttentionHealth.mockResolvedValue({ org_id: "org-health", severity_order: [], items: [] });
  ingestion.resetIngestionCheckpoint.mockResolvedValue({
    ok: true, org_id: "org-health", connector_id: "servicenow", cleared: true,
  });
  Element.prototype.scrollIntoView = vi.fn();
});

describe("R18-C2 T5 — in-context connector actions", () => {
  it("shows a Reconnect deep-link into Integration Hub for an auth-attention connector", async () => {
    renderPage();
    const link = await screen.findByTestId("reconnect-servicenow");
    // ServiceNow lives in the operational_systems category deep-link.
    expect(link).toHaveAttribute("href", "/integration-hub?category=operational_systems");
  });

  it("does not show any action for a healthy connector", async () => {
    renderPage();
    await screen.findByTestId("reconnect-servicenow");
    expect(screen.queryByTestId("reconnect-jira")).not.toBeInTheDocument();
    expect(screen.queryByTestId("reset-checkpoint-jira")).not.toBeInTheDocument();
  });

  it("owner: reset requires confirmation, calls the reset endpoint, shows cleared, and refreshes", async () => {
    const user = userEvent.setup();
    renderPage();

    const resetBtn = await screen.findByTestId("reset-checkpoint-servicenow");
    await user.click(resetBtn);

    // Confirmation dialog explains the full-reread impact before anything runs.
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/full re-read/i)).toBeInTheDocument();
    expect(ingestion.resetIngestionCheckpoint).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: /reset checkpoint/i }));

    await waitFor(() =>
      expect(ingestion.resetIngestionCheckpoint).toHaveBeenCalledWith("servicenow"),
    );
    // Result reflects that a checkpoint existed and was cleared.
    expect(await screen.findByTestId("reset-result-servicenow")).toHaveTextContent(/cleared/i);
    // Panel refreshes after success (initial load + post-reset refresh).
    await waitFor(() => expect(api.fetchConnectorHealth).toHaveBeenCalledTimes(2));
  });

  it("shows 'no checkpoint existed' when the reset reports nothing was cleared", async () => {
    ingestion.resetIngestionCheckpoint.mockResolvedValue({
      ok: true, org_id: "org-health", connector_id: "servicenow", cleared: false,
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByTestId("reset-checkpoint-servicenow"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /reset checkpoint/i }));
    expect(await screen.findByTestId("reset-result-servicenow")).toHaveTextContent(/no checkpoint existed/i);
  });

  it("reset failure shows a clear error and does NOT refresh or report resolved", async () => {
    ingestion.resetIngestionCheckpoint.mockRejectedValue(new Error("Reset failed on the server"));
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByTestId("reset-checkpoint-servicenow"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /reset checkpoint/i }));

    expect(await screen.findByTestId("reset-error-servicenow")).toHaveTextContent(/reset failed/i);
    // No success message.
    expect(screen.queryByTestId("reset-result-servicenow")).not.toBeInTheDocument();
    // Health was not re-fetched (known health left unchanged, not falsely resolved).
    expect(api.fetchConnectorHealth).toHaveBeenCalledTimes(1);
  });

  it("analyst sees Reconnect but never the owner-only Reset checkpoint", async () => {
    auth.role = "analyst";
    renderPage();
    expect(await screen.findByTestId("reconnect-servicenow")).toBeInTheDocument();
    expect(screen.queryByTestId("reset-checkpoint-servicenow")).not.toBeInTheDocument();
  });

  it("viewer is denied the dashboard entirely", () => {
    auth.role = "viewer";
    renderPage();
    expect(screen.getByText("Run Health access is restricted")).toBeInTheDocument();
    expect(api.fetchConnectorHealth).not.toHaveBeenCalled();
  });
});
