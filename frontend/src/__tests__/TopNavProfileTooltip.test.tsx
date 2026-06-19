import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockLogout = vi.fn();
let mockAuthUser: any = null;

vi.mock("../context/RunContext", () => ({
  useRunContext: () => ({ runId: null }),
}));

vi.mock("../context/ConnectorContext", () => ({
  useConnectorContext: () => ({ all: [] }),
}));

vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({ user: mockAuthUser, logout: mockLogout }),
}));

import TopNav from "../components/common/TopNav";

function renderTopNav() {
  return render(
    <MemoryRouter initialEntries={["/integration-hub"]}>
      <TopNav />
    </MemoryRouter>
  );
}

describe("TopNav profile tooltip", () => {
  beforeEach(() => {
    mockLogout.mockReset();
    mockAuthUser = null;
  });

  it("uses the email name before @ instead of the organization name", () => {
    mockAuthUser = {
      id: "u-1",
      email: "srivani@dwp.com",
      role: "owner",
      org_id: "org-dwp",
      org_name: "DWP",
    };

    renderTopNav();

    expect(screen.getByRole("button", { name: "Srivani's Profile" })).toBeTruthy();
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent("Srivani's Profile");
    expect(tooltip).toHaveClass("bg-panel", "text-text", "border-border");
    expect(screen.queryByText("DWP's Profile")).toBeNull();
  });

  it("falls back to a generic profile label when no email is loaded", () => {
    renderTopNav();

    expect(screen.getByRole("button", { name: "Profile" })).toBeTruthy();
    expect(screen.getByText("Profile")).toBeTruthy();
  });
});
