/**
 * Force a full-document load to `path`, replacing the current history entry and
 * discarding ALL in-memory React state.
 *
 * Used after an auth transition (login / register / accept-invite). A client-
 * side router navigate keeps every context provider mounted, so the previous
 * user's in-session data (connector statuses, run state, …) survives the
 * sign-in of a different user and leaks across orgs until a manual reload. A
 * full reload re-mounts the provider tree and re-fetches everything signed with
 * the new user's token — the same effect as the user pressing refresh.
 *
 * Isolated in its own module so the auth pages can be unit-tested without
 * touching jsdom's unimplemented `window.location` navigation.
 */
export function hardRedirect(path: string): void {
  window.location.replace(path);
}
