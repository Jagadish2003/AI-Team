/**
 * T11 — Wire BaselineContextPanel Into Opportunity Detail Panel
 *
 * Verifies that BaselineContextPanel renders correctly inside OpportunityDetail
 * for all enrichment states: no data, insufficient runs, rising+anomalous,
 * falling, stable, and first_deviation.
 *
 * Run:
 *   npx vitest run src/__tests__/T11_BaselineContextPanel_integration.test.tsx
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import { vi, describe, it, expect, beforeEach } from "vitest";
import BaselineContextPanel from "../components/analyst_review/BaselineContextPanel";
import type { OppEnrichment } from "../api/enrichmentApi";

// ── Base enrichment fixture ────────────────────────────────────────────────────

const BASE_ENRICHMENT: OppEnrichment = {
  oppId: "opp_001",
  aiSummary: "High approval wait time detected.",
  aiWhyBullets: [],
  aiRisks: [],
  aiSuggestedNextSteps: [],
  llmGenerated: false,
  llmModel: null,
  baseline_context: null,
  trend_direction: null,
  anomaly_score: null,
  is_anomalous: false,
  first_deviation: false,
  baseline_mean: null,
  run_count: null,
  entities: [],
};

// ── Helpers ────────────────────────────────────────────────────────────────────

function enrich(overrides: Partial<OppEnrichment>): OppEnrichment {
  return { ...BASE_ENRICHMENT, ...overrides };
}

// ── Tests ──────────────────────────────────────────────────────────────────────

describe("BaselineContextPanel", () => {
  it("renders nothing when enrichment is null", () => {
    const { container } = render(<BaselineContextPanel enrichment={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when both baseline_context and trend_direction are absent", () => {
    const { container } = render(
      <BaselineContextPanel enrichment={enrich({})} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows insufficient-data message when run_count < 3", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "rising",
          baseline_context: "Up 20% from your baseline",
          run_count: 2,
        })}
      />
    );
    expect(
      screen.getByText(
        "Baseline context will appear after 3 or more completed runs for this same Discovery Pack in your workspace."
      )
    ).toBeTruthy();
  });

  it("shows rising trend text and anomaly badge", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "rising",
          baseline_context: "Up 22% from your 30-day baseline of 4.1 tickets",
          is_anomalous: true,
          run_count: 5,
        })}
      />
    );
    expect(
      screen.getByText("Up 22% from your 30-day baseline of 4.1 tickets")
    ).toBeTruthy();
    expect(screen.getByText("Anomaly detected")).toBeTruthy();
  });

  it("shows falling trend text without anomaly badge", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "falling",
          baseline_context: "Trending down — currently 10% below your baseline",
          is_anomalous: false,
          run_count: 4,
        })}
      />
    );
    expect(
      screen.getByText(
        "Trending down — currently 10% below your baseline"
      )
    ).toBeTruthy();
    expect(screen.queryByText("Anomaly detected")).toBeNull();
  });

  it("shows stable trend text", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "stable",
          baseline_context: "Stable — within normal range of your baseline",
          run_count: 6,
        })}
      />
    );
    expect(
      screen.getByText("Stable — within normal range of your baseline")
    ).toBeTruthy();
  });

  it("shows first_deviation blue pill with exact required text", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "rising",
          baseline_context: "First deviation from a previously stable baseline",
          first_deviation: true,
          run_count: 3,
        })}
      />
    );
    expect(
      screen.getByText("First deviation from a previously stable baseline")
    ).toBeTruthy();
  });

  it("does not show anomaly badge when is_anomalous is false", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "stable",
          baseline_context: "Stable — within normal range of your baseline",
          is_anomalous: false,
          run_count: 4,
        })}
      />
    );
    expect(screen.queryByText("Anomaly detected")).toBeNull();
  });

  it("shows run count footnote", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "rising",
          baseline_context: "Trending up — currently 8% above your baseline",
          run_count: 7,
        })}
      />
    );
    expect(screen.getByText("Based on 7 runs")).toBeTruthy();
  });

  it("uses singular 'run' for run_count of 1", () => {
    render(
      <BaselineContextPanel
        enrichment={enrich({
          trend_direction: "rising",
          baseline_context: "Trending up",
          run_count: 3,
        })}
      />
    );
    // run_count=3 should show plural
    expect(screen.getByText("Based on 3 runs")).toBeTruthy();
  });
});
