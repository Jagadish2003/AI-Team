/**
 * ReasoningOverrideViewer.test.tsx
 *
 * Viewers have read-only access to opportunity review: Approve, Reject, Save
 * Override, and Lock are analyst+ writes (the backend gates the decision/
 * override routes at analyst+), so they must be disabled for a viewer. The
 * read-only "View Evidence" action stays enabled.
 */
import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import type { OpportunityCandidate } from "../types/analystReview";

const h: { role: "owner" | "analyst" | "viewer" } = { role: "viewer" };

vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({ user: { email: "likhith@dwp.com", role: h.role } }),
}));

import ReasoningOverride from "../components/analyst_review/ReasoningOverride";

const OPP: OpportunityCandidate = {
  id: "opp_001",
  title: "Test opportunity",
  category: "ops",
  tier: "HIGH" as OpportunityCandidate["tier"],
  impact: 5,
  effort: 2,
  confidence: "HIGH" as OpportunityCandidate["confidence"],
  aiRationale: "because",
  evidenceIds: [],
  decision: "UNREVIEWED",
  override: {
    isLocked: false,
    rationaleOverride: "",
    overrideReason: "",
    updatedAt: null,
  },
};

function renderOverride() {
  return render(
    <ReasoningOverride
      opp={OPP}
      audit={[]}
      onSave={vi.fn()}
      onViewEvidence={vi.fn()}
      onDecision={vi.fn()}
    />,
  );
}

describe("ReasoningOverride — viewer is read-only", () => {
  it("disables Approve, Reject, Save Override and Lock for a viewer", () => {
    h.role = "viewer";
    renderOverride();
    expect(screen.getByRole("button", { name: /approve/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /save override/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /lock override/i })).toBeDisabled();
    // Read-only action stays available.
    expect(screen.getByRole("button", { name: /view evidence/i })).toBeEnabled();
  });

  it("enables Approve and Reject for an analyst", () => {
    h.role = "analyst";
    renderOverride();
    expect(screen.getByRole("button", { name: /approve/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /reject/i })).toBeEnabled();
  });
});
