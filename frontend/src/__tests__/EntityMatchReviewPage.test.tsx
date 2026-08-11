/**
 * EntityMatchReviewPage (2.0-B2 T3) tests.
 *
 * The review surface exists so a person can answer an identity question the
 * platform deliberately refuses to answer itself. These tests pin the parts of
 * that promise the UI owns:
 *
 *   - a proposed match renders WITH the evidence behind it (both entities, their
 *     source systems and records, the reason, and the corroborating relationship)
 *     — a reviewer must be able to decide from the card alone;
 *   - confirm and reject both reach the API and refresh the queue;
 *   - a decided proposal shows who decided it and that it will not be re-proposed,
 *     with no decision buttons;
 *   - the screen never claims to have merged anything;
 *   - a viewer never mounts the content.
 *
 * The API boundary (api/entityMatchProposalsApi) and the auth/toast contexts are
 * mocked — no backend is touched.
 *
 * Run: npx vitest run src/__tests__/EntityMatchReviewPage.test.tsx
 */
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import type {
  EntityMatchProposal,
  ProposalListResponse,
} from "../api/entityMatchProposalsApi";

const h = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockDecide: vi.fn(),
  mockScan: vi.fn(),
  mockPush: vi.fn(),
  role: { current: "analyst" as "owner" | "analyst" | "viewer" },
}));

vi.mock("../api/entityMatchProposalsApi", () => ({
  fetchEntityMatchProposals: (...a: unknown[]) => h.mockList(...a),
  decideEntityMatchProposal: (...a: unknown[]) => h.mockDecide(...a),
  scanEntityMatchProposals: (...a: unknown[]) => h.mockScan(...a),
}));

vi.mock("../components/common/Toast", () => ({
  useToast: () => ({ push: h.mockPush }),
}));

vi.mock("../components/common/TopNav", () => ({
  default: () => <nav data-testid="top-nav" />,
}));

vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({ user: { role: h.role.current } }),
  useAuth: () => ({ user: { role: h.role.current } }),
}));

import EntityMatchReviewPage from "../pages/EntityMatchReviewPage";

const PENDING: EntityMatchProposal = {
  org_id: "default",
  proposal_id: "emp_abc123",
  entity_type: "system",
  left_entity_id: "e1",
  right_entity_id: "e2",
  tier: "name_similarity",
  confidence: 0.7,
  status: "pending",
  evidence: {
    subject: {
      entity_id: "e1",
      display_name: "Billing",
      canonical_name: "billing",
      entity_type: "system",
      source_system: "servicenow",
      source_record_id: "sn-2",
    },
    target: {
      entity_id: "e2",
      display_name: "billing",
      canonical_name: "billing",
      entity_type: "system",
      source_system: "git",
      source_record_id: "repo-1",
    },
    tier: "name_similarity",
    confidence: 0.7,
    reason:
      "exact normalised name match across sources with a corroborating observed relationship",
    corroborating_relationships: [
      { relationship_type: "depends_on", entity_id: "team-1" },
    ],
  },
  revision: 0,
  decided_by: null,
  decided_at: null,
  note: null,
  first_proposed_at: "2026-08-03T10:00:00+00:00",
  last_proposed_at: "2026-08-03T10:00:00+00:00",
};

const CONFIRMED: EntityMatchProposal = {
  ...PENDING,
  proposal_id: "emp_done999",
  status: "confirmed",
  revision: 1,
  decided_by: "analyst@example.com",
  decided_at: "2026-08-03T11:00:00+00:00",
};

