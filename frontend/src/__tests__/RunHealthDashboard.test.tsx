import { screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { renderWithCache } from "../test-utils/renderWithCache";
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

// The health panels read from the shared data cache (so they survive
// navigation), which needs a DataCacheProvider ancestor. Each render gets a
// fresh provider, so the cache never leaks between tests.
function renderPage(path = "/run-health") {
  return renderWithCache(<MemoryRouter initialEntries={[path]}><RunHealthDashboardPage /></MemoryRouter>);
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
  it("renders the visible areas from live response data with direct attention links", async () => {
    renderPage("/run-health?panel=connectors&connector=salesforce");

    expect(await screen.findByText("Salesforce authentication expired")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Connectors" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Runs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Attention Strip" })).toBeInTheDocument();

    expect(screen.getByText("ServiceNow")).toBeInTheDocument();
    expect(screen.getByText(/Recommendation generation timed out/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view connectors details/i })).toHaveAttribute(
      "href",
      "/run-health?panel=connectors&connector=salesforce",
    );
    // Selected panel is highlighted with the app accent ring (theme-aware).
    expect(screen.getByTestId("panel-connectors")).toHaveClass("ring-accent/30");
  });

  // The Content-and-Freshness and Packs panels are hidden from the dashboard, but
  // their health reads still run so both keep feeding the tenant summary and the
  // Attention Strip — hiding a card must not make a degraded tenant read healthy.
  it("hides the Content and Packs panels while still performing their health reads", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "Connectors" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Content and Freshness" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Packs" })).toBeNull();
    expect(screen.queryByTestId("panel-content")).toBeNull();
    expect(screen.queryByTestId("panel-packs")).toBeNull();

    expect(api.fetchContentHealth).toHaveBeenCalledTimes(1);
    expect(api.fetchPackHealth).toHaveBeenCalledTimes(1);
  });

  it("still degrades the tenant summary from a hidden panel's signal", async () => {
    // No connector, run or attention problem — the ONLY issue is stale content,
    // reported by a panel that is no longer rendered.
    api.fetchConnectorHealth.mockResolvedValue(makeConnectors(1));
    api.fetchRunHealth.mockResolvedValue(makeRuns(1));
    api.fetchContentHealth.mockResolvedValue({ ...contentResponse, pending_embeddings: 0, stale_chunks: 7, failed_refreshes: 0 });
    api.fetchAttentionHealth.mockResolvedValue({ org_id: "org-health", severity_order: ["critical", "high", "medium", "low"], items: [] });
    renderPage();

    expect(await screen.findByText("Tenant health is degraded")).toBeInTheDocument();
    expect(screen.queryByTestId("panel-content")).toBeNull();
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

  it("shows connector checkpoint support details without exposing raw JSON", async () => {
    api.fetchConnectorHealth.mockResolvedValue({
      org_id: "org-health",
      connectors: [{
        ...connectorResponse.connectors[0],
        connector_id: "azure_events",
        name: "Azure Events",
        auth_mode: "static",
        checkpoint_position: JSON.stringify({
          activity_log: { "5c43a0b5-1ca6-4d98-a58b-f1dcb13f1307": "2026-07-28T15:49:24.660908Z" },
          alerts: {},
          service_health: {},
        }),
        checkpoint_captured_at: "2026-07-29T13:23:00Z",
      }],
    });
    renderPage();

    const panel = await screen.findByTestId("panel-connectors");
    expect(within(panel).getByText("Activity Log")).toBeInTheDocument();
    expect(within(panel).getByText(/Continues after/i)).toBeInTheDocument();
    expect(panel).not.toHaveTextContent("No checkpoint saved yet");
    expect(panel).not.toHaveTextContent("Alerts");
    expect(panel).not.toHaveTextContent("Service Health");
    expect(panel).not.toHaveTextContent("activity_log");
    expect(panel).not.toHaveTextContent("5c43a0b5");
    expect(panel).not.toHaveTextContent("service_health");
  });

  it("turns run support details into customer-readable issues and hides normal INFO events", async () => {
    api.fetchRunHealth.mockResolvedValue({
      org_id: "org-health",
      runs: [{
        ...runResponse.runs[1],
        run_id: "run-salesforce-auth",
        degraded_stages: [{
          stage: "salesforce",
          reason: 'HTTP 401: [{"message":"Session expired or invalid","errorCode":"INVALID_SESSION_ID"}] Query: SELECT COUNT(Id) FROM Case WHERE CreatedDate = LAST_N_DAYS:90',
        }],
        stage_outcomes: [
          { stage: "queued", level: "INFO", message: "Queued" },
          { stage: "connect", level: "INFO", message: "Connected" },
          { stage: "ingest", level: "INFO", message: "Ingested" },
        ],
      }],
    });
    renderPage();

    const panel = await screen.findByTestId("panel-runs");
    expect(within(panel).getByText("Salesforce session expired or is no longer valid.")).toBeInTheDocument();
    expect(within(panel).getByText("Reconnect Salesforce, then rerun discovery.")).toBeInTheDocument();
    expect(panel).not.toHaveTextContent("INVALID_SESSION_ID");
    expect(panel).not.toHaveTextContent("SELECT COUNT");
    expect(panel).not.toHaveTextContent("Queued: INFO");
    expect(panel).not.toHaveTextContent("Connect: INFO");
    expect(panel).not.toHaveTextContent("Ingest: INFO");
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

    // The content panel is hidden, so the failure surfaces through the tenant
    // summary rather than a panel-level error. It must never read as healthy.
    expect(await screen.findByText("Health partially unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Tenant health is healthy")).toBeNull();
    expect(screen.queryByText("Indexed chunks")).toBeNull();
  });

  // Removed with the Content panel's visibility: it asserted the long-identifier
  // table/wrapping layout INSIDE that panel, which no longer renders. The panel
  // component itself is retained unchanged, so re-showing it restores that layout.

  it("shows independent loading states for every health read", () => {
    const pending = new Promise(() => undefined);
    api.fetchConnectorHealth.mockReturnValue(pending);
    api.fetchRunHealth.mockReturnValue(pending);
    api.fetchContentHealth.mockReturnValue(pending);
    api.fetchPackHealth.mockReturnValue(pending);
    api.fetchAttentionHealth.mockReturnValue(pending);
    renderPage();

    // Each visible panel shows a layout-shaped skeleton (labelled for a11y) rather
    // than a spinner + text, so the rows fill the reserved space with no shift.
    // Content and pack skeletons are absent: those panels are hidden.
    expect(screen.getByLabelText("Loading connector health")).toBeInTheDocument();
    expect(screen.getByLabelText("Loading run health")).toBeInTheDocument();
    expect(screen.getByLabelText("Loading attention items")).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading content health")).toBeNull();
    expect(screen.queryByLabelText("Loading pack health")).toBeNull();
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
    expect(screen.getByText("No current attention items")).toBeInTheDocument();
    expect(screen.getByText("Waiting for health data")).toBeInTheDocument();
    // Hidden panels contribute no empty state of their own.
    expect(screen.queryByText("No indexed content yet")).toBeNull();
    expect(screen.queryByText("No pack executions yet")).toBeNull();
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
    // Only the two visible panels report a partial badge now.
    expect(screen.getAllByText("Partial data")).toHaveLength(2);
  });

  // Retargeted from the (now hidden) Packs panel to a visible one: the behaviour
  // under test is that a retry refetches ONLY its own panel's read.
  it("retries only the failed panel", async () => {
    api.fetchRunHealth.mockRejectedValueOnce(new Error("Run read failed")).mockResolvedValueOnce(runResponse);
    renderPage();
    const retry = await screen.findByRole("button", { name: /retry run health/i });
    retry.click();
    await waitFor(() => expect(api.fetchRunHealth).toHaveBeenCalledTimes(2));
    expect(api.fetchConnectorHealth).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Run run-healthy-0001")).toBeInTheDocument();
  });

  // R191-P1 T5 (AC5) — "one Packs row per pack for a multi-pack run" — was removed
  // with the Packs panel's visibility, since it asserted rows in a panel that no
  // longer renders. The per-pack backend shape is still covered by the backend
  // pack-health contract tests; the panel component is retained unchanged.

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
    await screen.findByText("Run run-0-000");
    expect(cardListOf(screen.getByTestId("panel-runs"))?.className).toContain("overflow-y-auto");
    unmount();

    api.fetchRunHealth.mockResolvedValue(makeRuns(2));
    renderPage();
    await screen.findByText("Run run-0-000");
    expect(cardListOf(screen.getByTestId("panel-runs"))?.className).not.toContain("overflow-y-auto");
  });

  // ── Refresh ────────────────────────────────────────────────────────────────

  it("refetches every health read when Refresh is clicked", async () => {
    renderPage();
    await screen.findByText("ServiceNow");
    expect(api.fetchConnectorHealth).toHaveBeenCalledTimes(1);

    screen.getByTestId("refresh-health").click();

    await waitFor(() => expect(api.fetchConnectorHealth).toHaveBeenCalledTimes(2));
    expect(api.fetchRunHealth).toHaveBeenCalledTimes(2);
    expect(api.fetchContentHealth).toHaveBeenCalledTimes(2);
    expect(api.fetchPackHealth).toHaveBeenCalledTimes(2);
    expect(api.fetchAttentionHealth).toHaveBeenCalledTimes(2);
  });

  it("shows fresh data on the panels after a refresh", async () => {
    renderPage();
    await screen.findByText("ServiceNow");
    expect(screen.queryByText("Jira")).toBeNull();

    // Second read returns a different estate — the refresh must render it.
    api.fetchConnectorHealth.mockResolvedValue({
      org_id: "org-health",
      connectors: [{ ...connectorResponse.connectors[0], connector_id: "jira", name: "Jira" }],
    });
    screen.getByTestId("refresh-health").click();

    expect(await screen.findByText("Jira")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("ServiceNow")).toBeNull());
  });

  // The cache keeps the previous reference when a refetch returns identical data,
  // so an unchanged refresh emits nothing. The button must still confirm it ran —
  // otherwise it reads as broken on a healthy, unchanging tenant.
  it("reports completion even when the refreshed data is unchanged", async () => {
    renderPage();
    await screen.findByText("ServiceNow");
    const before = screen.getByTestId("health-updated-at").textContent;

    screen.getByTestId("refresh-health").click();

    await waitFor(() => expect(api.fetchConnectorHealth).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      const updated = screen.getByTestId("health-updated-at").textContent ?? "";
      expect(updated).toMatch(/^Updated /);
      expect(updated).not.toBe(before);
    });
  });

  it("disables the Refresh button and shows a busy state while reads are in flight", async () => {
    renderPage();
    await screen.findByText("ServiceNow");

    let release: (value: unknown) => void = () => undefined;
    api.fetchConnectorHealth.mockReturnValue(new Promise((resolve) => { release = resolve; }));

    const button = screen.getByTestId("refresh-health");
    button.click();

    await waitFor(() => expect(button).toBeDisabled());
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("Refreshing…");

    release(connectorResponse);
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(button).toHaveTextContent("Refresh");
  });

  it("re-enables Refresh after a failed read rather than staying stuck busy", async () => {
    renderPage();
    await screen.findByText("ServiceNow");

    api.fetchConnectorHealth.mockRejectedValue(new Error("Connector read failed"));
    const button = screen.getByTestId("refresh-health");
    button.click();

    await waitFor(() => expect(button).not.toBeDisabled());
    expect(button).toHaveTextContent("Refresh");
  });

  // ── Connector ordering ─────────────────────────────────────────────────────

  it("orders healthy connectors above disconnected ones", async () => {
    api.fetchConnectorHealth.mockResolvedValue({
      org_id: "org-health",
      connectors: [
        // Deliberately worst-first from the backend, to prove the UI reorders.
        { ...connectorResponse.connectors[0], connector_id: "sap", name: "SAP", connection_state: "disconnected" },
        { ...connectorResponse.connectors[0], connector_id: "slack", name: "Slack", connection_state: "needs_auth" },
        { ...connectorResponse.connectors[0], connector_id: "aws_events", name: "AWS Events", connection_state: "connected" },
        { ...connectorResponse.connectors[0], connector_id: "jira", name: "Jira", connection_state: "connected", last_error: "Rate limited" },
        { ...connectorResponse.connectors[0], connector_id: "azure_events", name: "Azure Events", connection_state: "connected" },
      ],
    });
    renderPage();

    const panel = await screen.findByTestId("panel-connectors");
    const names = within(panel).getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(names).toEqual([
      // Healthy (alphabetical within the tier)…
      "AWS Events",
      "Azure Events",
      // …then warning (connected but reporting an error)…
      "Jira",
      // …then disconnected/needs-auth last.
      "SAP",
      "Slack",
    ]);
  });

  it("keeps the panel state derived from health, not from display order", async () => {
    api.fetchConnectorHealth.mockResolvedValue({
      org_id: "org-health",
      connectors: [
        { ...connectorResponse.connectors[0], connector_id: "aws_events", name: "AWS Events", connection_state: "connected" },
        { ...connectorResponse.connectors[0], connector_id: "sap", name: "SAP", connection_state: "disconnected" },
      ],
    });
    renderPage();

    const panel = await screen.findByTestId("panel-connectors");
    // A disconnected connector sank to the bottom but must still degrade the panel.
    expect(panel).toHaveAttribute("data-state", "degraded");
    expect(within(panel).getByText("Need attention").parentElement).toHaveTextContent("1");
  });
});
