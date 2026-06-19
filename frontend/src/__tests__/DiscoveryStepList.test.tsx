// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { DiscoveryStepList } from "../pages/DiscoveryRunPage";

describe("DiscoveryStepList step state (CS-4 T5)", () => {
  it("marks an in-progress step active and earlier steps completed", () => {
    render(<DiscoveryStepList currentStep="sn" />);

    // Backend emission order: sf_crm → sn → jira → sf_ncino → … so only
    // sf_crm precedes "sn" → 1 completed; "sn" itself → active.
    expect(screen.getAllByLabelText("completed").length).toBe(1);
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });

  it("shows the terminal Complete step as completed (check) only once the run has finished", () => {
    render(<DiscoveryStepList currentStep="complete" runComplete />);

    // All seven steps — including "Complete" — render the completed check.
    expect(screen.getAllByLabelText("completed").length).toBe(7);
    // No spinner remains once the run has finished.
    expect(screen.queryByLabelText("active")).not.toBeInTheDocument();
  });

  it("keeps Complete as a spinner while the run is still computing at the complete step", () => {
    render(<DiscoveryStepList currentStep="complete" runComplete={false} />);

    // The six preceding steps are done, but Complete is not ticked yet.
    expect(screen.getAllByLabelText("completed").length).toBe(6);
    // Complete shows the active spinner until the run reaches 100%.
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });
});

describe("DiscoveryStepList Salesforce product labelling (CS-4)", () => {
  it("defaults the second Salesforce pass to nCino Lending when no product is declared", () => {
    render(<DiscoveryStepList currentStep="sf_crm" />);
    expect(screen.getByText("nCino Lending")).toBeInTheDocument();
    expect(
      screen.getByText("Ingesting nCino loan origination signals")
    ).toBeInTheDocument();
  });

  it("relabels the second Salesforce pass to Service Cloud when declared", () => {
    render(
      <DiscoveryStepList currentStep="sf_crm" salesforceProduct="salesforce_sc" />
    );
    expect(screen.getByText("Service Cloud")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Ingesting case management, service request, and SLA signals"
      )
    ).toBeInTheDocument();
    // The nCino copy must no longer appear once Service Cloud is declared.
    expect(screen.queryByText("nCino Lending")).not.toBeInTheDocument();
  });

  it("places the Service Cloud pass after Jira, matching backend log order", () => {
    render(
      <DiscoveryStepList
        currentStep="sf_ncino"
        salesforceProduct="salesforce_sc"
      />
    );
    // sf_ncino is now index 3 (after sf_crm, sn, jira) → three completed steps,
    // confirming the Service Cloud pass renders after Jira ingestion.
    expect(screen.getAllByLabelText("completed").length).toBe(3);
    expect(screen.getByLabelText("active")).toBeInTheDocument();
  });
});
