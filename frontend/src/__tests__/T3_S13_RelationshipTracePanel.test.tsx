// @vitest-environment jsdom
import React from "react";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RelationshipTracePanel } from "../components/analyst_review/OpportunityDetail";
import type { RelationshipSummary } from "../api/enrichmentApi";

const RELATIONSHIPS: RelationshipSummary[] = [
  {
    from_entity_name: "Sarah Chen",
    from_entity_type: "person",
    relationship_type: "owns",
    to_entity_name: "Loan Application 1042",
    to_entity_type: "object",
    inferred: false,
    confidence: 0.9,
  },
  {
    from_entity_name: "Covenant Review",
    from_entity_type: "process",
    relationship_type: "depends_on",
    to_entity_name: "Loan Origination",
    to_entity_type: "process",
    inferred: true,
    confidence: 0.6,
  },
];

describe("T3-S13-A RelationshipTracePanel", () => {
  it("renders nothing when no relationship summaries are available", () => {
    const { container } = render(<RelationshipTracePanel relationships={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders observed and inferred relationships with confidence", () => {
    render(<RelationshipTracePanel relationships={RELATIONSHIPS} />);

    expect(screen.getByText("Relationships")).toBeInTheDocument();
    expect(screen.getByText("2 linked")).toBeInTheDocument();
    expect(screen.getByText("Sarah Chen owns Loan Application 1042")).toBeInTheDocument();
    expect(
      screen.getByText("Covenant Review depends on Loan Origination")
    ).toBeInTheDocument();
    expect(screen.getByText("90%")).toBeInTheDocument();
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByTestId("relationship-inferred-1")).toHaveTextContent("inferred");
  });

  it("keeps relationship rows inside a thin inner scroll area", () => {
    render(<RelationshipTracePanel relationships={RELATIONSHIPS} />);

    const scrollArea = screen.getByTestId("relationship-trace-scroll");
    expect(scrollArea).toHaveClass("max-h-[13.5rem]", "overflow-y-auto", "pr-1");
  });
});
