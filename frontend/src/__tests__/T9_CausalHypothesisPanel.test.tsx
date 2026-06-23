// @vitest-environment jsdom
/**
 * T9 — CausalHypothesisPanel evidence trace rendering
 *
 * Covers all four states:
 *   1. null/undefined → renders nothing
 *   2. preliminary Gate 1 — amber banner with "X of N runs completed"
 *   3. preliminary Gate 2 / Gate 3 — correct banner, muted chain, muted-italic falsifiability
 *   4. confirmed — numbered list, [inferred] labels, "How to disprove this:" prefix
 *
 * Run:
 *   npx vitest run src/__tests__/T9_CausalHypothesisPanel.test.tsx
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CausalHypothesisPanel } from "../components/analyst_review/OpportunityDetail";
import type { CausalHypothesisSummary } from "../api/enrichmentApi";

// ── Base fixture ──────────────────────────────────────────────────────────────

const BASE_HYPOTHESIS: CausalHypothesisSummary = {
  cause_chain: [
    "Loan origination volume rose 40% above baseline [OBSERVED, rising, anomalous].",
    "[inferred: confidence=0.6] Backlog pressure from Jira reduces ServiceNow capacity.",
    "Covenant review queue backed up as loans awaited Credit Review clearance.",
  ],
  falsifiability_condition:
    "If covenant review completion rate does not improve when loan origination volume returns to baseline, the capacity hypothesis is incorrect.",
  confidence: 0.82,
  inferred: true,
  preliminary: false,
  preliminary_reason: null,
};

function hyp(overrides: Partial<CausalHypothesisSummary>): CausalHypothesisSummary {
  return { ...BASE_HYPOTHESIS, ...overrides };
}

// ── State 1: null / undefined ─────────────────────────────────────────────────

describe("CausalHypothesisPanel — null / undefined", () => {
  it("renders nothing when causal_hypothesis is null", () => {
    const { container } = render(<CausalHypothesisPanel causal_hypothesis={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when causal_hypothesis is undefined", () => {
    const { container } = render(<CausalHypothesisPanel causal_hypothesis={undefined} />);
    expect(container.firstChild).toBeNull();
  });
});

// ── State 2: preliminary Gate 1 ───────────────────────────────────────────────

describe("CausalHypothesisPanel — preliminary Gate 1 (insufficient run count)", () => {
  const gate1 = hyp({
    preliminary: true,
    preliminary_reason: "gate1_insufficient_run_count: 7 of 10 runs completed",
  });

  it("renders the amber banner", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate1} />);
    expect(screen.getByTestId("causal-preliminary-banner")).toBeInTheDocument();
  });

  it("banner contains parsed run counts", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate1} />);
    const banner = screen.getByTestId("causal-preliminary-banner");
    expect(banner.textContent).toContain("Preliminary");
    expect(banner.textContent).toContain("7 of 10 runs completed");
  });

  it("renders the cause chain text in opacity-75 when preliminary (badge stays full opacity)", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate1} />);
    const step0 = screen.getByTestId("causal-step-0");
    // opacity-75 is on the inner text div, not the card, so the INFERRED badge keeps full opacity
    expect(step0.querySelector(".opacity-75")).toBeTruthy();
    expect(step0).not.toHaveClass("opacity-75");
  });

  it("falsifiability condition is rendered in muted italic", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate1} />);
    const fc = screen.getByTestId("causal-falsifiability");
    expect(fc).toBeInTheDocument();
    expect(fc).toHaveClass("italic");
    expect(fc).toHaveClass("text-muted");
  });

  it("does NOT show 'How to disprove this:' prefix when preliminary", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate1} />);
    expect(screen.queryByText(/How to disprove this/)).toBeNull();
  });
});

// ── State 3a: preliminary Gate 2 (unresolved entities) ───────────────────────

describe("CausalHypothesisPanel — preliminary Gate 2 (unresolved entities)", () => {
  const gate2 = hyp({
    preliminary: true,
    preliminary_reason: "gate2_unresolved_entities: 3 entities require resolution",
  });

  it("renders correct Gate 2 banner text with entity count", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate2} />);
    const banner = screen.getByTestId("causal-preliminary-banner");
    expect(banner.textContent).toContain("Preliminary");
    expect(banner.textContent).toContain("3 entities require resolution");
  });

  it("chain text is muted when preliminary (badge stays full opacity)", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate2} />);
    const step0 = screen.getByTestId("causal-step-0");
    expect(step0.querySelector(".opacity-75")).toBeTruthy();
  });
});

// ── State 3b: preliminary Gate 3 (inferred primary step) ─────────────────────

describe("CausalHypothesisPanel — preliminary Gate 3 (inferred primary step)", () => {
  const gate3 = hyp({
    preliminary: true,
    preliminary_reason: "gate3_inferred_primary_step: step 2",
  });

  it("renders correct Gate 3 banner text", () => {
    render(<CausalHypothesisPanel causal_hypothesis={gate3} />);
    const banner = screen.getByTestId("causal-preliminary-banner");
    expect(banner.textContent).toContain("Preliminary");
    expect(banner.textContent).toContain("inferred relationships that have not yet been validated");
    expect(banner.textContent?.match(/Preliminary/g)).toHaveLength(1);
  });
});

// ── State 4: confirmed (all gates passed) ─────────────────────────────────────

describe("CausalHypothesisPanel — confirmed (preliminary === false)", () => {
  const confirmed = hyp({ preliminary: false, preliminary_reason: null });

  it("renders the panel with heading", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    expect(screen.getByTestId("causal-hypothesis-panel")).toBeInTheDocument();
    expect(screen.getByText("Causal Hypothesis")).toBeInTheDocument();
  });

  it("does NOT render an amber banner", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    expect(screen.queryByTestId("causal-preliminary-banner")).toBeNull();
  });

  it("renders the numbered cause chain in full (non-muted) styling", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    const step0 = screen.getByTestId("causal-step-0");
    expect(step0).toHaveClass("text-text");
    expect(step0).not.toHaveClass("opacity-75");
  });

  it("renders all three cause chain steps", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    expect(screen.getByTestId("causal-step-0")).toBeInTheDocument();
    expect(screen.getByTestId("causal-step-1")).toBeInTheDocument();
    expect(screen.getByTestId("causal-step-2")).toBeInTheDocument();
  });

  it("shows [inferred] label only on the step prefixed with [inferred:", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    // Step 1 has [inferred: confidence=0.6] prefix → label present
    expect(screen.getByTestId("causal-inferred-label-1")).toBeInTheDocument();
    // Steps 0 and 2 have no [inferred:] prefix → labels absent
    expect(screen.queryByTestId("causal-inferred-label-0")).toBeNull();
    expect(screen.queryByTestId("causal-inferred-label-2")).toBeNull();
  });

  it("'How to disprove this:' prefix is present on confirmed hypothesis", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    expect(screen.getByText("How to disprove this:")).toBeInTheDocument();
  });

  it("falsifiability condition body text is rendered", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    expect(
      screen.getByText(
        /If covenant review completion rate does not improve/
      )
    ).toBeInTheDocument();
  });

  it("shows confidence badge as a visually subordinate indicator", () => {
    render(<CausalHypothesisPanel causal_hypothesis={confirmed} />);
    expect(screen.getByTestId("causal-confidence-badge")).toHaveTextContent("82%");
  });
});

// ── Inferred label amber styling ──────────────────────────────────────────────

describe("CausalHypothesisPanel — [inferred] label styling", () => {
  it("uses amber tokens for the inferred label", () => {
    render(<CausalHypothesisPanel causal_hypothesis={hyp({ preliminary: false })} />);
    const label = screen.getByTestId("causal-inferred-label-1");
    expect(label).toHaveClass("text-amber-600");
    expect(label).toHaveClass("bg-amber-500/10");
  });
});

// ── Unknown / fallback preliminary_reason ─────────────────────────────────────

describe("CausalHypothesisPanel — unknown preliminary_reason fallback", () => {
  it("falls back to generic banner text for unrecognised reason", () => {
    render(
      <CausalHypothesisPanel
        causal_hypothesis={hyp({
          preliminary: true,
          preliminary_reason: "some_future_gate: details",
        })}
      />
    );
    const banner = screen.getByTestId("causal-preliminary-banner");
    expect(banner.textContent).toContain("Preliminary");
    expect(banner.textContent).toContain("analyst review required");
  });

  it("falls back to generic banner when preliminary_reason is null", () => {
    render(
      <CausalHypothesisPanel
        causal_hypothesis={hyp({ preliminary: true, preliminary_reason: null })}
      />
    );
    const banner = screen.getByTestId("causal-preliminary-banner");
    expect(banner.textContent).toContain("Preliminary");
    expect(banner.textContent).toContain("analyst review required");
  });
});
