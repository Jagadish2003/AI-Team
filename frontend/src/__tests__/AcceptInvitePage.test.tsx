/**
 * AT-239 — AcceptInvitePage unit tests.
 *
 * Mocks: useAuth(), useTheme(), useNavigate().
 * Uses MemoryRouter with ?token=<value> query param as the component reads it
 * via useSearchParams().
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import AcceptInvitePage from "../pages/AcceptInvitePage";
import { ApiError } from "../lib/apiClient";

// ── Mocks ─────────────────────────────────────────────────────────────────────

const mockAcceptInvite = vi.fn();
const mockNavigate = vi.fn();

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ acceptInvite: mockAcceptInvite }),
}));

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
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

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("AcceptInvitePage", () => {
  beforeEach(() => {
    mockAcceptInvite.mockReset();
    mockNavigate.mockReset();
  });

  // ── Missing token ──────────────────────────────────────────────────────────

  it("shows error state when no token query param is present", () => {
    renderWithoutToken();
    expect(screen.getByTestId("missing-token-error")).toBeTruthy();
  });

  it("does not render the password form when token is missing", () => {
    renderWithoutToken();
    expect(screen.queryByLabelText(/^password$/i)).toBeNull();
  });

  // ── Rendering with token ──────────────────────────────────────────────────

  it("renders password and confirm password fields when token is present", () => {
    renderWithToken();
    expect(screen.getAllByPlaceholderText("••••••••")).toHaveLength(2);
  });

  it("renders the Activate account button", () => {
    renderWithToken();
    expect(screen.getByRole("button", { name: /activate account/i })).toBeTruthy();
  });

  // ── Disabled state ─────────────────────────────────────────────────────────

  it("submit button is disabled when fields are empty", () => {
    renderWithToken();
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit button is disabled when passwords do not match", () => {
    renderWithToken();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "password123" } });
    fireEvent.change(confirmField, { target: { value: "different!" } });
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit button is disabled when password is shorter than 8 chars", () => {
    renderWithToken();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "short" } });
    fireEvent.change(confirmField, { target: { value: "short" } });
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit button is enabled with valid matching passwords", () => {
    renderWithToken();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "password123" } });
    fireEvent.change(confirmField, { target: { value: "password123" } });
    const btn = screen.getByRole("button", { name: /activate account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  // ── Successful activation ──────────────────────────────────────────────────

  it("calls acceptInvite() with the invite token and password", async () => {
    mockAcceptInvite.mockResolvedValue(undefined);
    renderWithToken("my-invite-token");

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
    renderWithToken();

    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "newpassword" } });
    fireEvent.change(confirmField, { target: { value: "newpassword" } });
    fireEvent.click(screen.getByRole("button", { name: /activate account/i }));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith("/integration-hub", { replace: true });
    });
  });

  // ── Error handling ─────────────────────────────────────────────────────────

  it("shows invalid/expired message on 400", async () => {
    mockAcceptInvite.mockRejectedValue(new ApiError("bad token", 400, {}));
    renderWithToken();

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
    renderWithToken();

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
    renderWithToken();

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
    renderWithToken();

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

  it("shows mismatch error when confirm password differs", () => {
    renderWithToken();
    const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(passField, { target: { value: "password123" } });
    fireEvent.change(confirmField, { target: { value: "different!" } });
    expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
  });
});
