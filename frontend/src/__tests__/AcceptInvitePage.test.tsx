/**
 * AT-239 — AcceptInvitePage unit tests.
 *
 * Mocks: useAuth(), useTheme(), useNavigate(), and getInviteInfo() (the
 * mount-time token resolver). A token now gates the form behind a GET
 * /api/auth/invite-info call, so valid-token tests resolve that mock and await
 * the form before interacting.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import AcceptInvitePage from "../pages/AcceptInvitePage";
import { ApiError } from "../lib/apiClient";

// ── Mocks ─────────────────────────────────────────────────────────────────────

const mockAcceptInvite = vi.fn();
const mockNavigate = vi.fn();
const mockGetInviteInfo = vi.fn();

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ acceptInvite: mockAcceptInvite }),
}));

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

vi.mock("../api/authApi", () => ({
  getInviteInfo: (token: string) => mockGetInviteInfo(token),
}));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

// ── Helpers ────────────────────────────────────────────────────────────────────

function renderWithToken(token = "valid-invite-token") {
  return render(
    <MemoryRouter initialEntries={[`/accept-invite?token=${token}`]}>
      <AcceptInvitePage />
    </MemoryRouter>
  );
}

function renderWithoutToken() {
  return render(
    <MemoryRouter initialEntries={["/accept-invite"]}>
      <AcceptInvitePage />
    </MemoryRouter>
  );
}

/** Render a valid-token page and wait for the password form to appear. */
async function renderActiveForm(token = "valid-invite-token") {
  renderWithToken(token);
  await screen.findByRole("button", { name: /activate account/i });
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("AcceptInvitePage", () => {
  beforeEach(() => {
    mockAcceptInvite.mockReset();
    mockNavigate.mockReset();
    mockGetInviteInfo.mockReset();
    // Default: a valid token resolving to an org name.
    mockGetInviteInfo.mockResolvedValue({
      org_name: "DWP",
      email: "analyst@dwp.com",
      role: "analyst",
    });
  });

  // ── Missing token ──────────────────────────────────────────────────────────

  it("shows the invalid state when no token query param is present", () => {
    renderWithoutToken();
    expect(screen.getByTestId("invite-invalid-error")).toBeTruthy();
    expect(mockGetInviteInfo).not.toHaveBeenCalled();
  });

  it("does not render the password form when token is missing", () => {
    renderWithoutToken();
    expect(screen.queryByLabelText(/^password$/i)).toBeNull();
  });

  // ── Token resolution on mount ───────────────────────────────────────────────

  it("resolves the token on mount and greets with the org name", async () => {
    await renderActiveForm("my-invite-token");
    expect(mockGetInviteInfo).toHaveBeenCalledWith("my-invite-token");
    expect(
      screen.getByText(/You have been invited to DWP's AgentIQ/i)
    ).toBeTruthy();
  });

  it("falls back to a generic greeting when org_name is null", async () => {
    mockGetInviteInfo.mockResolvedValue({ org_name: null, email: null, role: "analyst" });
    await renderActiveForm();
    expect(screen.getByText(/You have been invited to AgentIQ\./i)).toBeTruthy();
  });

  it("shows the invalid/used state (no form) when the token resolves to 400 on load", async () => {
    // Reusing an already-activated token: the page must NOT show the empty form.
    mockGetInviteInfo.mockRejectedValue(new ApiError("used", 400, {}));
    renderWithToken("already-used-token");

    await waitFor(() => {
      expect(screen.getByTestId("invite-invalid-error").textContent).toMatch(
        /invalid, expired, or has already been used/i
      );
    });
    expect(screen.queryByRole("button", { name: /activate account/i })).toBeNull();
  });

  // ── Rendering with a valid token ────────────────────────────────────────────

  it("renders password and confirm password fields when token is valid", async () => {
    await renderActiveForm();
    expect(screen.getAllByPlaceholderText("••••••••")).toHaveLength(2);
  });

  it("renders the Activate account button", async () => {
    await renderActiveForm();
    expect(screen.getByRole("button", { name: /activate account/i })).toBeTruthy();
  });

  // ── Password show/hide toggle (theme parity with Login/Register) ─────────────

  it("exposes show/hide toggles for the password fields", async () => {
    await renderActiveForm();
    expect(screen.getAllByRole("button", { name: /show password/i })).toHaveLength(2);
  });

  // ── Disabled state ─────────────────────────────────────────────────────────

  it("submit button is disabled when fields are empty", async () => {
    await renderActiveForm();
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit button is disabled when passwords do not match", async () => {
    await renderActiveForm();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "password123" } });
    fireEvent.change(confirmField, { target: { value: "different!" } });
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit button is disabled when password is shorter than 8 chars", async () => {
    await renderActiveForm();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "short" } });
    fireEvent.change(confirmField, { target: { value: "short" } });
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("shows the length hint while the password is too short", async () => {
    await renderActiveForm();
    const [passField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "short" } });
    expect(screen.getByText(/minimum of 8 characters/i)).toBeTruthy();
  });

  it("submit button is enabled with valid matching passwords", async () => {
    await renderActiveForm();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "password123" } });
    fireEvent.change(confirmField, { target: { value: "password123" } });
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  // ── Successful activation ──────────────────────────────────────────────────

  it("calls acceptInvite() with the invite token and password", async () => {
    mockAcceptInvite.mockResolvedValue(undefined);
    await renderActiveForm("my-invite-token");

    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "newpassword" } });
    fireEvent.change(confirmField, { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => {
      expect(mockAcceptInvite).toHaveBeenCalledWith("my-invite-token", "newpassword");
    });
  });

  it("navigates to /integration-hub on success", async () => {
    mockAcceptInvite.mockResolvedValue(undefined);
    await renderActiveForm();

    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "newpassword" } });
    fireEvent.change(confirmField, { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith("/integration-hub", { replace: true });
    });
  });

  // ── Error handling on submit ─────────────────────────────────────────────────

  it("shows invalid/expired message on 400", async () => {
    mockAcceptInvite.mockRejectedValue(new ApiError("bad token", 400, {}));
    await renderActiveForm();

    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "newpassword" } });
    fireEvent.change(confirmField, { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("accept-invite-error").textContent).toMatch(
        /invalid, expired, or has already been used/i
      );
    });
  });

  it("shows validation message on 422", async () => {
    mockAcceptInvite.mockRejectedValue(new ApiError("unprocessable", 422, {}));
    await renderActiveForm();

    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "newpassword" } });
    fireEvent.change(confirmField, { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("accept-invite-error").textContent).toMatch(
        /check your password/i
      );
    });
  });

  it("shows generic message for unexpected errors", async () => {
    mockAcceptInvite.mockRejectedValue(new Error("Network error"));
    await renderActiveForm();

    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "newpassword" } });
    fireEvent.change(confirmField, { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("accept-invite-error").textContent).toMatch(
        /something went wrong/i
      );
    });
  });

  it("does not navigate when activation fails", async () => {
    mockAcceptInvite.mockRejectedValue(new ApiError("bad token", 400, {}));
    await renderActiveForm();

    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "newpassword" } });
    fireEvent.change(confirmField, { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("accept-invite-error")).toBeTruthy();
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  // ── Password mismatch feedback ──────────────────────────────────────────────

  it("shows mismatch error when confirm password differs", async () => {
    await renderActiveForm();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "password123" } });
    fireEvent.change(confirmField, { target: { value: "different!" } });
    expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
  });
});
