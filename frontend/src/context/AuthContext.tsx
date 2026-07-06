/**
 * AgentIQ — AUTH-1 / AT-236
 * App-wide authentication state.
 *
 * Section 3 — Token Storage (updated: refresh keeps the session):
 *   The JWT is persisted in sessionStorage so a page refresh no longer logs the
 *   user out. (The original AUTH-1 POC kept the token in memory only — "page
 *   refresh = re-login" — which users hit as an accidental logout on every
 *   refresh.) On mount the stored token is restored into state and validated
 *   via GET /api/auth/me; an invalid/expired/revoked token drops the session.
 *
 *   sessionStorage is per-tab and is cleared when the tab/browser closes, so
 *   closing the browser still requires a fresh login (swap to localStorage if
 *   you want the session to survive a full browser restart). httpOnly cookies
 *   remain the production-grade, XSS-safe approach (web storage is readable by
 *   JS) and are still the recommended post-POC hardening step.
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import {
  AuthUser,
  acceptInvite as apiAcceptInvite,
  getMe,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
} from "../api/authApi";
import { setAuthToken, setUnauthorizedHandler } from "../lib/apiClient";

interface AuthContextValue {
  /** In-session JWT. null when logged out (and after any page refresh). */
  token: string | null;
  /** Current user identity + membership, or null when logged out. */
  user: AuthUser | null;
  /** Convenience flag — true when a user is authenticated this session. */
  isAuthenticated: boolean;
  /** True while the on-mount /api/auth/me refresh is in flight. */
  loading: boolean;
  /** POST /api/auth/login, store token + user in state. Throws ApiError on failure. */
  login: (email: string, password: string) => Promise<void>;
  /** POST /api/auth/register, then leave the user logged out. Throws ApiError on failure. */
  register: (
    orgName: string,
    email: string,
    password: string,
    fullName?: string
  ) => Promise<void>;
  /** POST /api/auth/logout (best-effort) and clear all state. */
  logout: () => Promise<void>;
  /** POST /api/auth/accept-invite, store token + user in state. Throws ApiError on failure. */
  acceptInvite: (inviteToken: string, password: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

// Section 3: the JWT is persisted here so a refresh restores the session.
const TOKEN_STORAGE_KEY = "agentiq_auth_token";

function readStoredToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null;
  }
}

/**
 * Persist (or clear, with null) the JWT in sessionStorage. The [token] effect
 * below does this on every render, but the auth callbacks call it directly too:
 * the login/accept-invite pages trigger a full-document reload right
 * after authenticating (to rebuild all in-session context for the new user),
 * and that reload happens before the post-render effect would have flushed —
 * so without this synchronous write the freshly issued token would be lost.
 */
function writeStoredToken(token: string | null): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    else sessionStorage.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    // Storage unavailable (private mode / disabled) — degrade to in-memory.
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Token restored from sessionStorage on mount (Section 3) so a refresh keeps
  // the user signed in; user is re-fetched via /api/auth/me below.
  const [token, setToken] = useState<string | null>(() => readStoredToken());
  const [user, setUser] = useState<AuthUser | null>(null);
  // Start in "loading" when a token was restored, so AuthGuard holds the route
  // (shows "Verifying session…") instead of flashing /login before the mount
  // /api/auth/me revalidation resolves on a refresh.
  const [loading, setLoading] = useState<boolean>(() => readStoredToken() !== null);

  // AC13: Register the 401 interceptor once. When any apiClient call receives a
  // 401, this handler clears auth state and sends the user back to /login.
  // Guard against redirect loops: skip the redirect if already on /login.
  // setToken and setUser are stable useState setters — empty dep array is correct.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setToken(null);
      setUser(null);
      if (!window.location.pathname.startsWith("/login")) {
        window.location.replace("/login");
      }
    });
    return () => {
      setUnauthorizedHandler(null);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep apiClient's Authorization header AND sessionStorage in sync with the
  // in-session JWT. apiClient sync scopes all data requests to THIS user's org
  // (without it, requests fall back to the static dev token → everyone resolves
  // to the `default` org, leaking connectors/runs). sessionStorage persistence
  // is what lets a refresh restore the session. Both are cleared when token
  // becomes null (logout / 401), so the dev-token fallback and a clean /login
  // apply again.
  useEffect(() => {
    setAuthToken(token);
    writeStoredToken(token);
  }, [token]);

  // Validate the token whenever it is present: right after login/accept-invite, and
  // on mount when it was restored from sessionStorage (a refresh). /api/auth/me
  // refreshes the user record; a rejected call (invalid/expired/revoked token)
  // drops the session and clears storage via the effect above.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    getMe(token)
      .then((freshUser) => {
        if (!cancelled) setUser(freshUser);
      })
      .catch(() => {
        // Token is invalid/expired/revoked — drop the session.
        if (!cancelled) {
          setToken(null);
          setUser(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const login = useCallback(async (email: string, password: string) => {
    const result = await apiLogin(email, password);
    // Persist synchronously: the caller reloads the document immediately after
    // (see writeStoredToken) so the post-render [token] effect would be too late.
    writeStoredToken(result.token);
    setToken(result.token);
    setUser(result.user);
  }, []);

  const register = useCallback(
    async (
      orgName: string,
      email: string,
      password: string,
      fullName?: string
    ) => {
      await apiRegister(orgName, email, password, fullName);
      writeStoredToken(null);
      setToken(null);
      setUser(null);
    },
    []
  );

  const acceptInvite = useCallback(async (inviteToken: string, password: string) => {
    const result = await apiAcceptInvite(inviteToken, password);
    writeStoredToken(result.token);
    setToken(result.token);
    setUser(result.user);
  }, []);

  const logout = useCallback(async () => {
    const current = token;
    // Clear local state regardless of whether the server call succeeds, so the
    // UI never gets stuck authenticated if logout fails.
    setToken(null);
    setUser(null);
    if (current) {
      try {
        await apiLogout(current);
      } catch {
        // Best-effort: the token is being discarded client-side either way.
      }
    }
  }, [token]);

  const value = useMemo<AuthContextValue>(
    () => ({
      token,
      user,
      isAuthenticated: Boolean(token && user),
      loading,
      login,
      register,
      logout,
      acceptInvite,
    }),
    [token, user, loading, login, register, logout, acceptInvite]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return ctx;
}

/**
 * Non-throwing variant of useAuth. Returns undefined when rendered outside an
 * AuthProvider instead of throwing. Use this in shared chrome (e.g. TopNav) that
 * is mounted inside an AuthProvider in the real app but is also rendered in
 * isolation by component tests that do not set one up.
 */
export function useAuthOptional(): AuthContextValue | undefined {
  return useContext(AuthContext);
}
