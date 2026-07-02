// @vitest-environment jsdom
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EntityTracePanel } from "../components/analyst_review/OpportunityDetail";
import type { EntitySummary } from "../api/enrichmentApi";

const ENTITY_MIN_RUN_COUNT_FROM_API = 3;

const ENTITIES: EntitySummary[] = [
  {
    entity_id: "ent_person_1",
    entity_type: "person",
    display_name: "Sarah Chen",
    source_system: "jira",
    resolution_confidence: 0.8,
    resolution_status: "resolved",
  },
  {
    entity_id: "ent_team_1",
    entity_type: "team",
    display_name: "Commercial Credit",
    source_system: "servicenow",
    resolution_confidence: 0.6,
    resolution_status: "ambiguous",
  },
];

const MANY_ENTITIES: EntitySummary[] = Array.from({ length: 9 }, (_, index) => ({
  entity_id: `ent_${index + 1}`,
  entity_type: index % 2 === 0 ? "person" : "process",
  display_name: `Entity ${index + 1}`,
  source_system: index % 2 === 0 ? "salesforce" : "agentiq",
  resolution_confidence: 1,
  resolution_status: "resolved",
}));

describe("T3-S12-A EntityTracePanel", () => {
  it("renders nothing before enrichment entities are loaded", () => {
    const { container } = render(<EntityTracePanel entities={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows the run-history message when no visible entities are available yet", () => {
    render(
      <EntityTracePanel
        entities={[]}
        runCount={2}
        entityMinRunCount={ENTITY_MIN_RUN_COUNT_FROM_API}
      />
    );

    expect(screen.getByText("Entities")).toBeInTheDocument();
    expect(screen.getByText("Hidden until 3 runs")).toBeInTheDocument();
    expect(
      screen.getByText("Entities will appear after 3 or more discovery runs.")
    ).toBeInTheDocument();
    expect(
      screen.getByText(/retaining early entity signals for graph completeness/i)
    ).toBeInTheDocument();
  });

  it("does not claim run-history hiding when enrichment loaded without a run count", () => {
    render(<EntityTracePanel entities={undefined} enrichmentLoaded />);

    expect(screen.getByText("Entities")).toBeInTheDocument();
    expect(screen.getByText("0 linked")).toBeInTheDocument();
    expect(screen.getByText("No entities linked to this opportunity.")).toBeInTheDocument();
    expect(screen.queryByText("Hidden until 3 runs")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Entities will appear after 3 or more discovery runs.")
    ).not.toBeInTheDocument();
  });

  it("shows a neutral empty state for a mature run with no linked entities", () => {
    render(
      <EntityTracePanel
        entities={[]}
        runCount={10}
        entityMinRunCount={ENTITY_MIN_RUN_COUNT_FROM_API}
      />
    );

    expect(screen.getByText("0 linked")).toBeInTheDocument();
    expect(screen.getByText("No entities linked to this opportunity.")).toBeInTheDocument();
    expect(screen.queryByText("Hidden until 3 runs")).not.toBeInTheDocument();
    expect(
      screen.getByText(/will show linked people, systems, and process entities/i)
    ).toBeInTheDocument();
  });

  it("renders entity summaries below the baseline context contract", () => {
    render(<EntityTracePanel entities={ENTITIES} />);

    expect(screen.getByText("Entities")).toBeInTheDocument();
    expect(screen.getByText("2 linked")).toBeInTheDocument();
    expect(screen.getByText("Sarah Chen")).toBeInTheDocument();
    expect(screen.getByText("Commercial Credit")).toBeInTheDocument();
    expect(screen.getByText("Jira")).toBeInTheDocument();
    expect(screen.getByText("ServiceNow")).toBeInTheDocument();
    expect(screen.getByText("80%")).toBeInTheDocument();
    expect(screen.getByText("60%")).toBeInTheDocument();
  });

  it("marks ambiguous entities with muted styling", () => {
    render(<EntityTracePanel entities={ENTITIES} />);

    expect(screen.getByText("Ambiguous")).toBeInTheDocument();
    expect(screen.getByTestId("entity-trace-ent_team_1")).toHaveClass(
      "text-muted",
      "opacity-75"
    );
  });

  it("keeps more than four entity rows inside a compact inner scroll area", () => {
    render(<EntityTracePanel entities={MANY_ENTITIES} />);

    const scrollArea = screen.getByTestId("entity-trace-scroll");
    expect(screen.getByText("9 linked")).toBeInTheDocument();
    expect(scrollArea).toHaveClass("max-h-[13.5rem]", "overflow-y-auto", "pr-1");
  });

  it("renders repeated occurrences of the same entity only once", () => {
    render(<EntityTracePanel entities={[ENTITIES[0], ENTITIES[0], ENTITIES[1]]} />);

    expect(screen.getByText("2 linked")).toBeInTheDocument();
    expect(screen.getAllByText("Sarah Chen")).toHaveLength(1);
  });

  it("shows entities without row-level run_count even when panel runCount is below threshold", () => {
    render(
      <EntityTracePanel
        entities={ENTITIES}
        runCount={2}
        entityMinRunCount={ENTITY_MIN_RUN_COUNT_FROM_API}
      />
    );

    expect(screen.getByText("2 linked")).toBeInTheDocument();
    expect(screen.getByText("Sarah Chen")).toBeInTheDocument();
    expect(screen.queryByText("Hidden until 3 runs")).not.toBeInTheDocument();
  });

  it("does not render entities with optional run_count below the display threshold", () => {
    render(
      <EntityTracePanel
        entities={[
          {
            ...ENTITIES[0],
            entity_id: "ent_early",
            display_name: "Early Entity",
            run_count: 2,
          },
          {
            ...ENTITIES[1],
            entity_id: "ent_ready",
            display_name: "Ready Entity",
            run_count: 3,
          },
        ]}
        runCount={3}
        entityMinRunCount={ENTITY_MIN_RUN_COUNT_FROM_API}
      />
    );

    expect(screen.queryByText("Early Entity")).not.toBeInTheDocument();
    expect(screen.getByText("Ready Entity")).toBeInTheDocument();
    expect(screen.getByText("1 linked")).toBeInTheDocument();
  });
});
