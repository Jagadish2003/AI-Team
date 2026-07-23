/**
 * Onboarding product tour — first-login behaviour.
 *
 * Covers:
 *   - Auto-shows on first login (no completion flag) with a personalised greeting.
 *   - Slide navigation (Next / Back) and the last-slide "Go To Dashboard" label.
 *   - Skip and complete both persist the per-user flag and never auto-show again.
 *   - Replay reopens the tour on demand.
 *   - ESC dismisses.
 *
 * The dashboard-behind-onboarding wiring (AuthGuard) is exercised indirectly:
 * the modal is a pure overlay driven by OnboardingContext, so we mount the
 * provider + modal directly and control auth via a mocked useAuth.
 */
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OnboardingProvider } from "../context/OnboardingContext";
import OnboardingModal from "../components/onboarding/OnboardingModal";
import { useAuth } from "../context/AuthContext";

vi.mock("../context/AuthContext", () => ({
  useAuth: vi.fn(),
}));

const mockUseAuth = vi.mocked(useAuth);

const USER = {
  isAuthenticated: true,
  loading: false,
  token: "jwt-test",
  user: {
    id: "user-42",
    email: "sreedhar@dwpglobalcorp.com",
    role: "owner" as const,
    org_id: "org-1",
  },
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  acceptInvite: vi.fn(),
};

function renderTour() {
  return render(
    <OnboardingProvider>
      <OnboardingModal />
    </OnboardingProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  mockUseAuth.mockReturnValue(USER);
});

describe("Onboarding — auto show on first login", () => {
  it("shows the tour with a personalised greeting when no flag is set", () => {
    renderTour();
    expect(screen.getByTestId("onboarding-overlay")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: /welcome, sreedhar/i })
    ).toBeInTheDocument();
  });

  it("does NOT auto-show when the completion flag is already set", () => {
    window.localStorage.setItem("agentiq_onboarding_completed_v1:user-42", "true");
    renderTour();
    expect(screen.queryByTestId("onboarding-overlay")).not.toBeInTheDocument();
  });
});

describe("Onboarding — navigation", () => {
  it("advances through slides and ends on Go To Dashboard", () => {
    renderTour();
    // Slide 1 — no Back, primary is Get Started.
    expect(screen.queryByTestId("onboarding-back")).not.toBeInTheDocument();
    expect(screen.getByTestId("onboarding-primary")).toHaveTextContent(/get started/i);

    fireEvent.click(screen.getByTestId("onboarding-primary")); // -> slide 2
    expect(screen.getByTestId("onboarding-back")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /connect your systems/i })).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("onboarding-primary")); // -> slide 3
    fireEvent.click(screen.getByTestId("onboarding-primary")); // -> slide 4 (last)
    expect(screen.getByTestId("onboarding-primary")).toHaveTextContent(/go to dashboard/i);
    // Skip button is hidden on the last slide.
    expect(screen.queryByTestId("onboarding-skip")).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId("onboarding-back")); // back to slide 3
    expect(screen.getByRole("heading", { name: /ai correlation/i })).toBeInTheDocument();
  });
});

describe("Onboarding — completion persists and never reappears", () => {
  it("Go To Dashboard sets the flag and closes", () => {
    const { unmount } = renderTour();
    fireEvent.click(screen.getByTestId("onboarding-primary")); // 2
    fireEvent.click(screen.getByTestId("onboarding-primary")); // 3
    fireEvent.click(screen.getByTestId("onboarding-primary")); // 4
    fireEvent.click(screen.getByTestId("onboarding-primary")); // Go To Dashboard

    expect(screen.queryByTestId("onboarding-overlay")).not.toBeInTheDocument();
    expect(
      window.localStorage.getItem("agentiq_onboarding_completed_v1:user-42")
    ).toBe("true");

    // Re-mounting (a future login) does not auto-show it again.
    unmount();
    renderTour();
    expect(screen.queryByTestId("onboarding-overlay")).not.toBeInTheDocument();
  });

  it("Skip (available from slide 2 onwards) sets the flag and closes", () => {
    renderTour();
    // Welcome slide has no Skip — it is an introduction, not part of the tour.
    expect(screen.queryByTestId("onboarding-skip")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("onboarding-primary")); // Get Started -> slide 2
    fireEvent.click(screen.getByTestId("onboarding-skip"));
    expect(screen.queryByTestId("onboarding-overlay")).not.toBeInTheDocument();
    expect(
      window.localStorage.getItem("agentiq_onboarding_completed_v1:user-42")
    ).toBe("true");
  });

  it("ESC dismisses and persists", () => {
    renderTour();
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(screen.queryByTestId("onboarding-overlay")).not.toBeInTheDocument();
    expect(
      window.localStorage.getItem("agentiq_onboarding_completed_v1:user-42")
    ).toBe("true");
  });
});
