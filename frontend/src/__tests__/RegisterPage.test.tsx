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

// CS-3: the default password now satisfies the full strength rule (length +
// uppercase + lowercase + special) so submit is enabled. Tests that need a
// weak password pass one explicitly.
function fillForm(
  orgName = "Acme Corp",
  email = "user@example.com",
  password = "Password1!",
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

/** The strength-indicator row (the <li data-met>) whose label matches. */
function requirementRow(label: RegExp | string): HTMLElement | null {
  return screen.getByText(label).closest("[data-met]");
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
    fillForm("Acme Corp", "abc.m@xy.org");
    expect(screen.queryByText(/valid email address/i)).toBeNull();
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
  });

  // ── Password strength indicator (CS-3) ───────────────────────────────────────

  it("renders the four password requirements, all unmet, before typing", () => {
    renderPage();
    const rows = screen.getAllByTestId("password-requirement");
    expect(rows).toHaveLength(4);
    rows.forEach((row) => expect(row.getAttribute("data-met")).toBe("false"));
  });

  it("marks the 8-character requirement met only once the password is long enough", () => {
    renderPage();
    const [pwd] = screen.getAllByPlaceholderText("••••••••");

    fireEvent.change(pwd, { target: { value: "Ab1!" } });
    expect(requirementRow(/at least 8 characters/i)?.getAttribute("data-met")).toBe("false");

    fireEvent.change(pwd, { target: { value: "Abcdef1!" } });
    expect(requirementRow(/at least 8 characters/i)?.getAttribute("data-met")).toBe("true");
  });

  it("turns every requirement green for a fully valid password", () => {
    renderPage();
    const [pwd] = screen.getAllByPlaceholderText("••••••••");
    fireEvent.change(pwd, { target: { value: "Password1!" } });
    screen
      .getAllByTestId("password-requirement")
      .forEach((row) => expect(row.getAttribute("data-met")).toBe("true"));
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
    fillForm("Acme", "user@example.com", "Password1!", "Password2!");
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit is disabled when password is shorter than 8 characters", () => {
    renderPage();
    fillForm("Acme", "user@example.com", "Aa1!x", "Aa1!x"); // upper/lower/special but only 5 chars
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit stays disabled for an 8+ char password missing uppercase and special", () => {
    renderPage();
    fillForm("Acme", "user@example.com", "password"); // 8 chars, lowercase only
    const btn = screen.getByRole("button", { name: /create account/i }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("submit is enabled with valid matching passwords meeting all requirements", () => {
    renderPage();
    fillForm(); // default password is Password1! — satisfies all four rules
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
    fillForm("Acme Corp", "  USER@Example.COM  ", "Password1!");
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(mockRegister).toHaveBeenCalledWith(
        "Acme Corp",
        "user@example.com",
        "Password1!"
      );
    });
  });

  it("reloads into /pending-approval on success (AUTH-2 T6)", async () => {
    mockRegister.mockResolvedValue(undefined);
    renderPage();
    fillForm();
    fireEvent.click(screen.getByRole("button", { name: /create account/i }));

    await waitFor(() => {
      expect(mockHardRedirect).toHaveBeenCalledWith("/pending-approval");
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
