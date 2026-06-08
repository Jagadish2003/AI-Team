// @vitest-environment jsdom
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EntityTracePanel } from "../components/analyst_review/OpportunityDetail";
import type { EntitySummary } from "../api/enrichmentApi";

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

describe("T3-S12-A EntityTracePanel", () => {
  it("renders nothing when no entity summaries are available", () => {
    const { container } = render(<EntityTracePanel entities={[]} />);
    expect(container.firstChild).toBeNull();
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
});