function listResponse(
  proposals: EntityMatchProposal[],
  counts?: Partial<Record<"pending" | "confirmed" | "rejected", number>>,
): ProposalListResponse {
  return {
    proposals,
    counts: { pending: 0, confirmed: 0, rejected: 0, ...counts },
    status: null,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/entity-matches"]}>
      <Routes>
        <Route path="/entity-matches" element={<EntityMatchReviewPage />} />
        <Route path="/integration-hub" element={<div>integration hub</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  h.role.current = "analyst";
  h.mockList.mockResolvedValue(listResponse([PENDING], { pending: 1 }));
  h.mockDecide.mockResolvedValue({
    proposal: { ...PENDING, status: "confirmed" },
    action: "confirm",
    previous_status: "pending",
    resulting_status: "confirmed",
    revision: 1,
    changed: true,
    actor_id: "analyst@example.com",
    decided_at: "2026-08-03T11:00:00+00:00",
  });
  h.mockScan.mockResolvedValue({
    created: 1,
    refreshed: 0,
    skipped_already_decided: 0,
    entity_types: ["system"],
  });
});

// ── the evidence a reviewer decides from ────────────────────────────────────

describe("proposed match card", () => {
  it("shows both entities with their source system and record", async () => {
    renderPage();
    const card = await screen.findByTestId("proposal-card");

    expect(within(card).getByText("Billing")).toBeInTheDocument();
    expect(within(card).getByText("servicenow")).toBeInTheDocument();
    expect(within(card).getByText("sn-2")).toBeInTheDocument();
    expect(within(card).getByText("git")).toBeInTheDocument();
    expect(within(card).getByText("repo-1")).toBeInTheDocument();
  });

  it("explains WHY it was proposed, including the corroborating relationship", async () => {
    renderPage();
    await screen.findByTestId("proposal-card");

    expect(screen.getByTestId("proposal-reason")).toHaveTextContent(
      /exact normalised name match across sources/i,
    );
    const corroboration = screen.getByTestId("proposal-corroboration");
    expect(corroboration).toHaveTextContent("depends_on");
    expect(corroboration).toHaveTextContent("team-1");
  });

  it("labels the match as pending and never claims it was merged", async () => {
    renderPage();
    const card = await screen.findByTestId("proposal-card");

    expect(screen.getByTestId("proposal-status")).toHaveAttribute(
      "data-status",
      "pending",
    );
    // The card itself must never say "merged" — the only place that word may
    // appear is the page-level disclaimer stating nothing IS merged here (its
    // own test below).
    expect(card.textContent).not.toMatch(/merge/i);
  });

  it("states that nothing is merged from this screen", async () => {
    renderPage();
    await screen.findByTestId("proposal-card");
    expect(
      screen.getByText(/Nothing is merged from this screen/i),
    ).toBeInTheDocument();
  });
});

// ── confirm / reject ────────────────────────────────────────────────────────

describe("deciding", () => {
  it("confirms a match and refreshes the queue", async () => {
    renderPage();
    await screen.findByTestId("proposal-card");

    fireEvent.click(screen.getByRole("button", { name: /confirm this match/i }));

    await waitFor(() =>
      expect(h.mockDecide).toHaveBeenCalledWith("emp_abc123", "confirm"),
    );
    await waitFor(() => expect(h.mockList).toHaveBeenCalledTimes(2));
    expect(h.mockPush).toHaveBeenCalledWith(
      expect.stringMatching(/confirmed/i),
      "success",
    );
  });

  it("rejects a match", async () => {
    renderPage();
    await screen.findByTestId("proposal-card");

    fireEvent.click(screen.getByRole("button", { name: /reject this match/i }));

    await waitFor(() =>
      expect(h.mockDecide).toHaveBeenCalledWith("emp_abc123", "reject"),
    );
  });

  it("says so when the decision was already recorded", async () => {
    h.mockDecide.mockResolvedValue({
      proposal: PENDING,
      action: "confirm",
      previous_status: "confirmed",
      resulting_status: "confirmed",
      revision: 1,
      changed: false,
      actor_id: "a",
      decided_at: "2026-08-03T11:00:00+00:00",
    });
    renderPage();
    await screen.findByTestId("proposal-card");

    fireEvent.click(screen.getByRole("button", { name: /confirm this match/i }));

    await waitFor(() =>
      expect(h.mockPush).toHaveBeenCalledWith(
        expect.stringMatching(/already recorded/i),
        "success",
      ),
    );
  });

  it("surfaces a failed decision without pretending it worked", async () => {
    h.mockDecide.mockRejectedValue(new Error("boom"));
    renderPage();
    await screen.findByTestId("proposal-card");

    fireEvent.click(screen.getByRole("button", { name: /confirm this match/i }));

    await waitFor(() =>
      expect(h.mockPush).toHaveBeenCalledWith(
        expect.stringMatching(/could not record the decision/i),
        "error",
      ),
    );
  });
});

// ── a decided proposal ──────────────────────────────────────────────────────

describe("decided proposals", () => {
  beforeEach(() => {
    h.mockList.mockResolvedValue(listResponse([CONFIRMED], { confirmed: 1 }));
  });

  it("shows who decided it and that it will not be proposed again", async () => {
    renderPage();
    const decided = await screen.findByTestId("proposal-decided");

    expect(decided).toHaveTextContent(/confirmed/i);
    expect(decided).toHaveTextContent("analyst@example.com");
    expect(decided).toHaveTextContent(/will not be proposed again/i);
  });

  it("offers no confirm/reject buttons (only Undo)", async () => {
    renderPage();
    await screen.findByTestId("proposal-decided");

    expect(screen.queryByRole("button", { name: /confirm this match/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /reject this match/i })).toBeNull();
  });

  it("offers an Undo button to reverse a decision", async () => {
    renderPage();
    await screen.findByTestId("proposal-decided");

    expect(
      screen.getByRole("button", { name: /undo this decision/i }),
    ).toBeTruthy();
  });

  it("undoes a decision and returns the match to the pending queue", async () => {
    h.mockDecide.mockResolvedValue({
      proposal: { ...CONFIRMED, status: "pending", decided_by: null, decided_at: null },
      action: "undo",
      previous_status: "confirmed",
      resulting_status: "pending",
      revision: 2,
      changed: true,
      actor_id: "analyst@example.com",
      decided_at: "2026-08-03T12:00:00+00:00",
    });
    renderPage();
    await screen.findByTestId("proposal-decided");

    fireEvent.click(screen.getByRole("button", { name: /undo this decision/i }));

    await waitFor(() =>
      expect(h.mockDecide).toHaveBeenCalledWith("emp_done999", "undo"),
    );
    await waitFor(() =>
      expect(h.mockPush).toHaveBeenCalledWith(
        expect.stringMatching(/pending queue/i),
        "success",
      ),
    );
    // The queue is reloaded after the undo so the item leaves this tab.
    expect(h.mockList).toHaveBeenCalledTimes(2);
  });

  it("says there is nothing to undo when the match is already pending", async () => {
    h.mockDecide.mockResolvedValue({
      proposal: CONFIRMED,
      action: "undo",
      previous_status: "pending",
      resulting_status: "pending",
      revision: 1,
      changed: false,
      actor_id: "analyst@example.com",
      decided_at: null,
    });
    renderPage();
    await screen.findByTestId("proposal-decided");

    fireEvent.click(screen.getByRole("button", { name: /undo this decision/i }));

    await waitFor(() =>
      expect(h.mockPush).toHaveBeenCalledWith(
        expect.stringMatching(/nothing to undo/i),
        "success",
      ),
    );
  });
});

// ── queue mechanics ─────────────────────────────────────────────────────────

describe("the queue", () => {
  it("loads pending first and switches status on the tabs", async () => {
    renderPage();
    await screen.findByTestId("proposal-card");
    expect(h.mockList).toHaveBeenCalledWith("pending");

    fireEvent.click(screen.getByTestId("proposal-tab-confirmed"));
    await waitFor(() => expect(h.mockList).toHaveBeenCalledWith("confirmed"));
  });

  it("explains an empty pending queue rather than showing a bare blank", async () => {
    h.mockList.mockResolvedValue(listResponse([]));
    renderPage();

    const empty = await screen.findByTestId("proposals-empty");
    expect(empty).toHaveTextContent(/resolve automatically and never appear here/i);
  });

  it("reports a failed load with a retry", async () => {
    h.mockList.mockRejectedValue(new Error("down"));
    renderPage();

    expect(
      await screen.findByText(/could not load entity match proposals/i),
    ).toBeInTheDocument();
  });

  it("scans on demand and reports what it found", async () => {
    renderPage();
    await screen.findByTestId("proposal-card");

    fireEvent.click(screen.getByRole("button", { name: /scan for matches/i }));

    await waitFor(() => expect(h.mockScan).toHaveBeenCalled());
    expect(h.mockPush).toHaveBeenCalledWith(
      expect.stringMatching(/1 new/i),
      "success",
    );
  });

  it("reports pairs a scan left alone because they were already decided", async () => {
    h.mockScan.mockResolvedValue({
      created: 0,
      refreshed: 0,
      skipped_already_decided: 3,
      entity_types: ["system"],
    });
    renderPage();
    await screen.findByTestId("proposal-card");

    fireEvent.click(screen.getByRole("button", { name: /scan for matches/i }));

    await waitFor(() =>
      expect(h.mockPush).toHaveBeenCalledWith(
        expect.stringMatching(/3 already decided/i),
        "success",
      ),
    );
  });
});

// ── access ──────────────────────────────────────────────────────────────────

describe("access", () => {
  it("never mounts the content for a viewer", async () => {
    h.role.current = "viewer";
    renderPage();

    expect(await screen.findByText("integration hub")).toBeInTheDocument();
    expect(screen.queryByTestId("proposal-card")).toBeNull();
    expect(h.mockList).not.toHaveBeenCalled();
  });
});
