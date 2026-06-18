// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  SALESFORCE_DUAL_EXTRACTION_TOOLTIP,
  StepInfoTooltip,
} from "../pages/DiscoveryRunPage";

// CS-4 T6 (AT-314): the approved wording must match exactly.
const APPROVED_TEXT =
  "AgentIQ reads from your Salesforce system in two passes: the first reads " +
  "CRM signals (Cases, Workflows, Approvals), the second reads nCino lending " +
  "signals (Loans, Covenants, Checklists). These are different datasets " +
  "serving different detectors. Both passes use your authorised read-only " +
  "token and are logged in the audit trail.";

describe("DiscoveryRun Salesforce step tooltip (AT-314)", () => {
  it("exports the approved explanation text verbatim", () => {
    expect(SALESFORCE_DUAL_EXTRACTION_TOOLTIP).toBe(APPROVED_TEXT);
  });

  it("renders a keyboard-focusable info icon with an aria-label", async () => {
    render(<StepInfoTooltip text={SALESFORCE_DUAL_EXTRACTION_TOOLTIP} />);

    const trigger = screen.getByRole("button", { name: APPROVED_TEXT });
    expect(trigger).toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-label", APPROVED_TEXT);
    // Native title fallback also carries the exact text.
    expect(trigger).toHaveAttribute("title", APPROVED_TEXT);

    // Keyboard-focusable: a <button> is reachable via Tab.
    await userEvent.tab();
    expect(trigger).toHaveFocus();
  });

  it("exposes the explanation through an accessible role=tooltip element", () => {
    render(<StepInfoTooltip text={SALESFORCE_DUAL_EXTRACTION_TOOLTIP} />);

    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(APPROVED_TEXT);

    // Trigger is linked to the tooltip via aria-describedby.
    const trigger = screen.getByRole("button", { name: APPROVED_TEXT });
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
  });
});
