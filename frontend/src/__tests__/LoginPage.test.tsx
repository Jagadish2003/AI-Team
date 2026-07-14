/**
 * AT-239 — LoginPage unit tests.
 *
 * Tests the component in isolation by mocking:
 *   - useAuth() (the API boundary)
 *   - useTheme() (returns "light" by default)
 *   - react-router-dom useNavigate (the SPA redirect after a successful login;
 *     the data-provider subtree is remounted by App.tsx's token-keyed
 *     SessionBoundary, so no full document reload is needed anymore)
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

// Partial-mock so MemoryRouter/Link keep working; only useNavigate is stubbed
// (the post-login SPA redirect target we assert on).
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
    expect(screen.getByLabelText("Password")).toBeTruthy();
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

  it("renders a 'Forgot password?' link to /forgot-password (CS-3 AC13)", () => {
    renderPage();
    const link = screen.getByRole("link", { name: /forgot password/i });
    expect(link.getAttribute("href")).toBe("/forgot-password");
  });

  // ── Show/hide password ───────────────────────────────────────────────────────

  it("toggles password visibility via the eye button", () => {
    renderPage();
    const pwd = screen.getByLabelText("Password") as HTMLInputElement;
    expect(pwd.type).toBe("password");

    fireEvent.click(screen.getByRole("button", { name: /show password/i }));
    expect(pwd.type).toBe("text");

    fireEvent.click(screen.getByRole("button", { name: /hide password/i }));
    expect(pwd.type).toBe("password");
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
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
    });
    const btn = screen.getByRole("button", { name: /sign in/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  // ── Inline validation (mirrors RegisterPage) ─────────────────────────────────

  it("shows an email error and disables submit for an invalid email format", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "not-an-email" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
    });
    expect(screen.getByText(/valid email address/i)).toBeTruthy();
    const btn = screen.getByRole("button", { name: /sign in/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("shows a length error and disables submit when the password is under 8 chars", () => {
    renderPage();
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "short" },
    });
    expect(screen.getByText(/enter minimum of 8 characters/i)).toBeTruthy();
    const btn = screen.getByRole("button", { name: /sign in/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  // ── Successful login ───────────────────────────────────────────────────────

  it("calls login() with trimmed+lowercased email and password", async () => {
    mockLogin.mockResolvedValue(undefined);
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "  USER@Example.COM  " },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "mypassword" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(mockLogin).toHaveBeenCalledWith("user@example.com", "mypassword");
    });
  });

  it("navigates into /integration-hub on successful login", async () => {
    mockLogin.mockResolvedValue(undefined);
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
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
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrongpass1" },
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
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toMatch(
        /too many failed attempts/i
      );
    });
  });

  it("shows the remaining wait time from the 429 retry_after (rounded up to minutes)", async () => {
    // 120s → "2 minutes". Backend nests retry_after under `detail`.
    mockLogin.mockRejectedValue(
      new ApiError("rate limited", 429, {
        detail: { message: "Too many failed attempts.", retry_after: 120 },
      })
    );
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toBe(
        "Too many failed attempts. Wait for 2 minutes."
      );
    });
  });

  it("rounds a partial-minute retry_after up and uses the singular 'minute'", async () => {
    // 30s → ceil → "1 minute" (singular).
    mockLogin.mockRejectedValue(
      new ApiError("rate limited", 429, { detail: { retry_after: 30 } })
    );
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toBe(
        "Too many failed attempts. Wait for 1 minute."
      );
    });
  });

  it("shows generic error message for non-ApiError failures", async () => {
    mockLogin.mockRejectedValue(new Error("Network error"));
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toMatch(
        /something went wrong/i
      );
    });
  });

  it("does not redirect on login failure", async () => {
    mockLogin.mockRejectedValue(new ApiError("bad creds", 401, {}));
    renderPage();

    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "wrongpass1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByTestId("login-error")).toBeTruthy();
    });
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  // ── AUTH-2 T7: pending / rejected org states ─────────────────────────────────

  function submitValidLogin() {
    fireEvent.change(screen.getByLabelText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "password1" },
    });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
  }

  it("renders the awaiting-approval message on a 403 org_pending_approval (T7-AC1)", async () => {
    mockLogin.mockRejectedValue(
      new ApiError("forbidden", 403, {
        detail: { message: "pending", error_code: "org_pending_approval" },
      })
    );
    renderPage();
    submitValidLogin();

    await waitFor(() => {
      expect(screen.getByTestId("login-pending").textContent).toBe(
        "Your organisation is awaiting approval."
      );
    });
    // Distinct from the bad-credentials error component.
    expect(screen.queryByTestId("login-error")).toBeNull();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("renders the registration-rejected message on a 403 org_rejected (T7-AC2)", async () => {
    mockLogin.mockRejectedValue(
      new ApiError("forbidden", 403, {
        detail: { message: "rejected", error_code: "org_rejected" },
      })
    );
    renderPage();
    submitValidLogin();

    await waitFor(() => {
      expect(screen.getByTestId("login-rejected").textContent).toBe(
        "This registration was not approved. Contact your CloudFulcrum representative."
      );
    });
    expect(screen.queryByTestId("login-error")).toBeNull();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("renders the three error states via distinct components (T7-AC3)", async () => {
    // Bad credentials → login-error (red).
    mockLogin.mockRejectedValue(new ApiError("bad creds", 401, {}));
    const { unmount: unmountInvalid } = renderPage();
    submitValidLogin();
    await waitFor(() => expect(screen.getByTestId("login-error")).toBeTruthy());
    expect(screen.queryByTestId("login-pending")).toBeNull();
    expect(screen.queryByTestId("login-rejected")).toBeNull();
    unmountInvalid();

    // Pending → login-pending (amber), and not the other two.
    mockLogin.mockReset();
    mockLogin.mockRejectedValue(
      new ApiError("forbidden", 403, { detail: { error_code: "org_pending_approval" } })
    );
    const { unmount: unmountPending } = renderPage();
    submitValidLogin();
    await waitFor(() => expect(screen.getByTestId("login-pending")).toBeTruthy());
    expect(screen.queryByTestId("login-error")).toBeNull();
    expect(screen.queryByTestId("login-rejected")).toBeNull();
    unmountPending();

    // Rejected → login-rejected (slate), and not the other two.
    mockLogin.mockReset();
    mockLogin.mockRejectedValue(
      new ApiError("forbidden", 403, { detail: { error_code: "org_rejected" } })
    );
    renderPage();
    submitValidLogin();
    await waitFor(() => expect(screen.getByTestId("login-rejected")).toBeTruthy());
    expect(screen.queryByTestId("login-error")).toBeNull();
    expect(screen.queryByTestId("login-pending")).toBeNull();
  });

  it("falls back to the red error box for a 403 without a known error_code (no regression)", async () => {
    mockLogin.mockRejectedValue(new ApiError("forbidden", 403, {}));
    renderPage();
    submitValidLogin();

    await waitFor(() => {
      expect(screen.getByTestId("login-error").textContent).toMatch(
        /something went wrong/i
      );
    });
  });
});
