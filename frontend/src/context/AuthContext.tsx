/**
 * AgentIQ — AUTH-1 / AT-236
 * App-wide authentication state.
 *
 * Section 3 — Token Storage (explicit, POC behaviour):
 *   The token and user live in React state ONLY. Not in localStorage, not in
 *   sessionStorage, not in a cookie. A page refresh wipes React state, so the
 *   token is gone and the user lands back on /login.
 *
 *   >>> PAGE REFRESH = RE-LOGIN. <<<  This is intentional for the POC.
 *
 *   GET /api/auth/me is called ONLY when a token already exists in the current
 *   session (to refresh the user record). It is NOT a session-restoration
 *   mechanism — on first mount the token is always null, so /api/auth/me is
 *   never called on load (AC14).
 *
 *   httpOnly cookie session persistence is the production-grade solution and is
 *   explicitly POST-POC. Do not implement it in this story.
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
import { setUnauthorizedHandler } from "../lib/apiClient";

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
  /** POST /api/auth/register, store token + user in state. Throws ApiError on failure. */
  register: (orgName: string, email: string, password: string) => Promise<void>;
  /** POST /api/auth/logout (best-effort) and clear all state. */
  logout: () => Promise<void>;
  /** POST /api/auth/accept-invite, store token + user in state. Throws ApiError on failure. */
  acceptInvite: (inviteToken: string, password: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // Token and user in React state ONLY — never persisted (Section 3).
  const [token, setToken] = useState<string | null>(null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(false);

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

  // On mount: only call /api/auth/me if a token is already in state. The token
  // is never in state on first mount (a page refresh wipes React state), so
  // this is a no-op on load — page refresh always requires re-login (AC14).
  // When a token IS present (i.e. right after login/register), this refreshes
  // the user record from the server.
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
    setToken(result.token);
    setUser(result.user);
  }, []);

  const register = useCallback(
    async (orgName: string, email: string, password: string) => {
      const result = await apiRegister(orgName, email, password);
      setToken(result.token);
      setUser(result.user);
    },
    []
  );

  const acceptInvite = useCallback(async (inviteToken: string, password: string) => {
    const result = await apiAcceptInvite(inviteToken, password);
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
