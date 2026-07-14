import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({
  fetchConnectorHealth: vi.fn(),
  fetchRunHealth: vi.fn(),
  fetchContentHealth: vi.fn(),
  fetchPackHealth: vi.fn(),
  fetchAttentionHealth: vi.fn(),
}));
const auth = vi.hoisted(() => ({ role: "owner" as "owner" | "analyst" | "viewer" }));

vi.mock("../api/runHealthApi", () => api);
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { email: "health@example.com", role: auth.role } }),
}));
vi.mock("../components/common/PageShell", () => ({
  default: ({ title, description, actions, children }: { title: string; description: string; actions?: React.ReactNode; children: React.ReactNode }) => (
    <main><h1>{title}</h1><p>{description}</p>{actions}<div>{children}</div></main>
  ),
}));

import RunHealthDashboardPage from "../pages/RunHealthDashboardPage";

const generatedAt = "2026-07-13T10:00:00Z";

const connectorResponse = {
  org_id: "org-health",
  connectors: [{
    connector_id: "servicenow",
    name: "ServiceNow",
    tier: "primary",
    connection_state: "connected",
    auth_mode: "oauth",
    last_successful_ingestion: "2026-07-13T09:45:00Z",
    checkpoint_position: "cursor-42",
    checkpoint_captured_at: "2026-07-13T09:50:00Z",
    checkpoint_age_seconds: 600,
    last_error: null,
  }],
};

const runResponse = {
  org_id: "org-health",
  runs: [
    {
      run_id: "run-healthy-0001",
      status: "complete",
      health_status: "healthy",
      degraded: false,
      started_at: "2026-07-13T09:00:00Z",
      updated_at: "2026-07-13T09:05:00Z",
      duration_seconds: 300,
      systems: ["servicenow"],
      system_count: 1,
      pack_id: "ncino",
      detectors_evaluated: 4,
      detectors_fired: 1,
      opportunities: 2,
      degraded_stages: [],
      stage_outcomes: [],
    },
    {
      run_id: "run-degraded-01",
      status: "complete",
      health_status: "degraded",
      degraded: true,
      started_at: "2026-07-13T08:00:00Z",
      updated_at: "2026-07-13T08:03:00Z",
      duration_seconds: 180,
      systems: ["servicenow"],
      system_count: 1,
      pack_id: "ncino",
      detectors_evaluated: 4,
      detectors_fired: 0,
      opportunities: 0,
      degraded_stages: [{ stage: "roadmap", reason: "Recommendation generation timed out" }],
      stage_outcomes: [{ stage: "roadmap", level: "WARNING", message: "Timed out" }],
    },
    { run_id: "run-failed-0001", status: "failed", health_status: "failed", degraded: false, degraded_stages: [], stage_outcomes: [] },
    { run_id: "run-running-001", status: "running", health_status: "running", degraded: false, degraded_stages: [], stage_outcomes: [] },
  ],
};

const contentResponse = {
  org_id: "org-health",
  generated_at: generatedAt,
  indexed_by_source: [{ source_system: "document", chunk_count: 120, embedded_count: 110 }],
  chunks_total: 120,
  chunks_embedded: 110,
  pending_embeddings: 10,
  stale_chunks: 3,
  pending_change_events: 2,
  failed_refreshes: 1,
  backfill: { active_model: "embed-v2", embedded_total: 110, on_active_model: 90, awaiting_backfill: 20, progress: 0.75, complete: false },
  redaction_count: 7,
  skipped: [{ reason: "unsupported_format", count: 2 }],
};

const packResponse = {
  run_id: "run-healthy-0001",
  packs: [{
    pack_id: "ncino",
    pack_name: "Commercial Lending",
    pack_version: "18.2.0",
    detector_count: 2,
    detectors: ["loan_workflow", "covenant_tracking"],
  }],
};

const attentionResponse = {
  org_id: "org-health",
  severity_order: ["critical", "high", "medium", "low"] as const,
  items: [{
    id: "auth:salesforce",
    condition: "expired_authentication",
    severity: "critical" as const,
    title: "Salesforce authentication expired",
    explanation: "Reconnect Salesforce so ingestion can continue.",
    connector_id: "salesforce",
    run_id: null,
    timestamp: "2026-07-13T09:55:00Z",
    panel: "connectors" as const,
    href: "/run-health?panel=connectors&connector=salesforce",
    details: {},
  }],
};

function renderPage(path = "/run-health") {
  return render(<MemoryRouter initialEntries={[path]}><RunHealthDashboardPage /></MemoryRouter>);
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.role = "owner";
  api.fetchConnectorHealth.mockResolvedValue(connectorResponse);
  api.fetchRunHealth.mockResolvedValue(runResponse);
  api.fetchContentHealth.mockResolvedValue(contentResponse);
  api.fetchPackHealth.mockResolvedValue(packResponse);
  api.fetchAttentionHealth.mockResolvedValue(attentionResponse);
  Element.prototype.scrollIntoView = vi.fn();
});

