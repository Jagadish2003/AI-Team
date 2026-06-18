// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { DiscoveryStepList } from "../pages/DiscoveryRunPage";

describe("DiscoveryStepList step state (CS-4 T5)", () => {
  it("marks an in-progress step active and earlier steps completed", () => {
    render(<DiscoveryStepList currentStep="sn" />);

    // sf_crm and sf_ncino precede "sn" → completed; "sn" itself → active.
    expect(screen.getAllByLabelText("completed").length).toBe(2);
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
