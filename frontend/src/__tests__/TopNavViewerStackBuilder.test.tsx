/**
 * TopNavViewerStackBuilder.test.tsx
 *
 * Viewers get a read-only experience: the Stack Builder nav entry (an analyst+
 * write workflow) is hidden for them, while analysts and owners still see it.
 * The backend still enforces the boundary regardless of what the nav renders.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, it, expect, vi } from "vitest";

// The role under test — mutated per test before rendering.
const h: { role: "owner" | "analyst" | "viewer" } = { role: "viewer" };

vi.mock("../context/RunContext", () => ({
  useRunContext: () => ({ runId: null }),
}));
vi.mock("../context/ConnectorContext", () => ({
  useConnectorContext: () => ({ all: [] }),
}));
vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn() }),
}));
vi.mock("../context/LicenseContext", () => ({
  useOrgName: () => "Test Org",
}));
vi.mock("../context/AuthContext", () => ({
  useAuthOptional: () => ({
    user: { email: "likhith@dwp.com", role: h.role },
    logout: vi.fn(),
  }),
}));

import TopNav from "../components/common/TopNav";

function renderNav() {
  return render(
    <MemoryRouter initialEntries={["/integration-hub"]}>
      <TopNav />
    </MemoryRouter>,
  );
}

describe("TopNav — Stack Builder visibility by role", () => {
  it("hides the Stack Builder nav entry for a viewer", () => {
    h.role = "viewer";
    renderNav();
    expect(screen.queryByRole("link", { name: /stack builder/i })).toBeNull();
    // A shared read-only surface (Integration Hub) is still present.
    expect(
      screen.getAllByRole("link", { name: /integration hub/i }).length,
    ).toBeGreaterThan(0);
  });

  it("shows the Stack Builder nav entry for an analyst", () => {
    h.role = "analyst";
    renderNav();
    expect(
      screen.getAllByRole("link", { name: /stack builder/i }).length,
    ).toBeGreaterThan(0);
  });

  it("shows the Stack Builder nav entry for an owner", () => {
    h.role = "owner";
    renderNav();
    expect(
      screen.getAllByRole("link", { name: /stack builder/i }).length,
    ).toBeGreaterThan(0);
  });
});
