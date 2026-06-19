/**
 * CS-3 — ForgotPasswordPage unit tests.
 *
 * Mocks the forgotPassword() API wrapper and useTheme(). Verifies the email
 * input, submit gating, and that the SAME neutral confirmation is shown after a
 * successful submit regardless of the email (anti-enumeration — AC11 analog on
 * the FE).
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import ForgotPasswordPage from "../pages/ForgotPasswordPage";

// ── Mocks ─────────────────────────────────────────────────────────────────────

const mockForgotPassword = vi.fn();

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

vi.mock("../api/authApi", () => ({
  forgotPassword: (email: string) => mockForgotPassword(email),
}));

// ── Helper ────────────────────────────────────────────────────────────────────

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/forgot-password"]}>
      <ForgotPasswordPage />
    </MemoryRouter>
  );
}

function submitEmail(value: string) {
  fireEvent.change(screen.getByLabelText(/email/i), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: /send reset link/i }));
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("ForgotPasswordPage", () => {
  beforeEach(() => {
    mockForgotPassword.mockReset();
    mockForgotPassword.mockResolvedValue(undefined);
  });

  it("renders the email field and submit button", () => {
    renderPage();
    expect(screen.getByLabelText(/email/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /send reset link/i })).toBeTruthy();
  });

  it("disables submit until a valid email is entered", () => {
    renderPage();
    const btn = screen.getByRole("button", { name: /send reset link/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "not-an-email" } });
    expect(btn.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: "user@company.com" } });
    expect(btn.disabled).toBe(false);
  });

  it("calls forgotPassword() with a normalised (trimmed, lowercased) email", async () => {
    renderPage();
    submitEmail("  User@Company.COM  ");
    await waitFor(() => {
      expect(mockForgotPassword).toHaveBeenCalledWith("user@company.com");
    });
  });

  it("shows the neutral confirmation for a registered email", async () => {
    renderPage();
    submitEmail("known@company.com");
    await waitFor(() => {
      expect(screen.getByTestId("forgot-password-confirmation").textContent).toMatch(
        /if that email is registered, a reset link has been sent/i
      );
    });
  });

  it("shows the SAME confirmation for an unregistered email (no enumeration)", async () => {
    renderPage();
    submitEmail("nobody@nowhere.com");
    await waitFor(() => {
      expect(screen.getByTestId("forgot-password-confirmation").textContent).toMatch(
        /if that email is registered, a reset link has been sent/i
      );
    });
    // The form is replaced by the confirmation — the email field is gone.
    expect(screen.queryByLabelText(/email/i)).toBeNull();
  });

  it("shows a generic retry error only on a transport failure", async () => {
    mockForgotPassword.mockRejectedValue(new Error("network down"));
    renderPage();
    submitEmail("user@company.com");
    await waitFor(() => {
      expect(screen.getByTestId("forgot-password-error").textContent).toMatch(
        /something went wrong/i
      );
    });
    // Still on the form so the user can retry.
    expect(screen.getByLabelText(/email/i)).toBeTruthy();
  });
});
