/**
 * Role helpers — single source of truth for "is this a viewer" across the UI.
 *
 * Two ways a session can be a viewer:
 *   1. A real login — AuthContext.user.role === "viewer" (from /api/auth/me).
 *   2. A dev/simulation run with no real login — the role is declared via the
 *      VITE_DEV_JWT_ROLE env var, or the configured dev token IS the viewer
 *      token. This mirrors the existing isViewerOnlyScopeUser() convention so
 *      the whole app agrees on who is a viewer.
 *
 * isViewerRole() prefers the authenticated role and falls back to the env/dev
 * signal only when there is no authenticated role (authRole null/undefined).
 */
import type { Role } from "../api/authApi";

function envRoleIsViewer(): boolean {
  const role = (import.meta.env.VITE_DEV_JWT_ROLE as string | undefined)
    ?.trim()
    .toLowerCase();
  if (role) return role === "viewer";

  const token =
    (import.meta.env.VITE_DEV_JWT as string | undefined) ?? "dev-token-change-me";
  const viewerToken =
    (import.meta.env.VITE_VIEWER_JWT as string | undefined) ?? "viewer-token";
  return token === viewerToken;
}

/** True when the current user should get read-only (viewer) treatment. */
export function isViewerRole(authRole?: Role | string | null): boolean {
  if (authRole) return authRole === "viewer";
  return envRoleIsViewer();
}
