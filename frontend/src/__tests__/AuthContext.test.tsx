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
});

afterEach(() => {
  vi.clearAllMocks();
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

  it("simulating a page refresh (fresh mount) leaves the user logged out", () => {
    // First session: log in so token+user exist.
    mockLogin.mockResolvedValue({ token: "jwt-abc", user: OWNER });
    mockGetMe.mockResolvedValue(OWNER);
    const { unmount } = renderProvider();
    unmount(); // page refresh wipes React state

    vi.clearAllMocks();

    // Fresh mount = new in-memory state. Token is null again, /me not called.
    renderProvider();
    expect(mockGetMe).not.toHaveBeenCalled();
    expect(screen.getByTestId("status").textContent).toBe("anonymous");
    expect(screen.getByTestId("token").textContent).toBe("none");
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

  it("register stores token + user", async () => {
    mockRegister.mockResolvedValue({ token: "jwt-reg", user: OWNER });
    mockGetMe.mockResolvedValue(OWNER);
    const user = userEvent.setup();
    renderProvider();

    await user.click(screen.getByText("register"));

    await waitFor(() =>
      expect(screen.getByTestId("status").textContent).toBe("authenticated")
    );
    expect(mockRegister).toHaveBeenCalledWith("Acme", "owner@example.com", "supersecret1");
    expect(screen.getByTestId("token").textContent).toBe("jwt-reg");
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
