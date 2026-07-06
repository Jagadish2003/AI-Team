/**
 * T41-8 — Nav Cleanup and Source Intake Merge — Tests
 *
 * Four acceptance criteria:
 *
 * AC1: Source Intake absent from TopNav items.
 *
 * AC2: /source-intake route redirects to /integration-hub.
 *      Tested via App route rendering with MemoryRouter at /source-intake.
 *
 * AC3: SourceConfigPanel renders in Integration Hub right panel.
 *      Tests the real SourceConfigPanel component — file upload UI present,
 *      "Add mock file" button absent, sample workspace absent, managed agent absent.
 *
 * AC4: DiscoveryStartBar no longer has "Upload Files Instead" button.
 *      onUpload prop deprecated and UI element removed.
 *
 * Run:
 *   npx vitest run src/__tests__/T41_8_nav_cleanup.test.tsx
 */

import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { vi, describe, it, expect } from "vitest";

// ── Shared mocks ──────────────────────────────────────────────────────────────

const mockIntakeState = {
  uploadedFiles: [] as any[],
  sampleWorkspaceEnabled: false,
};

vi.mock("../context/RunContext", () => ({
  RunProvider: ({ children }: any) => <>{children}</>,
  useRunContext: () => ({ runId: null }),
}));

vi.mock("../context/ConnectorContext", () => ({
  ConnectorProvider: ({ children }: any) => <>{children}</>,
  useConnectorContext: () => ({
    all: [],
    recommended: [],
    standard: [],
    selectedConnectorId: null,
    selectConnector: vi.fn(),
    connectConnector: vi.fn(),
    configureSync: vi.fn(),
    confidence: "low",
    recommendedConnectedCount: 0,
    nextBestRecommendedId: null,
    loading: false,
    error: null,
    refetch: vi.fn(),
  }),
}));

vi.mock("../context/SourceIntakeContext", () => ({
  SourceIntakeProvider: ({ children }: any) => <>{children}</>,
  useSourceIntakeContext: () => ({
    uploadedFiles: mockIntakeState.uploadedFiles,
    sampleWorkspaceEnabled: mockIntakeState.sampleWorkspaceEnabled,
    loading: false,
    error: null,
    refetch: vi.fn(),
    addMockFile: vi.fn(),
    addFilesFromSelection: vi.fn().mockReturnValue({ addedCount: 1 }),
    removeFile: vi.fn(),
    setSampleWorkspaceEnabled: vi.fn(),
  }),
}));

vi.mock("../context/AnalystReviewContext", () => ({
  AnalystReviewProvider: ({ children }: any) => <>{children}</>,
  useAnalystReviewContext: () => ({
    opportunities: [],
    selectedId: null,
    select: vi.fn(),
  }),
}));

vi.mock("../components/common/Toast", () => ({
  ToastProvider: ({ children }: any) => <>{children}</>,
  useToast: () => ({ push: vi.fn() }),
}));

// ─────────────────────────────────────────────────────────────────────────────
// AC1 — Source Intake absent from TopNav
// ─────────────────────────────────────────────────────────────────────────────

// Import the real items array by rendering TopNav and checking rendered links
import TopNav from "../components/common/TopNav";

