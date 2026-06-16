/**
 * CS-3 / AC9 — PasswordStrengthIndicator unit tests.
 *
 * Covers the getPasswordRequirements() helper (the single source of truth the
 * three password-creation pages share) and the visual checklist: four rules,
 * each turning green as it is satisfied.
 */
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import PasswordStrengthIndicator, {
  getPasswordRequirements,
} from "../components/auth/PasswordStrengthIndicator";

describe("getPasswordRequirements", () => {
  it("marks all four requirements met for a fully valid password", () => {
    const reqs = getPasswordRequirements("Password1!");
    expect(reqs).toHaveLength(4);
    expect(reqs.every((r) => r.met)).toBe(true);
  });

  it("flags missing uppercase and special for an all-lowercase password", () => {
    const byLabel = Object.fromEntries(
      getPasswordRequirements("password").map((r) => [r.label, r.met])
    );
    expect(byLabel["At least 8 characters"]).toBe(true);
    expect(byLabel["One lowercase letter (a-z)"]).toBe(true);
    expect(byLabel["One uppercase letter (A-Z)"]).toBe(false);
    expect(byLabel["One special character (!@#$%^&*...)"]).toBe(false);
  });

  it("flags only the length rule for a short but otherwise strong password", () => {
    const unmet = getPasswordRequirements("Pa1!").filter((r) => !r.met); // 4 chars
    expect(unmet).toHaveLength(1);
    expect(unmet[0].label).toMatch(/8 characters/);
  });

  it("treats an empty password as all unmet", () => {
    expect(getPasswordRequirements("").every((r) => !r.met)).toBe(true);
  });

  it("recognises a range of special characters", () => {
    for (const ch of ["!", "@", "#", "-", "_", "?", ".", "$", "*"]) {
      const special = getPasswordRequirements(`Abcdefg1${ch}`).find((r) =>
        r.label.startsWith("One special")
      );
      expect(special?.met).toBe(true);
    }
  });
});

describe("PasswordStrengthIndicator", () => {
  it("renders all four requirement rows", () => {
    render(<PasswordStrengthIndicator password="" />);
    expect(screen.getAllByTestId("password-requirement")).toHaveLength(4);
    expect(screen.getByText(/at least 8 characters/i)).toBeTruthy();
    expect(screen.getByText(/one uppercase letter/i)).toBeTruthy();
    expect(screen.getByText(/one lowercase letter/i)).toBeTruthy();
    expect(screen.getByText(/one special character/i)).toBeTruthy();
  });

  it("renders every requirement as not-met (grey) for an empty password", () => {
    render(<PasswordStrengthIndicator password="" />);
    screen.getAllByTestId("password-requirement").forEach((row) => {
      expect(row.getAttribute("data-met")).toBe("false");
      expect(row.className).toContain("text-muted");
    });
  });

  it("turns a requirement green the moment it is satisfied", () => {
    const { rerender } = render(<PasswordStrengthIndicator password="ab" />);
    const lengthRow = () =>
      screen.getByText(/at least 8 characters/i).closest("[data-met]") as HTMLElement;

    expect(lengthRow().getAttribute("data-met")).toBe("false");

    rerender(<PasswordStrengthIndicator password="abcdefgh" />);
    expect(lengthRow().getAttribute("data-met")).toBe("true");
    expect(lengthRow().className).toContain("text-green-500");
  });

  it("greens all four requirements for a fully valid password", () => {
    render(<PasswordStrengthIndicator password="Password1!" />);
    screen.getAllByTestId("password-requirement").forEach((row) => {
      expect(row.getAttribute("data-met")).toBe("true");
      expect(row.className).toContain("text-green-500");
    });
  });
});
