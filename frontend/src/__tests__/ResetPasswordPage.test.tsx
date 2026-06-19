/**
 * CS-3 — ResetPasswordPage unit tests.
 *
 * Mocks resetPassword(), useTheme(), useToast(), and react-router's useNavigate
 * (keeping MemoryRouter + useSearchParams real so the ?token= param resolves).
 * Verifies token gating, the strength-gated submit, the success path (toast +
 * navigate to /login), and the 400 expired-token message.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import ResetPasswordPage from "../pages/ResetPasswordPage";
import { ApiError } from "../lib/apiClient";

// ── Mocks ─────────────────────────────────────────────────────────────────────

const mockResetPassword = vi.fn();
const mockPush = vi.fn();
const mockNavigate = vi.fn();

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

vi.mock("../components/common/Toast", () => ({
  useToast: () => ({ push: mockPush }),
}));

vi.mock("../api/authApi", () => ({
  resetPassword: (token: string, password: string) => mockResetPassword(token, password),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

// ── Helpers ────────────────────────────────────────────────────────────────────

const STRONG = "Password1!";

function renderWithToken(token = "valid-reset-token") {
  return render(
    <MemoryRouter initialEntries={[`/reset-password?token=${token}`]}>
      <ResetPasswordPage />
    </MemoryRouter>
  );
}

function renderWithoutToken() {
  return render(
    <MemoryRouter initialEntries={["/reset-password"]}>
      <ResetPasswordPage />
    </MemoryRouter>
  );
}

function fillPasswords(pw: string, confirm = pw) {
  const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
  fireEvent.change(passField, { target: { value: pw } });
  fireEvent.change(confirmField, { target: { value: confirm } });
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("ResetPasswordPage", () => {
  beforeEach(() => {
    mockResetPassword.mockReset();
    mockPush.mockReset();
    mockNavigate.mockReset();
  });

  // ── Missing token ──────────────────────────────────────────────────────────

  it("shows the invalid state (no form) when no token is present", () => {
    renderWithoutToken();
    expect(screen.getByTestId("reset-invalid-error")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /reset password/i })).toBeNull();
  });

  // ── Rendering with a token ───────────────────────────────────────────────────

  it("renders the password fields and strength indicator with a token", () => {
    renderWithToken();
    expect(screen.getAllByPlaceholderText("••••••••")).toHaveLength(2);
    expect(screen.getByTestId("password-strength")).toBeTruthy();
    expect(screen.getByRole("button", { name: /reset password/i })).toBeTruthy();
  });

  // ── Submit gating (strength indicator drives this — AC9 analog) ──────────────

  it("disables submit until the password is strong AND confirmed", () => {
    renderWithToken();
    const btn = screen.getByRole("button", { name: /reset password/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);

    // Strong but mismatched → still disabled.
    fillPasswords(STRONG, "Different1!");
    expect(btn.disabled).toBe(true);

    // Matched but weak → still disabled.
    fillPasswords("weak", "weak");
    expect(btn.disabled).toBe(true);

    // Strong + matched → enabled.
    fillPasswords(STRONG);
    expect(btn.disabled).toBe(false);
  });

  it("shows the mismatch hint when confirm differs", () => {
    renderWithToken();
    fillPasswords(STRONG, "Different1!");
    expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
  });

  // ── Success path ─────────────────────────────────────────────────────────────

  it("calls resetPassword() with the token + new password", async () => {
    mockResetPassword.mockResolvedValue(undefined);
    renderWithToken("my-token");
    fillPasswords(STRONG);
    fireEvent.click(screen.getByRole("button", { name: /reset password/i }));
    await waitFor(() => {
      expect(mockResetPassword).toHaveBeenCalledWith("my-token", STRONG);
    });
  });

  it("toasts and navigates to /login on success", async () => {
    mockResetPassword.mockResolvedValue(undefined);
    renderWithToken();
    fillPasswords(STRONG);
    fireEvent.click(screen.getByRole("button", { name: /reset password/i }));
    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith("Password reset. Please log in.", "success");
    });
    expect(mockNavigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  // ── Error paths ──────────────────────────────────────────────────────────────

  it("shows the expired-link message on 400 and does not navigate", async () => {
    mockResetPassword.mockRejectedValue(new ApiError("expired", 400, {}));
    renderWithToken();
    fillPasswords(STRONG);
    fireEvent.click(screen.getByRole("button", { name: /reset password/i }));
    await waitFor(() => {
      expect(screen.getByTestId("reset-password-error").textContent).toMatch(
        /this reset link has expired/i
      );
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("shows a weak-password message on 422", async () => {
    mockResetPassword.mockRejectedValue(new ApiError("weak", 422, {}));
    renderWithToken();
    fillPasswords(STRONG);
    fireEvent.click(screen.getByRole("button", { name: /reset password/i }));
    await waitFor(() => {
      expect(screen.getByTestId("reset-password-error").textContent).toMatch(
        /stronger password/i
      );
    });
  });

  it("shows a generic message for unexpected errors", async () => {
    mockResetPassword.mockRejectedValue(new Error("network"));
    renderWithToken();
    fillPasswords(STRONG);
    fireEvent.click(screen.getByRole("button", { name: /reset password/i }));
    await waitFor(() => {
      expect(screen.getByTestId("reset-password-error").textContent).toMatch(
        /something went wrong/i
      );
    });
  });
});