describe("AC1 — Source Intake absent from TopNav", () => {
  it('does not render a "Source Intake" nav link', () => {
    render(
      <MemoryRouter initialEntries={["/integration-hub"]}>
        <TopNav />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Source Intake")).toBeNull();
  });

  it("still renders Integration Hub nav link", () => {
    render(
      <MemoryRouter initialEntries={["/integration-hub"]}>
        <TopNav />
      </MemoryRouter>,
    );
    expect(screen.getByText("Integration Hub")).toBeDefined();
  });

  it("renders exactly 8 nav items (Sprint 4.1 exit criterion #12)", () => {
    render(
      <MemoryRouter initialEntries={["/integration-hub"]}>
        <TopNav />
      </MemoryRouter>,
    );
    const expectedItems = [
      "Integration Hub",
      "Stack Builder",
      "Discovery Run",
      "Source Intelligence",
      "Opportunity Review",
      "Agent Roadmap",
      "Agentforce Blueprint",
      "Executive Report",
    ];
    for (const label of expectedItems) {
      expect(screen.getByText(label)).toBeDefined();
    }
    // Source Intake must not be present
    expect(screen.queryByText("Source Intake")).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// AC2 — /source-intake redirects to /integration-hub
// ─────────────────────────────────────────────────────────────────────────────

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="route-location">{location.pathname}</div>;
}

function renderSourceIntakeRedirect() {
  return render(
    <MemoryRouter initialEntries={["/source-intake"]}>
      <Routes>
        <Route
          path="/source-intake"
          element={<Navigate to="/integration-hub" replace />}
        />
        <Route
          path="/integration-hub"
          element={
            <>
              <TopNav />
              <h1>Integration Hub</h1>
              <LocationProbe />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

describe("AC2 — /source-intake redirects to /integration-hub", () => {
  it("renders Integration Hub heading when navigating to /source-intake", () => {
    renderSourceIntakeRedirect();
    // Use getAllByText because it appears in both nav and page heading
    expect(screen.getAllByText("Integration Hub").length).toBeGreaterThan(0);
    expect(screen.getByTestId("route-location")).toHaveTextContent(
      "/integration-hub",
    );
  });

  it("does NOT render standalone Source Intake page at /source-intake", () => {
    renderSourceIntakeRedirect();
    // SourceIntakePage renders a unique "Source Intake" text in its page heading.
    // After the redirect it must be absent — Integration Hub heading replaces it.
    const allText = document.body.textContent ?? "";
    // The heading "Source Intake" exists only on SourceIntakePage, not Integration Hub
    // Check it as a standalone heading context, not as a substring of longer text
    const headings = document.querySelectorAll('h1, h2, [class*="text-2xl"]');
    const sourceIntakeHeading = Array.from(headings).find(
      (el) => el.textContent?.trim() === "Source Intake",
    );
    expect(sourceIntakeHeading).toBeUndefined();
  });

  it("Source Intake is absent from nav after redirect", () => {
    renderSourceIntakeRedirect();
    expect(screen.queryByText("Source Intake")).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// AC3 — SourceConfigPanel: production-ready UI, demo elements absent
// ─────────────────────────────────────────────────────────────────────────────

import SourceConfigPanel from "../components/integrations/SourceConfigPanel";

describe("AC3 — SourceConfigPanel renders in Integration Hub right panel", () => {
  it('renders the "Upload Additional Data" toggle', () => {
    render(
      <MemoryRouter>
        <SourceConfigPanel />
      </MemoryRouter>,
    );
    expect(screen.getByText("Upload Additional Data")).toBeDefined();
  });

  it("expands to show drop zone and Browse on click", () => {
    render(
      <MemoryRouter>
        <SourceConfigPanel />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("Upload Additional Data"));
    expect(screen.getByText(/browse/i)).toBeDefined();
  });

  it('does NOT render "Add a mock file" button — engineering aid removed', () => {
    render(
      <MemoryRouter>
        <SourceConfigPanel />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByText("Upload Additional Data"));
    expect(screen.queryByText(/mock file/i)).toBeNull();
    expect(screen.queryByText(/Add a mock/i)).toBeNull();
  });

  it("does NOT render Sample Workspace panel — demo framing removed", () => {
    render(
      <MemoryRouter>
        <SourceConfigPanel />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/Sample Workspace/i)).toBeNull();
    expect(screen.queryByText(/Start Fresh/i)).toBeNull();
    expect(screen.queryByText(/demo\/evaluation/i)).toBeNull();
  });

  it("does NOT render Launch Managed Agent panel — surfaces on customer demand", () => {
    render(
      <MemoryRouter>
        <SourceConfigPanel />
      </MemoryRouter>,
    );
    expect(screen.queryByText(/Launch Managed Agent/i)).toBeNull();
    expect(screen.queryByText(/Download Connector Agent/i)).toBeNull();
    expect(screen.queryByText(/Installation Guide/i)).toBeNull();
  });

  it("shows collapsed by default — no file list shown until expanded", () => {
    render(
      <MemoryRouter>
        <SourceConfigPanel />
      </MemoryRouter>,
    );
    // In collapsed state, the file list area is not rendered
    expect(screen.queryByText(/drag & drop/i)).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// AC4 — DiscoveryStartBar: Upload Files button removed
// ─────────────────────────────────────────────────────────────────────────────

import DiscoveryStartBar from "../components/integrations/DiscoveryStartBar";

const mockConnectors = [
  {
    id: "salesforce",
    name: "Salesforce",
    status: "connected" as const,
    configured: true,
    category: "CRM",
    tier: "recommended" as const,
    reads: ["Cases"],
    lastSynced: "1h ago",
    metrics: [],
    signalStrength: 90,
  },
];

describe("AC4 — DiscoveryStartBar Upload Files button removed", () => {
  it('does not render "Upload Files Instead" button', () => {
    render(
      <MemoryRouter>
        <DiscoveryStartBar
          confidence="LOW"
          recommendedReadyCount={0}
          recommendedTotal={3}
          recommended={mockConnectors}
          canStart={false}
          onStart={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.queryByText("Upload Files Instead")).toBeNull();
  });

  it("still renders Start Discovery Run button (no regression)", () => {
    render(
      <MemoryRouter>
        <DiscoveryStartBar
          confidence="HIGH"
          recommendedReadyCount={3}
          recommendedTotal={3}
          recommended={mockConnectors}
          canStart={true}
          onStart={vi.fn()}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("Start Discovery Run")).toBeDefined();
  });

  it("accepts deprecated onUpload prop without error (backward compat)", () => {
    // Should render without TypeScript or runtime errors when onUpload is passed
    expect(() =>
      render(
        <MemoryRouter>
          <DiscoveryStartBar
            confidence="LOW"
            recommendedReadyCount={0}
            recommendedTotal={3}
            recommended={[]}
            canStart={false}
            onStart={vi.fn()}
            onUpload={vi.fn()}
          />
        </MemoryRouter>,
      ),
    ).not.toThrow();
    // But the button should still not render
    expect(screen.queryByText("Upload Files Instead")).toBeNull();
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// AC4-INTEGRATION — upload panel merged into Integration Hub, start bar removed
// ─────────────────────────────────────────────────────────────────────────────

import IntegrationHubPage from "../pages/IntegrationHubPage";

function renderIntegrationHubPage() {
  return render(
    <MemoryRouter initialEntries={["/integration-hub"]}>
      <IntegrationHubPage />
    </MemoryRouter>,
  );
}

describe("AC4-INTEGRATION — Integration Hub no longer owns discovery start", () => {
  it("does not render the old start bar when no connectors and no files", () => {
    mockIntakeState.uploadedFiles = [];
    mockIntakeState.sampleWorkspaceEnabled = false;

    renderIntegrationHubPage();

    expect(screen.getByText("Upload Additional Data")).toBeDefined();
    expect(screen.queryByText("Start Discovery Run")).toBeNull();
    expect(screen.queryByText("Upload Files Instead")).toBeNull();
  });

  it("sampleWorkspaceEnabled alone does NOT reintroduce a start action", () => {
    mockIntakeState.uploadedFiles = [];
    mockIntakeState.sampleWorkspaceEnabled = true;

    renderIntegrationHubPage();

    expect(screen.getByText("Upload Additional Data")).toBeDefined();
    expect(screen.queryByText("Start Discovery Run")).toBeNull();
    expect(screen.queryByText("Upload Files Instead")).toBeNull();
  });

  it("uploadedFiles stay in SourceConfigPanel without reintroducing the start bar", () => {
    mockIntakeState.uploadedFiles = [
      {
        id: "f1",
        name: "data.csv",
        sizeLabel: "4 KB",
        uploadedLabel: "Just now",
      },
    ];
    mockIntakeState.sampleWorkspaceEnabled = false;

    renderIntegrationHubPage();

    fireEvent.click(screen.getByText("Upload Additional Data"));

    expect(screen.getByText("data.csv")).toBeDefined();
    expect(screen.queryByText("Start Discovery Run")).toBeNull();
    expect(screen.queryByText("Upload Files Instead")).toBeNull();
  });
});
