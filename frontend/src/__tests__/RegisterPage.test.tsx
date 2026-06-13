/**
 * AT-239 — RegisterPage unit tests.
 *
 * Mocks: useAuth(), useTheme(), utils/navigation/hardRedirect().
 * Does NOT reach into backend or real AuthContext.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi, describe, it, expect, beforeEach } from "vitest";

import RegisterPage from "../pages/RegisterPage";
import { ApiError } from "../lib/apiClient";

// ── Mocks ─────────────────────────────────────────────────────────────────────

const mockRegister = vi.fn();
const mockHardRedirect = vi.fn();

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ register: mockRegister }),
}));

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ theme: "light" }),
}));

vi.mock("../utils/navigation", () => ({
  hardRedirect: (path: string) => mockHardRedirect(path),
}));

// ── Helper ────────────────────────────────────────────────────────────────────

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/register"]}>
      <RegisterPage />
    </MemoryRouter>
  );
}

function fillForm(
  orgName = "Acme Corp",
  email = "user@example.com",
  password = "password123",
  confirmPassword?: string
) {
  fireEvent.change(screen.getByLabelText(/organisation name/i), {
    target: { value: orgName },
  });
  fireEvent.change(screen.getByLabelText(/^email/i), {
    target: { value: email },
  });
  // Get the first password field (Password label)
  const [passField] = screen.getAllByPlaceholderText("••••••••");
  fireEvent.change(passField, { target: { value: password } });
  const [, confirmField] = screen.getAllByPlaceholderText("••••••••");
  fireEvent.change(confirmField, {
    target: { value: confirmPassword ?? password },
  });
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("RegisterPage", () => {
  beforeEach(() => {
    mockRegister.mockReset();
    mockHardRedirect.mockReset();
  });

  // ── Rendering ──────────────────────────────────────────────────────────────

  it("renders all four fields", () => {
    renderPage();
    expect(screen.getByLabelText(/organisation name/i)).toBeTruthy();
    expect(screen.getByLabelText(/^email/i)).toBeTruthy();
    // Two password placeholders
    expect(screen.getAllByPlaceholderText("••••••••")).toHaveLength(2);
  });

  it("renders a link to /login", () => {
    renderPage();
    const link = screen.getByRole("link", { name: /sign in/i });
    expect(link.getAttribute("href")).toBe("/login");
  });

  // ── Email format validation ──────────────────────────────────────────────────

  it("shows an error and disables submit for an invalid email format", () => {
    renderPage();
    fillForm("Acme Corp", "not-an-email", "password123");
    expect(screen.getByText(/valid email address/i)).toBeTruthy();
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("accepts a dotted-local-part email such as abc.m@xy.org", () => {
    renderPage();
    fillForm("Acme Corp", "abc.m@xy.org", "password123");
    expect(screen.queryByText(/valid email address/i)).toBeNull();
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  // ── Dynamic "min. 8 characters" hint ─────────────────────────────────────────

  it("shows the length hint only while the password is too short", () => {
    renderPage();
    const [pwd] = screen.getAllByPlaceholderText("••••••••");

    fireEvent.change(pwd, { target: { value: "abc" } });
    expect(screen.getByText(/enter minimum of 8 characters/i)).toBeTruthy();

    fireEvent.change(pwd, { target: { value: "abcdefgh" } });
    expect(screen.queryByText(/enter minimum of 8 characters/i)).toBeNull();
  });

  it("does not show the length hint when the password field is empty", () => {
    renderPage();
    expect(screen.queryByText(/enter minimum of 8 characters/i)).toBeNull();
  });

  // ── Show/hide password ───────────────────────────────────────────────────────

  it("toggles the two password fields independently via their eye buttons", () => {
    renderPage();
    const [pwd, confirm] = screen.getAllByPlaceholderText("••••••••") as HTMLInputElement[];
    const eyeButtons = screen.getAllByRole("button", { name: /show password/i });

    expect(pwd.type).toBe("password");
    expect(confirm.type).toBe("password");

    fireEvent.click(eyeButtons[0]);
    expect(pwd.type).toBe("text");
    expect(confirm.type).toBe("password"); // second field unaffected
  });

  // ── Disabled state ─────────────────────────────────────────────────────────

  it("submit is disabled when fields are empty", () => {
    renderPage();
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit is disabled when passwords do not match", () => {
    renderPage();
    fillForm("Acme", "user@example.com", "password123", "different");
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit is disabled when password is shorter than 8 characters", () => {
    renderPage();
    fillForm("Acme", "user@example.com", "short", "short");
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit is enabled with valid matching passwords of 8+ chars", () => {
    renderPage();
    fillForm();
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  // ── Password mismatch visual feedback ──────────────────────────────────────

  it("shows mismatch error when confirm password differs", () => {
    renderPage();
    fillForm("Acme", "user@example.com", "password123", "different!");
    expect(screen.getByText(/passwords do not match/i)).toBeTruthy();
  });

  it("does not show mismatch error when confirm password is empty", () => {
    renderPage();
    fillForm("Acme", "user@example.com", "password123", "");
    expect(screen.queryByText(/passwords do not match/i)).toBeNull();
  });

  // ── Successful registration ────────────────────────────────────────────────

  it("calls register() with orgName, lowercase email, and password", async () => {
    mockRegister.mockResolvedValue(undefined);
    renderPage();
    fillForm("Acme Corp", "  USER@Example.COM  ", "password123");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(mockRegister).toHaveBeenCalledWith(
        "Acme Corp",
        "user@example.com",
        "password123"
      );
    });
  });

  it("reloads into /integration-hub on success", async () => {
    mockRegister.mockResolvedValue(undefined);
    renderPage();
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(mockHardRedirect).toHaveBeenCalledWith("/integration-hub");
    });
  });

  // ── Error handling ─────────────────────────────────────────────────────────

  it("shows email-taken message on 409", async () => {
    mockRegister.mockRejectedValue(new ApiError("conflict", 409, {}));
    renderPage();
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("register-error").textContent).toMatch(
        /already registered/i
      );
    });
  });

  it("shows validation message on 422", async () => {
    mockRegister.mockRejectedValue(new ApiError("unprocessable", 422, {}));
    renderPage();
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("register-error").textContent).toMatch(
        /check your inputs/i
      );
    });
  });

  it("shows generic message for unexpected errors", async () => {
    mockRegister.mockRejectedValue(new Error("Network error"));
    renderPage();
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("register-error").textContent).toMatch(
        /something went wrong/i
      );
    });
  });

  it("does not redirect when registration fails", async () => {
    mockRegister.mockRejectedValue(new ApiError("conflict", 409, {}));
    renderPage();
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(screen.getByTestId("register-error")).toBeTruthy();
    });
    expect(mockHardRedirect).not.toHaveBeenCalled();
  });
});
