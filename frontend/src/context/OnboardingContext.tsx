/**
 * OnboardingContext — first-login product tour state.
 *
 * Owns the "should the premium onboarding appear?" decision and the persistent
 * "already seen it" flag, WITHOUT touching authentication, routing, or any API.
 * It is a thin overlay layer:
 *
 *   - Mounted INSIDE AuthGuard's authenticated branch (so it only ever exists for
 *     a signed-in user, and the dashboard renders behind it via <Outlet/>).
 *   - Auto-shows exactly once, on the first authenticated mount for a user who has
 *     no completion flag yet (i.e. their first login after registration +
 *     organisation approval).
 *   - Completing OR skipping persists `hasCompletedOnboarding = true` so it never
 *     auto-appears again. Future logins continue straight to the dashboard.
 *   - `replay()` reopens it on demand (Settings / profile menu "Replay product
 *     tour") without depending on the flag.
 *
 * Persistence: there is no server-side user-preference field exposed to the SPA,
 * and the auth/API surface is intentionally off-limits, so the flag lives in
 * localStorage keyed per user id — persistent across logins on this browser,
 * mirroring the storage pattern already used by ThemeContext / AuthContext.
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { useAuth } from "./AuthContext";

interface OnboardingContextValue {
  /** True while the onboarding card is visible. */
  isOpen: boolean;
  /** Complete or skip: persist the flag for this user and close. */
  dismiss: () => void;
  /** Reopen the tour on demand (does not clear the completion flag). */
  replay: () => void;
}

const OnboardingContext = createContext<OnboardingContextValue | undefined>(
  undefined
);

// Versioned prefix so the tour can be re-shown to everyone in future by bumping
// the version, without colliding with a stale flag.
const STORAGE_PREFIX = "agentiq_onboarding_completed_v1";

function storageKey(userId: string): string {
  return `${STORAGE_PREFIX}:${userId}`;
}

function hasCompleted(userId: string): boolean {
  try {
    return window.localStorage.getItem(storageKey(userId)) === "true";
  } catch {
    // Storage unavailable (private mode / disabled) — treat as "not completed"
    // in memory; the tour simply shows for the session and never persists.
    return false;
  }
}

function markCompleted(userId: string): void {
  try {
    window.localStorage.setItem(storageKey(userId), "true");
  } catch {
    // Degrade silently — persistence is best-effort.
  }
}

export function OnboardingProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const userId = user?.id ?? null;

  const [isOpen, setIsOpen] = useState(false);

  // Auto-show ONCE per user, on the first authenticated mount where the user has
  // no completion flag. Keyed on userId so switching users re-evaluates cleanly.
  useEffect(() => {
    if (!userId) {
      setIsOpen(false);
      return;
    }
    if (!hasCompleted(userId)) {
      setIsOpen(true);
    }
  }, [userId]);

  const dismiss = useCallback(() => {
    if (userId) markCompleted(userId);
    setIsOpen(false);
  }, [userId]);

  const replay = useCallback(() => {
    setIsOpen(true);
  }, []);

  const value = useMemo<OnboardingContextValue>(
    () => ({ isOpen, dismiss, replay }),
    [isOpen, dismiss, replay]
  );

  return (
    <OnboardingContext.Provider value={value}>
      {children}
    </OnboardingContext.Provider>
  );
}

export function useOnboarding(): OnboardingContextValue {
  const ctx = useContext(OnboardingContext);
  if (ctx === undefined) {
    throw new Error("useOnboarding must be used within an OnboardingProvider");
  }
  return ctx;
}

/**
 * Non-throwing variant. Returns undefined when rendered outside an
 * OnboardingProvider (e.g. isolated component tests that render a page without
 * AuthGuard). Shared chrome like TopNav uses this so the "Replay product tour"
 * entry appears in the real app but is silently absent in those tests.
 */
export function useOnboardingOptional(): OnboardingContextValue | undefined {
  return useContext(OnboardingContext);
}