describe("RunHealthDashboardPage", () => {
  it("renders all five areas from live response data with direct attention links", async () => {
    renderPage("/run-health?panel=connectors&connector=salesforce");

    expect(await screen.findByText("Salesforce authentication expired")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Connectors" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Runs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Content and Freshness" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Packs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Attention Strip" })).toBeInTheDocument();

    expect(screen.getByText("ServiceNow")).toBeInTheDocument();
    expect(screen.getByText("Recommendation generation timed out")).toBeInTheDocument();
    expect(screen.getByText("Commercial Lending")).toBeInTheDocument();
    expect(screen.getByText("Loan Workflow")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view connectors details/i })).toHaveAttribute(
      "href",
      "/run-health?panel=connectors&connector=salesforce",
    );
    // Selected panel is highlighted with the app accent ring (theme-aware).
    expect(screen.getByTestId("panel-connectors")).toHaveClass("ring-accent/30");
  });

  it("distinguishes successful, degraded, failed, and in-progress runs", async () => {
    renderPage();
    const panel = await screen.findByTestId("panel-runs");
    expect(within(panel).getByText("Successful").parentElement).toHaveTextContent("1");
    expect(within(panel).getAllByText("Degraded")[0].parentElement).toHaveTextContent("1");
    expect(within(panel).getAllByText("Failed")[0].parentElement).toHaveTextContent("1");
    expect(within(panel).getByText("In progress").parentElement).toHaveTextContent("1");
    expect(panel).toHaveAttribute("data-state", "degraded");
  });

  it("labels the Analyst experience read-only while preserving all health reads", async () => {
    auth.role = "analyst";
    renderPage();
    expect(await screen.findByText("Read-only")).toBeInTheDocument();
    expect(screen.getByText("ServiceNow")).toBeInTheDocument();
    expect(api.fetchAttentionHealth).toHaveBeenCalledTimes(1);
  });

  it("denies Viewers before any health data is requested", () => {
    auth.role = "viewer";
    renderPage();
    expect(screen.getByText("Run Health access is restricted")).toBeInTheDocument();
    expect(api.fetchConnectorHealth).not.toHaveBeenCalled();
    expect(api.fetchRunHealth).not.toHaveBeenCalled();
    expect(api.fetchContentHealth).not.toHaveBeenCalled();
    expect(api.fetchPackHealth).not.toHaveBeenCalled();
    expect(api.fetchAttentionHealth).not.toHaveBeenCalled();
  });

  it("reports an API failure without manufacturing green or zero content health", async () => {
    api.fetchContentHealth.mockRejectedValue(new Error("Retrieval store unavailable"));
    renderPage();

    const contentPanel = await screen.findByTestId("panel-content");
    expect(await within(contentPanel).findByText("Content health unavailable")).toBeInTheDocument();
    expect(within(contentPanel).getByText("Retrieval store unavailable")).toBeInTheDocument();
    expect(contentPanel).toHaveAttribute("data-state", "error");
    expect(within(contentPanel).queryByText("Indexed chunks")).toBeNull();
    expect(screen.getByText("Health partially unavailable")).toBeInTheDocument();
  });

  it("shows independent loading states for every health read", () => {
    const pending = new Promise(() => undefined);
    api.fetchConnectorHealth.mockReturnValue(pending);
    api.fetchRunHealth.mockReturnValue(pending);
    api.fetchContentHealth.mockReturnValue(pending);
    api.fetchPackHealth.mockReturnValue(pending);
    api.fetchAttentionHealth.mockReturnValue(pending);
    renderPage();

    expect(screen.getByText("Loading connector health…")).toBeInTheDocument();
    expect(screen.getByText("Loading run health…")).toBeInTheDocument();
    expect(screen.getByText("Loading content health…")).toBeInTheDocument();
    expect(screen.getByText("Loading pack health…")).toBeInTheDocument();
    expect(screen.getByText("Loading attention items…")).toBeInTheDocument();
  });

  it("uses explicit empty states when successful reads contain no activity", async () => {
    api.fetchConnectorHealth.mockResolvedValue({ org_id: "org-health", connectors: [] });
    api.fetchRunHealth.mockResolvedValue({ org_id: "org-health", runs: [] });
    api.fetchContentHealth.mockResolvedValue({
      ...contentResponse,
      indexed_by_source: [],
      chunks_total: 0,
      chunks_embedded: 0,
      pending_embeddings: 0,
      stale_chunks: 0,
      pending_change_events: 0,
      failed_refreshes: 0,
      backfill: { active_model: null, embedded_total: 0, on_active_model: 0, awaiting_backfill: 0, progress: null, complete: false },
      redaction_count: 0,
      skipped: [],
    });
    api.fetchPackHealth.mockResolvedValue({ run_id: null, packs: [] });
    api.fetchAttentionHealth.mockResolvedValue({ org_id: "org-health", severity_order: ["critical", "high", "medium", "low"], items: [] });
    renderPage();

    expect(await screen.findByText("No connectors configured")).toBeInTheDocument();
    expect(screen.getByText("No discovery runs yet")).toBeInTheDocument();
    expect(screen.getByText("No indexed content yet")).toBeInTheDocument();
    expect(screen.getByText("No pack executions yet")).toBeInTheDocument();
    expect(screen.getByText("No current attention items")).toBeInTheDocument();
    expect(screen.getByText("Waiting for health data")).toBeInTheDocument();
  });

  it("marks incomplete successful responses as partial instead of healthy", async () => {
    api.fetchConnectorHealth.mockResolvedValue({
      org_id: "org-health",
      connectors: [{ ...connectorResponse.connectors[0], auth_mode: null }],
    });
    api.fetchRunHealth.mockResolvedValue({ org_id: "org-health", runs: [{ run_id: "incomplete-run", status: "complete", health_status: "healthy" }] });
    api.fetchContentHealth.mockResolvedValue({ ...contentResponse, indexed_by_source: [], pending_embeddings: 0, stale_chunks: 0, failed_refreshes: 0 });
    api.fetchPackHealth.mockResolvedValue({ run_id: "incomplete-run", packs: [{ ...packResponse.packs[0], detectors: [] }] });
    api.fetchAttentionHealth.mockResolvedValue({ org_id: "org-health", severity_order: ["critical", "high", "medium", "low"], items: [] });
    renderPage();

    await waitFor(() => expect(screen.getByTestId("panel-connectors")).toHaveAttribute("data-state", "partial"));
    expect(screen.getByTestId("panel-runs")).toHaveAttribute("data-state", "partial");
    expect(screen.getByTestId("panel-content")).toHaveAttribute("data-state", "partial");
    expect(screen.getByTestId("panel-packs")).toHaveAttribute("data-state", "partial");
    expect(screen.getAllByText("Partial data")).toHaveLength(4);
  });

  it("retries only the failed panel", async () => {
    api.fetchPackHealth.mockRejectedValueOnce(new Error("Pack read failed")).mockResolvedValueOnce(packResponse);
    renderPage();
    const retry = await screen.findByRole("button", { name: /retry pack health/i });
    retry.click();
    await waitFor(() => expect(api.fetchPackHealth).toHaveBeenCalledTimes(2));
    expect(api.fetchConnectorHealth).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Commercial Lending")).toBeInTheDocument();
  });

  // Internal scroll: the Connectors and Runs card lists scroll once they exceed
  // three cards, so the panels stay compact instead of growing with the list.
  function makeConnectors(n: number) {
    return {
      org_id: "org-health",
      connectors: Array.from({ length: n }, (_, i) => ({
        connector_id: `conn-${i}`,
        name: `Connector ${i}`,
        tier: "primary",
        connection_state: "connected",
        auth_mode: "oauth",
        last_successful_ingestion: "2026-07-13T09:45:00Z",
        checkpoint_position: `cursor-${i}`,
        checkpoint_captured_at: "2026-07-13T09:50:00Z",
        checkpoint_age_seconds: 600,
        last_error: null,
      })),
    };
  }

  function makeRuns(n: number) {
    return {
      org_id: "org-health",
      runs: Array.from({ length: n }, (_, i) => ({
        run_id: `run-${i}-000`,
        status: "complete",
        health_status: "healthy",
        degraded: false,
        started_at: "2026-07-13T09:00:00Z",
        updated_at: "2026-07-13T09:05:00Z",
        duration_seconds: 300,
        systems: ["servicenow"],
        system_count: 1,
        pack_id: "ncino",
        detectors_evaluated: 4,
        detectors_fired: 1,
        opportunities: 2,
        degraded_stages: [],
        stage_outcomes: [],
      })),
    };
  }

  // The card list is the space-y-3 container that directly holds the <article> cards.
  function cardListOf(panel: HTMLElement): HTMLElement | null {
    const article = panel.querySelector("article");
    return (article?.parentElement as HTMLElement) ?? null;
  }

  it("gives the Connectors card list an internal scrollbar only when there are more than three connectors", async () => {
    api.fetchConnectorHealth.mockResolvedValue(makeConnectors(4));
    const { unmount } = renderPage();
    await screen.findByText("Connector 0");
    expect(cardListOf(screen.getByTestId("panel-connectors"))?.className).toContain("overflow-y-auto");
    unmount();

    api.fetchConnectorHealth.mockResolvedValue(makeConnectors(3));
    renderPage();
    await screen.findByText("Connector 0");
    expect(cardListOf(screen.getByTestId("panel-connectors"))?.className).not.toContain("overflow-y-auto");
  });

  it("gives the Runs card list an internal scrollbar only when there are more than three runs", async () => {
    api.fetchRunHealth.mockResolvedValue(makeRuns(4));
    const { unmount } = renderPage();
    await screen.findByText("Run run-0-00");
    expect(cardListOf(screen.getByTestId("panel-runs"))?.className).toContain("overflow-y-auto");
    unmount();

    api.fetchRunHealth.mockResolvedValue(makeRuns(2));
    renderPage();
    await screen.findByText("Run run-0-00");
    expect(cardListOf(screen.getByTestId("panel-runs"))?.className).not.toContain("overflow-y-auto");
  });
});
