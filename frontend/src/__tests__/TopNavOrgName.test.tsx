/**
 * R17-D4 Addendum A §2 / T13 (AT-508) — TopNav organisation-name label.
 *
 * The header shows the license-resolved organisation name (the workspace label)
 * for every role, via the shared `useOrgName()` hook. Before any key is installed
 * it shows the neutral default (AC16); with a key it shows the resolved name
 * (AC15).
 *
 * Run: npx vitest run src/__tests__/TopNavOrgName.test.tsx
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({ orgName: "Teachers Credit Union" }));

vi.mock("../context/RunContext", () => ({
  useRunContext: () => ({ runId: null }),
}));

vi.mock("../context/ConnectorContext", () => ({
  useConnectorContext: () => ({ all: [] }),
}));

vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({ user: { email: "srivani@dwp.com", role: "owner" }, logout: vi.fn() }),
}));

vi.mock("../context/LicenseContext", () => ({
  useOrgName: () => h.orgName,
}));

import TopNav from "../components/common/TopNav";

function renderTopNav() {
  return render(
    <MemoryRouter initialEntries={["/integration-hub"]}>
      <TopNav />
    </MemoryRouter>,
  );
}

describe("TopNav organisation label", () => {
  beforeEach(() => {
    h.orgName = "Teachers Credit Union";
  });

  it("shows the license-resolved organisation name in the header (AC15)", () => {
    renderTopNav();
    expect(screen.getByTestId("org-name-label")).toHaveTextContent("Teachers Credit Union");
  });

  it("shows the neutral default when no key is installed (AC16)", () => {
    h.orgName = "Your Organisation";
    renderTopNav();
    expect(screen.getByTestId("org-name-label")).toHaveTextContent("Your Organisation");
  });
});
