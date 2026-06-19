// @vitest-environment jsdom
/**
 * AUTH-2 T6 — PendingApprovalPage unit tests.
 *
 * Verifies the static post-registration confirmation: exact message (T6-AC2),
 * no auto-redirect and no polling (T6-AC4). useTheme is mocked; the real
 * react-router Link is exercised inside a MemoryRouter.
 */
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { vi, describe, it, expect, afterEach } from "vitest";

import PendingApprovalPage from "../pages/PendingApprovalPage";

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/pending-approval"]}>
      <Routes>
        <Route path="/pending-approval" element={<PendingApprovalPage />} />
        {/* Sentinel: if the page auto-redirected here, this text would appear. */}
        <Route path="/login" element={<div>LOGIN ROUTE</div>} />
      </Routes>
    </MemoryRouter>
  );
}

describe("PendingApprovalPage (AUTH-2 T6)", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders the exact pending-approval message (T6-AC2)", () => {
    renderPage();
    expect(
      screen.getByText(
        "Your organisation has been submitted for approval. You will receive an email once it is reviewed."
      )
    ).toBeTruthy();
  });

  it("does not auto-redirect — it stays on the page (T6-AC4)", () => {
    renderPage();
    // The page renders; it does NOT navigate to /login on its own.
    expect(screen.getByText(/submitted for approval/i)).toBeTruthy();
    expect(screen.queryByText("LOGIN ROUTE")).toBeNull();
  });

  it("offers only a manual sign-in link (T6-AC4)", () => {
    renderPage();
    const link = screen.getByRole("link", { name: /back to sign in/i });
    expect(link.getAttribute("href")).toBe("/login");
  });

  it("schedules no polling interval (T6-AC4)", () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    renderPage();
    expect(setIntervalSpy).not.toHaveBeenCalled();
  });
});
