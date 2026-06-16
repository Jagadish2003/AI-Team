/**
 * CS-3 — ResetPasswordPage unit tests.
 *
 * Mocks: resetPassword() API, useTheme(), useToast(), and react-router's
 * useNavigate(). The reset token is supplied through the MemoryRouter query
 * string, so useSearchParams() stays real.
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

vi.mock("../api/authApi", () => ({
  resetPassword: (token: string, password: string) =>
    mockResetPassword(token, password),
}));

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

vi.mock("../components/common/Toast", () => ({
  useToast: () => ({ push: mockPush }),
}));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

// ── Helpers ────────────────────────────────────────────────────────────────────

function renderWithToken(token = "valid-reset-token") {
  return render(
    <MemoryRouter initialEntries={[`/reset-password?token=${token}`]}>
      <ResetPasswordPage />
    </MemoryRouter>
  );
}

function fillPasswords(password: string, confirm = password) {
  const [passField, confirmField] = screen.getAllByPlaceholderText("••••••••");
  fireEvent.change(passField, { target: { value: password } });
  fireEvent.change(confirmField, { target: { value: confirm } });
}

function submitButton() {
  return screen.getByRole("button", { name: /reset password/i }) as HTMLButtonElement;
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("ResetPasswordPage", () => {
  beforeEach(() => {
    mockResetPassword.mockReset();
    mockPush.mockReset();
    mockNavigate.mockReset();
  });

  // ── Missing token ──────────────────────────────────────────────────────────

  it("shows the invalid state and no form when the token is missing", () => {
    render(
      <MemoryRouter initialEntries={["/reset-password"]}>
        <ResetPasswordPage />
      </MemoryRouter>
    );
    expect(screen.getByTestId("reset-invalid-error")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /reset password/i })).toBeNull();
  });

  // ── Rendering with a token ───────────────────────────────────────────────────

  it("renders the new-password form when a token is present", () => {
    renderWithToken();
    expect(screen.getAllByPlaceholderText("••••••••")).toHaveLength(2);
    expect(submitButton()).toBeTruthy();
  });

  it("renders the four-requirement strength indicator", () => {
    renderWithToken();
    expect(screen.getAllByTestId("password-requirement")).toHaveLength(4);
  });

  // ── Submit gating ────────────────────────────────────────────────────────────

  it("disables submit until all four requirements are met", () => {
    renderWithToken();
    expect(submitButton().disabled).toBe(true); // empty

    fillPasswords("password"); // 8 chars, lowercase only
    expect(submitButton().disabled).toBe(true);

    fillPasswords("Password1!");
    expect(submitButton().disabled).toBe(false);
  });

  it("disables submit when the passwords do not match", () => {
    renderWithToken();
    fillPasswords("Password1!", "Password2!");
    expect(submitButton().disabled).toBe(true);
    expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
  });

  // ── Successful reset ─────────────────────────────────────────────────────────

  it("posts the token + new password, toasts, and navigates to /login on success", async () => {
    mockResetPassword.mockResolvedValue(undefined);
    renderWithToken("my-reset-token");
    fillPasswords("Password1!");
    fireEvent.click(submitButton());

    await waitFor(() => {
      expect(mockResetPassword).toHaveBeenCalledWith("my-reset-token", "Password1!");
    });
    expect(mockPush).toHaveBeenCalledWith("Password reset. Please log in.", "success");
    expect(mockNavigate).toHaveBeenCalledWith("/login");
  });

  // ── Error handling ───────────────────────────────────────────────────────────

  it("shows an expired-link message on 400 and does not navigate", async () => {
    mockResetPassword.mockRejectedValue(new ApiError("expired", 400, {}));
    renderWithToken();
    fillPasswords("Password1!");
    fireEvent.click(submitButton());

    await waitFor(() => {
      expect(screen.getByTestId("reset-password-error").textContent).toMatch(
        /reset link has expired/i
      );
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("shows a validation message on 422", async () => {
    mockResetPassword.mockRejectedValue(new ApiError("weak", 422, {}));
    renderWithToken();
    fillPasswords("Password1!");
    fireEvent.click(submitButton());

    await waitFor(() => {
      expect(screen.getByTestId("reset-password-error").textContent).toMatch(
        /check your password/i
      );
    });
  });
});
