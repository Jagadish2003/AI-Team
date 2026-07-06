/**
 * TopNav layout — org-name overflow guard
 *
 * Regression cover for the R17-D4 Addendum A header issue: once the dynamic
 * organisation name (T13, resolved from the license) was added next to the
 * AgentIQ logo, a long name (e.g. "City National Bank") pushed the primary nav
 * items off-screen on a 13" laptop — the first item ("Integration Hub") was
 * clipped underneath the org label.
 *
 * jsdom has no layout engine, so we can't assert pixel widths. Instead we lock
 * the CSS invariants that make the header resilient to any name length:
 *
 *   1. The org label ellipsizes (truncate + min-w-0) and lives inside the
 *      flex-1 brand group that absorbs the row's slack — so a long name shrinks
 *      instead of stealing the width the nav items need. The nav itself is NOT
 *      flex-1, so it keeps its natural width and every item stays visible.
 *   2. The full name is still available on hover (title attribute).
 *   3. All 8 nav items remain rendered even with a very long org name.
 *
 * Run:
 *   npx vitest run src/__tests__/TopNavLayout.test.tsx
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect } from "vitest";

const LONG_ORG_NAME = "City National Bank of Greater Metropolitan Area";

vi.mock("../context/RunContext", () => ({
  RunProvider: ({ children }: any) => <>{children}</>,
  useRunContext: () => ({ runId: null }),
}));

vi.mock("../context/ConnectorContext", () => ({
  ConnectorProvider: ({ children }: any) => <>{children}</>,
  useConnectorContext: () => ({ all: [] }),
}));

// Force a long, license-derived org name so the truncation guard is meaningful.
vi.mock("../context/LicenseContext", () => ({
  useOrgName: () => LONG_ORG_NAME,
}));

import TopNav from "../components/common/TopNav";

function renderNav() {
  return render(
    <MemoryRouter initialEntries={["/integration-hub"]}>
      <TopNav />
    </MemoryRouter>,
  );
}

describe("TopNav — org name never crowds out the nav", () => {
  it("renders the org label as a truncating, shrinkable element inside the flex-1 brand group", () => {
    renderNav();
    const label = screen.getByTestId("org-name-label");

    // The label itself must be able to shrink and ellipsize.
    expect(label.className).toContain("min-w-0");
    expect(label.className).toContain("truncate");

    // The mechanism: the parent brand group is flex-1 + min-w-0, so IT absorbs
    // the row's slack and truncates the name — the nav is left at natural width.
    // (Guards against reverting to a flex-1 nav, which clips the first item.)
    const brandGroup = label.parentElement;
    expect(brandGroup?.className).toContain("flex-1");
    expect(brandGroup?.className).toContain("min-w-0");
  });

  it("keeps the full org name available on hover via the title attribute", () => {
    renderNav();
    const label = screen.getByTestId("org-name-label");
    expect(label).toHaveAttribute("title", LONG_ORG_NAME);
    expect(label).toHaveTextContent(LONG_ORG_NAME);
  });

  it("still renders all 8 primary nav items alongside a long org name", () => {
    renderNav();
    const expectedItems = [
      "Integration Hub",
      "Stack Builder",
      "Discovery Run",
      "Source Intelligence",
      "Opportunity Review",
      "Agent Roadmap",
      "Agentforce Blueprint",
      "Executive Report",
    ];
    for (const label of expectedItems) {
      // getAllByText: labels appear in both the desktop and mobile nav lists.
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });
});
