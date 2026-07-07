/**
 * AUTH-1 / AT-236 — AuthContext behaviour.
 *
 * Focus: AC14 (design review, made testable here) — page refresh clears the
 * token from React state, GET /api/auth/me is NOT called on mount when no
 * token exists, and the user is unauthenticated. Plus the login/register/logout
 * state transitions described in Section 6.
 */
import React from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthProvider, useAuth } from "../context/AuthContext";
import * as apiClient from "../lib/apiClient";

// Mock the auth API boundary — tests assert on calls, never hit the network.
vi.mock("../api/authApi", () => ({
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  getMe: vi.fn(),
}));

import { getMe, login, logout, register } from "../api/authApi";

const mockLogin = vi.mocked(login);
const mockRegister = vi.mocked(register);
const mockLogout = vi.mocked(logout);
const mockGetMe = vi.mocked(getMe);

const OWNER = {
  id: "u-1",
  email: "owner@example.com",
  role: "owner" as const,
  org_id: "org-1",
};

function Consumer() {
  const { user, token, isAuthenticated, loading, login, register, logout } = useAuth();
  return (
    <div>
      <div data-testid="status">
        {isAuthenticated ? "authenticated" : "anonymous"}
      </div>
      <div data-testid="user">{user ? user.email : "none"}</div>
      <div data-testid="token">{token ?? "none"}</div>
      <div data-testid="loading">{loading ? "loading" : "idle"}</div>
      <button onClick={() => void login("owner@example.com", "supersecret1")}>
        login
      </button>
      <button onClick={() => void register("Acme", "owner@example.com", "supersecret1")}>
        register
      </button>
      <button onClick={() => void logout()}>logout</button>
    </div>
  );
}

function renderProvider() {
  return render(
    <AuthProvider>
      <Consumer />
    </AuthProvider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // Session persistence (Section 3): start each test with empty storage so a
  // token written by one test cannot leak into the next.
  sessionStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
});

describe("AuthContext — mount behaviour (AC14)", () => {
  it("does not call /api/auth/me on mount when no token is in state", async () => {
    renderProvider();
    // Give any (incorrect) effect a chance to fire.
    await Promise.resolve();
    expect(mockGetMe).not.toHaveBeenCalled();
    expect(screen.getByTestId("status").textContent).toBe("anonymous");
    expect(screen.getByTestId("user").textContent).toBe("none");
    expect(screen.getByTestId("loading").textContent).toBe("idle");
  });

  it("restores the session on a page refresh (token persisted in sessionStorage)", async () => {
    // First session: log in so the token is persisted to sessionStorage.
    mockLogin.mockResolvedValue({ token: "jwt-abc", user: OWNER });
    mockGetMe.mockResolvedValue(OWNER);
    const user = userEvent.setup();
    const { unmount } = renderProvider();
    await user.click(screen.getByText("login"));
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authenticated")
    );
    unmount(); // a page refresh wipes React state...

    vi.clearAllMocks();
    mockGetMe.mockResolvedValue(OWNER);

    // ...but the token is still in sessionStorage, so a fresh mount restores it
    // and revalidates via /api/auth/me — the user stays signed in (no re-login).
    renderProvider();
    expect(screen.getByTestId("token").textContent).toBe("jwt-abc");
    await waitFor(() => expect(mockGetMe).toHaveBeenCalledWith("jwt-abc"));
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authenticated")
    );
  });

  it("a refresh with a token but a rejected /api/auth/me drops the session", async () => {
    sessionStorage.setItem("agentiq_auth_token", "jwt-stale");
    mockGetMe.mockRejectedValue(new Error("401"));

    renderProvider();

    // Restored token is validated; rejection clears state + storage.
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("anonymous")
    );
    expect(screen.getByTestId("token").textContent).toBe("none");
    expect(sessionStorage.getItem("agentiq_auth_token")).toBeNull();
  });
});

describe("AuthContext — login / register", () => {
  it("login stores token + user and then refreshes via /api/auth/me", async () => {
    mockLogin.mockResolvedValue({ token: "jwt-abc", user: OWNER });
    mockGetMe.mockResolvedValue({ ...OWNER, last_login_at: "2026-06-09T10:00:00Z" });
    const user = userEvent.setup();
    renderProvider();

    await user.click(screen.getByText("login"));

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authenticated")
    );
    expect(screen.getByTestId("token").textContent).toBe("jwt-abc");
    expect(screen.getByTestId("user").textContent).toBe("owner@example.com");
    // /me is called because a token now exists in state (Section 6).
    await waitFor(() => expect(mockGetMe).toHaveBeenCalledWith("jwt-abc"));
  });

  it("register creates the account but leaves the user logged out", async () => {
    mockRegister.mockResolvedValue({ token: "jwt-reg", user: OWNER });
    mockGetMe.mockResolvedValue(OWNER);
    const user = userEvent.setup();
    renderProvider();

    await user.click(screen.getByText("register"));

    await waitFor(() =>
      expect(mockRegister).toHaveBeenCalledWith(
        "Acme",
        "owner@example.com",
        "supersecret1",
        undefined
      )
    );
    expect(screen.getByTestId("status").textContent).toBe("anonymous");
    expect(screen.getByTestId("token").textContent).toBe("none");
    expect(screen.getByTestId("user").textContent).toBe("none");
    expect(sessionStorage.getItem("agentiq_auth_token")).toBeNull();
    expect(mockGetMe).not.toHaveBeenCalled();
  });
});

