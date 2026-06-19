/**
 * LicensePage (LIC-1 / T8 / AT-349) tests.
 *
 * Covers:
 *   - the three status badge states (valid / grace / read-only)
 *   - the valid update flow (key stored → success toast → status refreshed)
 *   - the invalid update flow ("This key is not valid" toast, status untouched)
 *   - Owner-only gating (Analyst/Viewer never see the page)
 *
 * The API boundary (api/licenseApi) and the auth/toast contexts are mocked — no
 * backend is touched.
 *
 * Run: npx vitest run src/__tests__/LicensePage.test.tsx
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";
import type { LicenseStatusResponse } from "../types/license";

const h = vi.hoisted(() => ({
  mockFetch: vi.fn(),
  mockUpdate: vi.fn(),
  mockPush: vi.fn(),
  role: { current: "owner" as "owner" | "analyst" | "viewer" },
}));

vi.mock("../api/licenseApi", () => ({
  fetchLicenseStatus: (...a: unknown[]) => h.mockFetch(...a),
  updateLicenseKey: (...a: unknown[]) => h.mockUpdate(...a),
}));

vi.mock("../components/common/Toast", () => ({
  useToast: () => ({ push: h.mockPush }),
}));

vi.mock("../components/common/TopNav", () => ({
  default: () => <nav data-testid="top-nav" />,
}));

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { role: h.role.current } }),
}));

// ApiError must be the real class so `instanceof ApiError` works in the page.
import { ApiError } from "../lib/apiClient";
import LicensePage from "../pages/LicensePage";

const VALID: LicenseStatusResponse = {
  status: "valid",
  customer: "City National Bank",
  term: 12,
  expires_at: "2027-06-18",
  days_remaining: 200,
};

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/license"]}>
      <Routes>
        <Route path="/license" element={<LicensePage />} />
        <Route path="/integration-hub" element={<div>integration hub</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  h.role.current = "owner";
  h.mockFetch.mockResolvedValue(VALID);
  h.mockUpdate.mockResolvedValue(VALID);
});

describe("status badge", () => {
  it("shows a green Valid badge within term", async () => {
    h.mockFetch.mockResolvedValue({ ...VALID, status: "valid" });
    renderPage();
    const badge = await screen.findByTestId("license-status-badge");
    expect(badge).toHaveTextContent("Valid");
    expect(badge).toHaveAttribute("data-tone", "green");
  });

  it("shows an amber Grace badge", async () => {
    h.mockFetch.mockResolvedValue({ ...VALID, status: "grace", days_remaining: -3 });
    renderPage();
    const badge = await screen.findByTestId("license-status-badge");
    expect(badge).toHaveTextContent("Grace");
    expect(badge).toHaveAttribute("data-tone", "amber");
  });

  it("shows a red Read-only badge", async () => {
    h.mockFetch.mockResolvedValue({ ...VALID, status: "readonly", days_remaining: -30 });
    renderPage();
    const badge = await screen.findByTestId("license-status-badge");
    expect(badge).toHaveTextContent("Read-only");
    expect(badge).toHaveAttribute("data-tone", "red");
  });

  it("renders the issued-to / term / expiry / days-remaining details (AC3)", async () => {
    renderPage();
    expect(await screen.findByText("City National Bank")).toBeInTheDocument();
    expect(screen.getByText("12 months")).toBeInTheDocument();
    expect(screen.getByText("2027-06-18")).toBeInTheDocument();
    expect(screen.getByText("200")).toBeInTheDocument();
  });
});

describe("update key flow", () => {
  it("valid key: stores, toasts success, and refreshes status (AC7)", async () => {
    h.mockFetch.mockResolvedValue({ ...VALID, status: "grace", days_remaining: -1 });
    const RENEWED: LicenseStatusResponse = {
      status: "valid",
      customer: "City National Bank",
      term: 12,
      expires_at: "2028-06-18",
      days_remaining: 365,
    };
    h.mockUpdate.mockResolvedValue(RENEWED);

    renderPage();
    const field = await screen.findByLabelText("License key");
    fireEvent.change(field, { target: { value: "newpayload.newsig" } });
    fireEvent.click(screen.getByRole("button", { name: /update key/i }));

    await waitFor(() => expect(h.mockUpdate).toHaveBeenCalledWith("newpayload.newsig"));
    expect(h.mockPush).toHaveBeenCalledWith("License updated.", "success");
    // Status refreshed immediately, no restart.
    await waitFor(() => {
      const badge = screen.getByTestId("license-status-badge");
      expect(badge).toHaveTextContent("Valid");
    });
    expect(screen.getByText("2028-06-18")).toBeInTheDocument();
  });

  it("invalid key: shows 'This key is not valid' and does not change status", async () => {
    h.mockUpdate.mockRejectedValue(new ApiError("bad", 400, { detail: "This key is not valid" }));

    renderPage();
    const field = await screen.findByLabelText("License key");
    fireEvent.change(field, { target: { value: "garbage" } });
    fireEvent.click(screen.getByRole("button", { name: /update key/i }));

    await waitFor(() => expect(h.mockPush).toHaveBeenCalledWith("This key is not valid", "error"));
    // Original status remains.
    const badge = screen.getByTestId("license-status-badge");
    expect(badge).toHaveTextContent("Valid");
  });
});

describe("owner-only gating (AC9)", () => {
  it("redirects an analyst away and never loads license data", async () => {
    h.role.current = "analyst";
    renderPage();
    expect(await screen.findByText("integration hub")).toBeInTheDocument();
    expect(screen.queryByTestId("license-status-badge")).not.toBeInTheDocument();
    expect(h.mockFetch).not.toHaveBeenCalled();
  });

  it("redirects a viewer away", async () => {
    h.role.current = "viewer";
    renderPage();
    expect(await screen.findByText("integration hub")).toBeInTheDocument();
    expect(h.mockFetch).not.toHaveBeenCalled();
  });
});
