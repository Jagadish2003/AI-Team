// @vitest-environment jsdom
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  EnrichmentPanel,
  parseObservationTag,
} from "../components/analyst_review/OpportunityDetail";
import type { OppEnrichment } from "../api/enrichmentApi";
import type { OpportunityCandidate } from "../types/analystReview";

// Minimal opportunity stub — EnrichmentPanel only reads title/aiRationale.
const OPP: OpportunityCandidate = {
  id: "opp_001",
  title: "SLA breaches rising in billing queue",
  category: "operations",
  tier: "Tier 1" as OpportunityCandidate["tier"],
  impact: 8,
  effort: 4,
  confidence: "High" as OpportunityCandidate["confidence"],
  aiRationale: "Deterministic rationale fallback.",
  evidenceIds: [],
  decision: "UNREVIEWED" as OpportunityCandidate["decision"],
  override: {} as OpportunityCandidate["override"],
};

// Full enrichment object — every field present so the type stays honest.
function makeEnrichment(overrides: Partial<OppEnrichment>): OppEnrichment {
  return {
    oppId: "opp_001",
    aiSummary: "Billing queue SLA breaches are climbing.",
    aiWhyBullets: [],
    aiRisks: [],
    aiSuggestedNextSteps: [],
    llmGenerated: true,
    llmModel: "claude-sonnet-4-5",
    baseline_context: null,
    trend_direction: null,
    anomaly_score: null,
    is_anomalous: false,
    first_deviation: false,
    baseline_mean: null,
    baseline_stddev: null,
    baseline_window_days: null,
    run_count: null,
    current_value: null,
    recent_values: [],
    signal_key: null,
    pack_id: null,
    entities: [],
    relationships: [],
    llm_grounded: true,
    graph_entity_count: 5,
    graph_entity_count_shown: 5,
    graph_truncated: false,
    hallucination_removals: [],
    hallucination_rewrites: 0,
    hallucination_llm_rewrites: 0,
    preliminary: false,
    preliminary_reason: null,
    corroboration_label: null,
    ...overrides,
  };
}

describe("ENT-3 / T3-S15-A parseObservationTag", () => {
  it("parses an [OBSERVED] tag", () => {
    const r = parseObservationTag("[OBSERVED] Sarah Chen owns the queue");
    expect(r.kind).toBe("observed");
    expect(r.basis).toBeNull();
    expect(r.text).toBe("Sarah Chen owns the queue");
  });

  it("parses an [INFERRED: basis] tag and captures the basis", () => {
    const r = parseObservationTag("[INFERRED: co-firing signals] Risk is rising");
    expect(r.kind).toBe("inferred");
    expect(r.basis).toBe("co-firing signals");
    expect(r.text).toBe("Risk is rising");
  });

  it("returns no tag for an untagged bullet", () => {
    const r = parseObservationTag("Plain bullet without a tag");
    expect(r.kind).toBeNull();
    expect(r.text).toBe("Plain bullet without a tag");
  });
});

describe("ENT-3 / T3-S15-A EnrichmentPanel evidence trace", () => {
  it("renders the preliminary banner with the reason when preliminary=true (AC9)", () => {
    const enrichment = makeEnrichment({
      preliminary: true,
      preliminary_reason: "Baseline context is still accumulating (3 of 10 runs completed)",
    });
    render(<EnrichmentPanel opp={OPP} enrichment={enrichment} />);

    const banner = screen.getByTestId("preliminary-banner");
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveTextContent("Analyst review required");
    expect(banner).toHaveTextContent(
      "Baseline context is still accumulating (3 of 10 runs completed)"
    );
  });

  it("does NOT render the preliminary banner when preliminary=false", () => {
    const enrichment = makeEnrichment({ preliminary: false });
    render(<EnrichmentPanel opp={OPP} enrichment={enrichment} />);
    expect(screen.queryByTestId("preliminary-banner")).toBeNull();
  });

  it("renders an OBSERVED pill and an INFERRED pill per tagged why-bullet", () => {
    const enrichment = makeEnrichment({
      aiWhyBullets: [
        "[OBSERVED] Sarah Chen owns 12 open cases",
        "[INFERRED: co-firing signals] Backlog risk is spreading",
      ],
    });
    render(<EnrichmentPanel opp={OPP} enrichment={enrichment} />);

    expect(screen.getByTestId("observation-pill-observed")).toBeInTheDocument();
    expect(screen.getByTestId("observation-pill-inferred")).toBeInTheDocument();
    // Tag prefix is stripped from the rendered bullet text.
    expect(screen.getByText("Sarah Chen owns 12 open cases")).toBeInTheDocument();
    expect(screen.getByText("Backlog risk is spreading")).toBeInTheDocument();
  });

  it("renders the corroboration label below the analysis when present", () => {
    const enrichment = makeEnrichment({
      corroboration_label: "Corroborated across Jira and ServiceNow",
    });
    render(<EnrichmentPanel opp={OPP} enrichment={enrichment} />);
    const label = screen.getByTestId("corroboration-label");
    expect(label).toHaveTextContent("Corroborated across Jira and ServiceNow");
  });

  it("omits the corroboration label when not provided", () => {
    const enrichment = makeEnrichment({ corroboration_label: null });
    render(<EnrichmentPanel opp={OPP} enrichment={enrichment} />);
    expect(screen.queryByTestId("corroboration-label")).toBeNull();
  });
});
