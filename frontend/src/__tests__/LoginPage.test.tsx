/**
 * AT-239 — LoginPage unit tests.
 *
 * Tests the component in isolation by mocking:
 *   - useAuth() (the API boundary)
 *   - useTheme() (returns "light" by default)
 *   - react-router-dom/useNavigate
 *
 * We do NOT reach into the backend or real AuthContext.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import LoginPage from "../pages/LoginPage";
import { ApiError } from "../lib/apiClient";

// ── Mocks ─────────────────────────────────────────────────────────────────────

const mockLogin = vi.fn();
const mockNavigate = vi.fn();

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ login: mockLogin }),
}));

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => mockNavigate };
});

// ── Helper ────────────────────────────────────────────────────────────────────

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <LoginPage />
    </MemoryRouter>
  );
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("LoginPage", () => {
  beforeEach(() => {
    mockLogin.mockReset();
    mockNavigate.mockReset();
  });

  // ── Rendering ──────────────────────────────────────────────────────────────

  it("renders email and password fields", () => {
    renderPage();
    expect(screen.getByLabelText(/email/i)).toBeTruthy();
    expect(screen.getByLabelText(/password/i)).toBeTruthy();
  });

  it("renders the sign in button", () => {
    renderPage();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeTruthy();
  });

  it("renders a link to /register", () => {
    renderPage();
    const link = screen.getByRole("link", { name: /register/i });
    expect(link.getAttribute("href")).toBe("/register");
  });

  // ── Submit disabled state ──────────────────────────────────────────────────

  it("submit button is disabled when fields are empty", () => {
    renderPage();
    const btn = screen.getByRole("button", { name: /sign in/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit button is enabled when both fields are filled", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "secret" },
    });
    const btn = screen.getByRole("button", { name: /sign in/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  // ── Successful login ───────────────────────────────────────────────────────

  it("calls login() with trimmed+lowercased email and password", async () => {
    mockLogin.mockResolvedValue(undefined);
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "  USER@Example.COM  " },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "mypassword" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(mockLogin).toHaveBeenCalledWith("user@example.com", "mypassword");
    });
  });

  it("navigates to /integration-hub on successful login", async () => {
    mockLogin.mockResolvedValue(undefined);
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(mockNavigate).toHaveBeenCalledWith("/integration-hub", { replace: true });
    });
  });

  // ── Error handling ─────────────────────────────────────────────────────────

  it("shows 'Invalid email or password' on 401", async () => {
    mockLogin.mockRejectedValue(new ApiError("login failed", 401, {}));
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toMatch(
        /invalid email or password/i
      );
    });
  });

  it("shows rate-limit message on 429", async () => {
    mockLogin.mockRejectedValue(new ApiError("rate limited", 429, {}));
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toMatch(
        /too many failed attempts/i
      );
    });
  });

  it("shows generic error message for non-ApiError failures", async () => {
    mockLogin.mockRejectedValue(new Error("Network error"));
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toMatch(
        /something went wrong/i
      );
    });
  });

  it("does not navigate on login failure", async () => {
    mockLogin.mockRejectedValue(new ApiError("bad creds", 401, {}));
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/password/i), {
      target: { value: "wrong" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error")).toBeTruthy();
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});
