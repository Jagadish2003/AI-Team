/**
 * 2.0-C1 T5 (AT-830) — Surfacing.
 *
 * Sub-task: "run health shows pack state (active / disabled / rolled back, with
 * versions); findings display the pack version that produced them."
 *
 * This is the UI half of the story's **AC5** — *run health reflects pack state and
 * version accurately across all transitions* — so the run-health assertions walk the
 * transitions in turn (active → disabled → rolled back → both) rather than checking
 * one static shape.
 *
 * It also pins the reader-facing half of **AC2** and **AC3**: a finding produced by a
 * pack that is disabled or rolled back today still displays, still shows the version
 * that produced it, and is clearly labelled.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { renderWithCache } from "../test-utils/renderWithCache";
import type { OpportunityCandidate } from "../types/analystReview";
import type { PackHealthItem } from "../types/runHealth";

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
vi.mock("../context/RunContext", () => ({
  useRunContext: () => ({ runId: "run_test_001" }),
}));
vi.mock("../components/common/PageShell", () => ({
  default: ({
    title,
    description,
    actions,
    children,
  }: {
    title: string;
    description: string;
    actions?: React.ReactNode;
    children: React.ReactNode;
  }) => (
    <main>
      <h1>{title}</h1>
      <p>{description}</p>
      {actions}
      <div>{children}</div>
    </main>
  ),
}));

import OpportunityDetail, {
  PackProvenanceRow,
} from "../components/analyst_review/OpportunityDetail";
import {
  HIDDEN_PANELS,
  packLifecycleLabel,
} from "../pages/RunHealthDashboardPage";

// ── fixtures ─────────────────────────────────────────────────────────────────

// The other four panels are irrelevant here — seeded only so the page renders.
const emptyConnectors = { org_id: "org-a", generated_at: "2026-07-30T10:00:00Z", connectors: [] };
const emptyRuns = { org_id: "org-a", generated_at: "2026-07-30T10:00:00Z", runs: [] };
const emptyContent = {
  org_id: "org-a",
  generated_at: "2026-07-30T10:00:00Z",
  indexed_by_source: [],
  chunks_total: 0,
  chunks_embedded: 0,
  pending_embeddings: 0,
  stale_chunks: 0,
  pending_change_events: 0,
  failed_refreshes: 0,
  backfill: {
    active_model: "embed-v2",
    embedded_total: 0,
    on_active_model: 0,
    awaiting_backfill: 0,
    progress: 1,
    complete: true,
  },
  redaction_count: 0,
  skipped: [],
};
const emptyAttention = {
  org_id: "org-a",
  severity_order: ["critical", "high", "medium", "low"] as const,
  items: [],
};

function packRow(overrides: Partial<PackHealthItem> = {}): PackHealthItem {
  return {
    pack_id: "cloud_ops",
    pack_name: "Cloud Operations",
    pack_version: "1.2.0",
    detector_count: 2,
    detectors: ["cloud_ops_queue_ageing", "cloud_ops_alert_triage_toil"],
    executed_at: "2026-07-30T10:00:00Z",
    pack_state: "active",
    pinned_version: null,
    rolled_back: false,
    ...overrides,
  };
}

function packHealth(
  packs: PackHealthItem[],
  extra: Record<string, unknown> = {},
) {
  return { run_id: "run-abcdef123", packs, excluded_packs: [], ...extra };
}

// The health panels read from the shared data cache (so they survive navigation),
// which needs a provider ancestor. Each render gets a fresh one, so the cache never
// leaks between tests.
// Merge note: `dev` HID the packs panel from the dashboard grid (see
// HIDDEN_PANELS in RunHealthDashboardPage — kept intact so re-enabling it is a
// one-line change). These tests are about what the PANEL renders, not about
// whether the dashboard currently shows it, so they mount the panel directly.
// Rendering the whole dashboard would assert dev's layout decision, not this
// behaviour — and would simply time out waiting for a panel that is not there.
async function renderDashboard() {
  const data = await api.fetchPackHealth();
  return renderWithCache(
    <MemoryRouter initialEntries={["/run-health"]}>
      <HIDDEN_PANELS.PacksPanel
        resource={{ status: "success", data, error: null }}
        retry={() => {}}
        highlighted={false}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  auth.role = "owner";
  api.fetchConnectorHealth.mockResolvedValue(emptyConnectors);
  api.fetchRunHealth.mockResolvedValue(emptyRuns);
  api.fetchContentHealth.mockResolvedValue(emptyContent);
  api.fetchAttentionHealth.mockResolvedValue(emptyAttention);
  Element.prototype.scrollIntoView = vi.fn();
});

// ── The label helper: the two orthogonal facts ────────────────────────────────

describe("packLifecycleLabel", () => {
  it("reports an active, current-version pack", () => {
    const label = packLifecycleLabel(packRow());
    expect(label.stateLabel).toBe("Active");
    expect(label.versionLabel).toBe("Version 1.2.0");
    expect(label.rolledBack).toBe(false);
  });

  it("reports a disabled pack without treating it as a fault", () => {
    const label = packLifecycleLabel(packRow({ pack_state: "disabled" }));
    expect(label.stateLabel).toBe("Disabled");
    // Disabling is a deliberate customer choice — informational, never an error tone.
    expect(label.stateTone).toBe("warn");
    expect(label.stateTone).not.toBe("bad");
  });

  it("reports a rolled-back version distinctly from a current one", () => {
    const label = packLifecycleLabel(
      packRow({ pack_version: "1.1.0", pinned_version: "1.1.0", rolled_back: true }),
    );
    expect(label.versionLabel).toBe("Rolled back to 1.1.0");
    expect(label.rolledBack).toBe(true);
  });

  it("keeps state and version independent — a pack can be both", () => {
    // The two lifecycle dimensions never collapse into one word.
    const label = packLifecycleLabel(
      packRow({
        pack_state: "disabled",
        pack_version: "1.1.0",
        pinned_version: "1.1.0",
        rolled_back: true,
      }),
    );
    expect(label.stateLabel).toBe("Disabled");
    expect(label.versionLabel).toBe("Rolled back to 1.1.0");
  });

  it("infers a rollback from a pin alone, for an older response shape", () => {
    const label = packLifecycleLabel(
      packRow({ pinned_version: "1.1.0", rolled_back: undefined }),
    );
    expect(label.rolledBack).toBe(true);
  });

  it("defaults a missing state to Active rather than blank", () => {
    const label = packLifecycleLabel(packRow({ pack_state: undefined }));
    expect(label.stateLabel).toBe("Active");
  });

  it("says so when the executed version is unknown", () => {
    const label = packLifecycleLabel(packRow({ pack_version: null }));
    expect(label.versionLabel).toBe("Version unavailable");
    expect(label.versionTone).toBe("warn");
  });
});

// ── AC5: run health across the transitions ────────────────────────────────────

describe("Run health packs panel — pack state and version (AC5)", () => {
  it("shows an active pack with the version that executed", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth([packRow()]),
    );
    await renderDashboard();

    const row = await screen.findByTestId("pack-row-cloud_ops");
    expect(within(row).getByTestId("pack-state-cloud_ops")).toHaveTextContent("Active");
    expect(within(row).getByTestId("pack-version-cloud_ops")).toHaveTextContent(
      "Version 1.2.0",
    );
    expect(screen.queryByTestId("pack-disabled-note-cloud_ops")).toBeNull();
    expect(screen.queryByTestId("pack-rollback-note-cloud_ops")).toBeNull();
  });

  it("shows a DISABLED pack and says its output is kept as it executed", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth([packRow({ pack_state: "disabled" })]),
    );
    await renderDashboard();

    const row = await screen.findByTestId("pack-row-cloud_ops");
    expect(within(row).getByTestId("pack-state-cloud_ops")).toHaveTextContent("Disabled");
    expect(await screen.findByTestId("pack-disabled-note-cloud_ops")).toHaveTextContent(
      /will not run again/i,
    );
    // AC2's reader-facing half: the execution record is NOT presented as lost — the
    // version that ran, the detector count, and the detector list all still render.
    expect(within(row).getByTestId("pack-version-cloud_ops")).toHaveTextContent("1.2.0");
    expect(within(row).getByText("Detectors attempted")).toBeInTheDocument();
    expect(within(row).getByText("Detector list")).toBeInTheDocument();
    expect(within(row).getByText("Version executed")).toBeInTheDocument();
  });

  it("shows a ROLLED-BACK pack as a deliberate pin, not a downgrade", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth(
        [packRow({ pack_version: "1.1.0", pinned_version: "1.1.0", rolled_back: true })],
        { pinned_pack_versions: { cloud_ops: "1.1.0" } },
      ),
    );
    await renderDashboard();

    const row = await screen.findByTestId("pack-row-cloud_ops");
    expect(within(row).getByTestId("pack-version-cloud_ops")).toHaveTextContent(
      "Rolled back to 1.1.0",
    );
    expect(await screen.findByTestId("pack-rollback-note-cloud_ops")).toHaveTextContent(
      /deliberate rollback/i,
    );
    // Still active — a rollback is not a disable.
    expect(within(row).getByTestId("pack-state-cloud_ops")).toHaveTextContent("Active");
  });

  it("shows a pack that is BOTH disabled and rolled back", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth([
        packRow({
          pack_state: "disabled",
          pack_version: "1.1.0",
          pinned_version: "1.1.0",
          rolled_back: true,
        }),
      ]),
    );
    await renderDashboard();

    expect(await screen.findByTestId("pack-disabled-note-cloud_ops")).toBeInTheDocument();
    expect(screen.getByTestId("pack-rollback-note-cloud_ops")).toBeInTheDocument();
  });

  it("reports each pack's state independently in a multi-pack run", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth([
        packRow(),
        packRow({
          pack_id: "security_ops",
          pack_name: "Security Operations",
          pack_state: "disabled",
          pack_version: "1.1.0",
          pinned_version: "1.1.0",
          rolled_back: true,
          detectors: ["security_ops_sir_triage_toil"],
          detector_count: 1,
        }),
      ]),
    );
    await renderDashboard();

    expect(await screen.findByTestId("pack-state-cloud_ops")).toHaveTextContent("Active");
    expect(screen.getByTestId("pack-state-security_ops")).toHaveTextContent("Disabled");
    expect(screen.getByTestId("pack-version-security_ops")).toHaveTextContent(
      "Rolled back to 1.1.0",
    );
    // One pack's lifecycle must not bleed into the other's row.
    expect(screen.queryByTestId("pack-disabled-note-cloud_ops")).toBeNull();
    expect(screen.queryByTestId("pack-rollback-note-cloud_ops")).toBeNull();
  });

  it("names packs that were selected but excluded because they are disabled", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth([packRow()], {
        excluded_packs: [
          { packId: "security_ops", state: "disabled", reason: "pack_disabled" },
        ],
      }),
    );
    await renderDashboard();

    const excluded = await screen.findByTestId("packs-excluded");
    expect(within(excluded).getByTestId("pack-excluded-security_ops")).toHaveTextContent(
      "security_ops",
    );
    // The reason is stated — an analyst seeing one pack where two were selected must
    // not be left to infer why.
    expect(excluded).toHaveTextContent(/disabled for this organisation/i);
    expect(excluded).toHaveTextContent(/earlier runs are unaffected/i);
  });

  it("explains an empty panel caused by every pack being disabled", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth([], {
        excluded_packs: [
          { packId: "cloud_ops", state: "disabled", reason: "pack_disabled" },
        ],
      }),
    );
    await renderDashboard();

    // The generic "no runs yet" message would be actively misleading here.
    const panel = await screen.findByTestId("panel-packs");
    expect(panel).toHaveTextContent(/No pack executed for this run/i);
    expect(panel).toHaveTextContent(/cloud_ops/);
    expect(panel).not.toHaveTextContent(/No pack executions yet/i);
  });

  it("keeps the original empty message when nothing was excluded", async () => {
    api.fetchPackHealth.mockResolvedValue(
      packHealth([]),
    );
    await renderDashboard();

    const panel = await screen.findByTestId("panel-packs");
    expect(panel).toHaveTextContent(/No pack executions yet/i);
  });

  it("does not report a disabled pack as an unhealthy panel", async () => {
    // Disabling is intentional configuration, so it must not make the dashboard
    // look broken.
    api.fetchPackHealth.mockResolvedValue(
      packHealth([packRow({ pack_state: "disabled" })]),
    );
    await renderDashboard();

    await waitFor(() =>
      expect(screen.getByTestId("panel-packs")).toHaveAttribute("data-state", "healthy"),
    );
  });

  it("renders without the lifecycle fields at all (pre-2.0-C1 response)", async () => {
    api.fetchPackHealth.mockResolvedValue({
      run_id: "run-old",
      packs: [
        {
          pack_id: "ncino",
          pack_name: "Commercial Lending",
          pack_version: "18.2.0",
          detector_count: 1,
          detectors: ["loan_workflow"],
        },
      ],
    });
    await renderDashboard();

    const row = await screen.findByTestId("pack-row-ncino");
    expect(within(row).getByTestId("pack-state-ncino")).toHaveTextContent("Active");
    expect(within(row).getByTestId("pack-version-ncino")).toHaveTextContent(
      "Version 18.2.0",
    );
    expect(screen.queryByTestId("packs-excluded")).toBeNull();
  });
});

// ── Findings display the pack version that produced them ──────────────────────

function candidate(overrides: Partial<OpportunityCandidate> = {}): OpportunityCandidate {
  return {
    id: "opp-1",
    title: "Recurring resolution loop",
    category: "Operations",
    tier: "T1" as OpportunityCandidate["tier"],
    impact: 8,
    effort: 3,
    confidence: "HIGH" as OpportunityCandidate["confidence"],
    aiRationale: "Incidents recur on the same resolution signature.",
    evidenceIds: ["ev-1"],
    decision: "UNREVIEWED" as OpportunityCandidate["decision"],
    override: {
      isLocked: false,
      rationaleOverride: "",
      overrideReason: "",
      updatedAt: null,
    },
    packId: "cloud_ops",
    packVersion: "1.2.0",
    ...overrides,
  };
}

describe("PackProvenanceRow — findings show the pack version that produced them", () => {
  it("shows the producing pack and its version", () => {
    render(<PackProvenanceRow opp={candidate()} />);
    expect(screen.getByTestId("pack-provenance-id")).toHaveTextContent("cloud_ops");
    expect(screen.getByTestId("pack-provenance-version")).toHaveTextContent("v1.2.0");
  });

  it("labels a finding whose pack is now disabled, without hiding it", () => {
    render(
      <PackProvenanceRow
        opp={candidate({
          packState: "disabled",
          packStateLabel: "Produced by a now-disabled pack",
        })}
      />,
    );
    // AC2: retrievable AND correctly labelled.
    expect(screen.getByTestId("pack-provenance-disabled")).toHaveTextContent(
      /now-disabled pack/i,
    );
    expect(screen.getByTestId("pack-provenance-version")).toHaveTextContent("v1.2.0");
  });

  it("falls back to a default label when the backend sent none", () => {
    render(<PackProvenanceRow opp={candidate({ packState: "disabled" })} />);
    expect(screen.getByTestId("pack-provenance-disabled")).toHaveTextContent(
      /now-disabled pack/i,
    );
  });

  it("adds no disabled label for an active pack", () => {
    render(<PackProvenanceRow opp={candidate({ packState: "active" })} />);
    expect(screen.queryByTestId("pack-provenance-disabled")).toBeNull();
  });

  it("keeps the version that PRODUCED the finding, not the pack's current one", () => {
    // AC3: rolling the pack back to 1.1.0 must not rewrite a 1.2.0 finding.
    render(<PackProvenanceRow opp={candidate({ packVersion: "1.2.0" })} />);
    expect(screen.getByTestId("pack-provenance-version")).toHaveTextContent("v1.2.0");
    expect(screen.getByTestId("pack-provenance-version")).not.toHaveTextContent("1.1.0");
  });

  it("renders nothing for a finding with no pack stamp", () => {
    // Runs materialised before R191-P1 T3 carry no stamp — never invent one.
    const { container } = render(
      <PackProvenanceRow opp={candidate({ packId: undefined, packVersion: undefined })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the version alone when only the version is stamped", () => {
    render(<PackProvenanceRow opp={candidate({ packId: undefined })} />);
    expect(screen.getByTestId("pack-provenance-version")).toHaveTextContent("v1.2.0");
    expect(screen.queryByTestId("pack-provenance-id")).toBeNull();
  });
});

describe("OpportunityDetail wires the provenance row into the finding view", () => {
  it("shows the pack version inside the opportunity detail", async () => {
    render(
      <MemoryRouter>
        <OpportunityDetail opp={candidate()} audit={[]} hideTitleBar />
      </MemoryRouter>,
    );
    const provenance = await screen.findByTestId("pack-provenance");
    expect(provenance).toHaveTextContent("cloud_ops");
    expect(provenance).toHaveTextContent("v1.2.0");
  });

  it("labels a now-disabled pack's finding in the detail view", async () => {
    render(
      <MemoryRouter>
        <OpportunityDetail
          opp={candidate({
            packState: "disabled",
            packStateLabel: "Produced by a now-disabled pack",
          })}
          audit={[]}
        />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("pack-provenance-disabled")).toBeInTheDocument();
    // AC2: the finding itself is still fully rendered — the label sits alongside it,
    // it does not replace or suppress it.
    expect(screen.getByText("Recurring resolution loop")).toBeInTheDocument();
    expect(screen.getByTestId("pack-provenance-version")).toHaveTextContent("v1.2.0");
  });
});
