/**
 * CS-3 — PasswordStrengthIndicator unit tests.
 *
 * Covers the getPasswordRequirements() helper (which mirrors the backend
 * validate_password_strength rules) and the rendered four-requirement checklist
 * that turns green as conditions are met (AC9).
 */
import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import PasswordStrengthIndicator, {
  getPasswordRequirements,
  isPasswordStrong,
} from "../components/auth/PasswordStrengthIndicator";

// ── getPasswordRequirements (rule parity with backend) ─────────────────────────

describe("getPasswordRequirements", () => {
  function metLabels(password: string): string[] {
    return getPasswordRequirements(password)
      .filter((r) => r.met)
      .map((r) => r.label);
  }
  function unmetCount(password: string): number {
    return getPasswordRequirements(password).filter((r) => !r.met).length;
  }

  it("returns four requirements in display order", () => {
    const reqs = getPasswordRequirements("");
    expect(reqs).toHaveLength(4);
    expect(reqs.map((r) => r.label)).toEqual([
      "At least 8 characters",
      "One uppercase letter (A-Z)",
      "One lowercase letter (a-z)",
      "One special character (!@#$%^&*…)",
    ]);
  });

  it("marks all four met for a fully valid password (AC1 parity)", () => {
    expect(isPasswordStrong("Password1!")).toBe(true);
    expect(unmetCount("Password1!")).toBe(0);
  });

  it("'password' meets only length + lowercase (missing upper & special)", () => {
    // Backend AC2 analog: lowercase 8-char word fails uppercase and special.
    expect(metLabels("password")).toEqual([
      "At least 8 characters",
      "One lowercase letter (a-z)",
    ]);
    expect(isPasswordStrong("password")).toBe(false);
  });

  it("'Pass1!' fails only the length rule (AC3 parity)", () => {
    const reqs = getPasswordRequirements("Pass1!");
    const unmet = reqs.filter((r) => !r.met);
    expect(unmet).toHaveLength(1);
    expect(unmet[0].label).toBe("At least 8 characters");
  });

  it("recognises a variety of special characters", () => {
    for (const ch of ["!", "@", "#", "$", "%", "^", "&", "*", "-", "_", "?", "."]) {
      expect(isPasswordStrong(`Abcdefg1${ch}`)).toBe(true);
    }
  });

  it("does not count a letter/digit-only password as having a special char", () => {
    const reqs = getPasswordRequirements("Abcdefgh1");
    const special = reqs.find((r) => r.label.startsWith("One special"));
    expect(special?.met).toBe(false);
  });
});

// ── Rendered checklist (AC9) ───────────────────────────────────────────────────

describe("PasswordStrengthIndicator render", () => {
  it("renders all four requirement rows", () => {
    render(<PasswordStrengthIndicator password="" />);
    const list = screen.getByTestId("password-strength");
    expect(list.querySelectorAll("li")).toHaveLength(4);
  });

  it("marks each row met/unmet via data-met as the password satisfies rules", () => {
    render(<PasswordStrengthIndicator password="Password1!" />);
    const rows = screen
      .getByTestId("password-strength")
      .querySelectorAll("li");
    rows.forEach((row) => expect(row.getAttribute("data-met")).toBe("true"));
  });

  it("shows unmet rows as not-met for a weak password", () => {
    render(<PasswordStrengthIndicator password="abc" />);
    const metRows = Array.from(
      screen.getByTestId("password-strength").querySelectorAll("li")
    ).filter((r) => r.getAttribute("data-met") === "true");
    // Only the lowercase rule is satisfied by "abc".
    expect(metRows).toHaveLength(1);
  });
});
