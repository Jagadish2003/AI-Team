/**
 * AgentIQ — AUTH-1 / AT-236
 * Typed wrappers for the /api/auth/* endpoints used by AuthContext.
 *
 * These are intentionally separate from the shared apiClient.ts helpers: the
 * shared client signs every request with a static dev token, whereas the auth
 * endpoints need the *in-session* JWT (logout / me) or no token at all
 * (login / register). The AT-237 401-interceptor work updates apiClient.ts; this
 * module owns only the auth endpoint shapes.
 *
 * Base-URL resolution mirrors apiClient.ts so dev and prod behave identically.
 */
import { ApiError } from "../lib/apiClient";

const ENV_BASE_URL = import.meta.env.VITE_API_BASE_URL as string | undefined;

const BASE_URL =
  ENV_BASE_URL ??
  (import.meta.env.DEV
    ? "http://localhost:8000"
    : (() => {
        throw new Error(
          "VITE_API_BASE_URL is not set. Copy .env.development.example to .env.development (or set env in hosting)."
        );
      })());

export type Role = "owner" | "analyst" | "viewer";

/** Identity + membership as returned by the backend. org_id and role come from
 * workspace_members, never from the users table (see AUTH-1 Section 1/2). */
export interface AuthUser {
  id: string;
  email: string;
  role: Role;
  org_id: string;
  /** Human-readable organization name (from the orgs table). Used for display
   * — e.g. the profile tooltip — instead of the raw org_id UUID. */
  org_name?: string | null;
  /** Present only on GET /api/auth/me. */
  last_login_at?: string | null;
}

/** Shape of POST /api/auth/login and /api/auth/register. */
export interface AuthResult {
  token: string;
  user: AuthUser;
}

async function parseBody(res: Response): Promise<unknown> {
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) return res.json().catch(() => ({}));
  return res.text().catch(() => "");
}

function bearer(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

/** POST /api/auth/login — public. Returns a JWT + user. 401 on bad creds, 429 if throttled. */
export async function login(email: string, password: string): Promise<AuthResult> {
  const res = await fetch(`${BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError("POST /api/auth/login failed", res.status, body);
  return body as AuthResult;
}

/** POST /api/auth/register — public. Creates org + owner, returns a JWT + user. 409 if email taken. */
export async function register(
  orgName: string,
  email: string,
  password: string
): Promise<AuthResult> {
  const res = await fetch(`${BASE_URL}/api/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ org_name: orgName, email, password }),
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError("POST /api/auth/register failed", res.status, body);
  return body as AuthResult;
}

/** GET /api/auth/me — refreshes user state for an existing in-session token. Not a session-restoration mechanism. */
export async function getMe(token: string): Promise<AuthUser> {
  const res = await fetch(`${BASE_URL}/api/auth/me`, {
    headers: { ...bearer(token) },
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError("GET /api/auth/me failed", res.status, body);
  return body as AuthUser;
}

/** POST /api/auth/logout — revokes the token's jti server-side. Returns 204 (no body). */
export async function logout(token: string): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/auth/logout`, {
    method: "POST",
    headers: { ...bearer(token) },
  });
  if (!res.ok) {
    const body = await parseBody(res);
    throw new ApiError("POST /api/auth/logout failed", res.status, body);
  }
}

/** Shape of GET /api/auth/invite-info — the org/email a token resolves to. */
export interface InviteInfo {
  org_name: string | null;
  email: string | null;
  role: Role | null;
}

/**
 * GET /api/auth/invite-info?token=… — AUTH-1 / AT-239.
 * Resolves an invite token to its org name and invitee email WITHOUT consuming
 * it, so the accept-invite page can greet the invitee and surface an invalid /
 * expired / already-used token on load. 400 on a bad/used/expired token.
 */
export async function getInviteInfo(inviteToken: string): Promise<InviteInfo> {
  const res = await fetch(
    `${BASE_URL}/api/auth/invite-info?token=${encodeURIComponent(inviteToken)}`
  );
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError("GET /api/auth/invite-info failed", res.status, body);
  return body as InviteInfo;
}

/**
 * POST /api/auth/accept-invite — AUTH-1 / AT-239.
 * Sets password for an invited user and activates the account.
 * invite_token comes from the invite URL query param.
 * Returns a JWT + user on success. 400 on invalid/expired/already-used token.
 * Single-use — second call with the same token returns 400.
 */
export async function acceptInvite(
  inviteToken: string,
  password: string
): Promise<AuthResult> {
  const res = await fetch(`${BASE_URL}/api/auth/accept-invite`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invite_token: inviteToken, password }),
  });
  const body = await parseBody(res);
  if (!res.ok) throw new ApiError("POST /api/auth/accept-invite failed", res.status, body);
  return body as AuthResult;
}

/**
 * POST /api/auth/reset-password — CS-3 §5. Public.
 * Sets a new password for the account a reset token belongs to. The token comes
 * from the reset-password URL query param. Returns 200 (no useful body) on
 * success, 400 on an invalid/expired token, and 422 when the new password fails
 * the strength rule. Unlike accept-invite this does NOT return a session — the
 * user is sent to /login afterwards to sign in with the new password.
 */
export async function resetPassword(
  resetToken: string,
  newPassword: string
): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/auth/reset-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reset_token: resetToken, new_password: newPassword }),
  });
  if (!res.ok) {
    const body = await parseBody(res);
    throw new ApiError("POST /api/auth/reset-password failed", res.status, body);
  }
}