describe("AuthContext — logout", () => {
  it("logout calls the endpoint with the token and clears all state", async () => {
    mockLogin.mockResolvedValue({ token: "jwt-abc", user: OWNER });
    mockGetMe.mockResolvedValue(OWNER);
    mockLogout.mockResolvedValue();
    const user = userEvent.setup();
    renderProvider();

    await user.click(screen.getByText("login"));
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authenticated")
    );

    await user.click(screen.getByText("logout"));

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("anonymous")
    );
    expect(mockLogout).toHaveBeenCalledWith("jwt-abc");
    expect(screen.getByTestId("token").textContent).toBe("none");
    expect(screen.getByTestId("user").textContent).toBe("none");
    // Logout must also clear the persisted token so a later refresh stays out.
    expect(sessionStorage.getItem("agentiq_auth_token")).toBeNull();
  });

  it("clears local state even if the logout endpoint fails", async () => {
    mockLogin.mockResolvedValue({ token: "jwt-abc", user: OWNER });
    mockGetMe.mockResolvedValue(OWNER);
    mockLogout.mockRejectedValue(new Error("network"));
    const user = userEvent.setup();
    renderProvider();

    await user.click(screen.getByText("login"));
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authenticated")
    );

    await user.click(screen.getByText("logout"));

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("anonymous")
    );
  });
});

describe("AuthContext — /me refresh failure", () => {
  it("drops the session when /api/auth/me rejects for an in-state token", async () => {
    mockLogin.mockResolvedValue({ token: "jwt-expired", user: OWNER });
    mockGetMe.mockRejectedValue(new Error("401"));
    const user = userEvent.setup();
    renderProvider();

    await user.click(screen.getByText("login"));

    // The follow-up /me refresh fails → token + user are cleared.
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("anonymous")
    );
    expect(screen.getByTestId("token").textContent).toBe("none");
  });
});

describe("useAuth guard", () => {
  it("throws when used outside an AuthProvider", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Consumer />)).toThrow(/within an AuthProvider/);
    spy.mockRestore();
  });
});

describe("AC13 — 401 interceptor", () => {
  it("AuthProvider registers a handler with setUnauthorizedHandler on mount", () => {
    const spy = vi.spyOn(apiClient, "setUnauthorizedHandler");
    const { unmount } = renderProvider();
    expect(spy).toHaveBeenCalledWith(expect.any(Function));
    unmount();
    spy.mockRestore();
  });

  it("AuthProvider clears the handler (null) on unmount", () => {
    const spy = vi.spyOn(apiClient, "setUnauthorizedHandler");
    const { unmount } = renderProvider();
    spy.mockClear();
    unmount();
    expect(spy).toHaveBeenCalledWith(null);
    spy.mockRestore();
  });

  it("the registered handler clears token + user and redirects to /login", async () => {
    const replaceFn = vi.fn();
    vi.stubGlobal("location", { pathname: "/dashboard", replace: replaceFn });

    // Capture the handler AuthProvider registers.
    let captured: (() => void) | null = null;
    const spy = vi.spyOn(apiClient, "setUnauthorizedHandler").mockImplementation((h) => {
      captured = h;
    });

    mockLogin.mockResolvedValue({ token: "jwt-abc", user: OWNER });
    mockGetMe.mockResolvedValue(OWNER);
    const user = userEvent.setup();
    const { unmount } = render(
      <AuthProvider>
        <Consumer />
      </AuthProvider>
    );

    // Log in so token + user are populated.
    await user.click(screen.getByText("login"));
    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authenticated")
    );

    // Fire the captured 401 handler exactly as the interceptor would.
    expect(captured).not.toBeNull();
    captured!();

    await waitFor(() =>
      expect(screen.getByTestId("token").textContent).toBe("none")
    );
    expect(screen.getByTestId("user").textContent).toBe("none");
    expect(screen.getByTestId("status").textContent).toBe("anonymous");
    expect(replaceFn).toHaveBeenCalledWith("/login");

    spy.mockRestore();
    vi.unstubAllGlobals();
    unmount();
  });

  it("the registered handler does not redirect when already on /login", () => {
    const replaceFn = vi.fn();
    vi.stubGlobal("location", { pathname: "/login", replace: replaceFn });

    let captured: (() => void) | null = null;
    const spy = vi.spyOn(apiClient, "setUnauthorizedHandler").mockImplementation((h) => {
      captured = h;
    });

    const { unmount } = renderProvider();
    expect(captured).not.toBeNull();
    captured!();

    expect(replaceFn).not.toHaveBeenCalled();

    spy.mockRestore();
    vi.unstubAllGlobals();
    unmount();
  });
});
