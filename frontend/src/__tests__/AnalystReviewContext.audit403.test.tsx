/**
 * Regression: an owner-only audit 403 must not break the opportunity view.
 *
 * RBAC makes GET /api/runs/{run_id}/audit owner-only, so analysts and viewers
 * get 403 there. The opportunity page (Viewer+ per T1-S11 Task 2 AC2) fetches
 * opportunities AND the audit trail together — this test proves that a rejected
 * audit fetch degrades to an empty trail while opportunities still load, so a
 * non-owner can still access the page.
 */
import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";

import type { OpportunityCandidate } from "../types/analystReview";

const OPP: OpportunityCandidate = {
  id: "opp_001",
  title: "Test opportunity",
  decision: "UNREVIEWED",
} as unknown as OpportunityCandidate;

const fetchOpportunities = vi.fn();
const fetchAudit = vi.fn();

vi.mock("../api/analystReviewApi", () => ({
  fetchOpportunities: (runId: string) => fetchOpportunities(runId),
  fetchAudit: (runId: string) => fetchAudit(runId),
  postOpportunityDecision: vi.fn(),
  postOpportunityOverride: vi.fn(),
}));

vi.mock("../context/RunContext", () => ({
  useRunContext: () => ({ runId: "run_test" }),
}));

vi.mock("../context/DiscoveryRunContext", () => ({
  useDiscoveryRunContext: () => ({ run: { status: "complete" } }),
}));

import {
  AnalystReviewProvider,
  useAnalystReviewContext,
} from "../context/AnalystReviewContext";

function Probe() {
  const { loading, error, opportunities, audit } = useAnalystReviewContext();
  if (loading) return <div>loading</div>;
  return (
    <div>
      <div data-testid="error">{error ?? "no-error"}</div>
      <div data-testid="opp-count">{opportunities.length}</div>
      <div data-testid="audit-count">{audit.length}</div>
    </div>
  );
}

describe("AnalystReviewContext — audit 403 tolerance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders opportunities even when the audit fetch is forbidden (403)", async () => {
    fetchOpportunities.mockResolvedValue([OPP]);
    // Simulate the owner-only audit endpoint rejecting for a non-owner.
    fetchAudit.mockRejectedValue(
      Object.assign(new Error("Forbidden"), { status: 403 })
    );

    render(
      <AnalystReviewProvider>
        <Probe />
      </AnalystReviewProvider>
    );

    await waitFor(() =>
      expect(screen.getByTestId("opp-count")).toHaveTextContent("1")
    );
    // The page is NOT broken: no error surfaced, audit degrades to empty.
    expect(screen.getByTestId("error")).toHaveTextContent("no-error");
    expect(screen.getByTestId("audit-count")).toHaveTextContent("0");
  });

  it("still surfaces an error when the critical opportunities fetch fails", async () => {
    fetchOpportunities.mockRejectedValue(new Error("boom"));
    fetchAudit.mockResolvedValue([]);

    render(
      <AnalystReviewProvider>
        <Probe />
      </AnalystReviewProvider>
    );

    await waitFor(() =>
      expect(screen.getByTestId("error")).not.toHaveTextContent("no-error")
    );
    expect(screen.getByTestId("opp-count")).toHaveTextContent("0");
  });
});
