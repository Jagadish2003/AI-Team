/**
 * AUTH-1 / AT-239 — Accept invite page.
 *
 * Section 6 behaviour:
 *   Reads invite_token from the URL query param `?token=<value>`.
 *   POST /api/auth/accept-invite via AuthContext.acceptInvite().
 *   Sets password for an invited user (is_active=False → True).
 *   Returns JWT — user is logged in immediately.
 *   Redirects to /integration-hub on success.
 *   Single-use — second call with the same token returns 400.
 *   Expired token (>72 h) returns 400.
 */
import React, { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { ApiError } from "../lib/apiClient";

// ── Shared styling constants ──────────────────────────────────────────────────

const INPUT_CLS =
  "w-full rounded-lg border border-border bg-bg px-3 py-2.5 text-sm text-text " +
  "placeholder:text-muted focus:border-accent/60 focus:outline-none focus:ring-2 " +
  "focus:ring-accent/30 disabled:opacity-50";

const SUBMIT_CLS =
  "w-full inline-flex min-h-9 items-center justify-center whitespace-nowrap rounded-md " +
  "px-3 py-2 text-sm font-medium transition-[border-color,background-color,box-shadow,color,opacity] " +
  "focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 " +
  "disabled:cursor-not-allowed disabled:opacity-40 " +
  "border border-accent bg-accent text-textwhite shadow-sm hover:bg-accent/90";

// ── Error message resolver ────────────────────────────────────────────────────

function acceptInviteErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 400) return "This invite link is invalid, expired, or has already been used.";
    if (err.status === 422) return "Please check your password and try again.";
  }
  return "Something went wrong. Please try again.";
}

// ── Component ────────────────────────────────────────────────────────────────

export default function AcceptInvitePage() {
  const { acceptInvite } = useAuth();
  const { theme } = useTheme();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const inviteToken = searchParams.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const passwordMismatch =
    confirmPassword.length > 0 && password !== confirmPassword;

  const canSubmit =
    inviteToken.length > 0 &&
    password.length >= 8 &&
    password === confirmPassword &&
    !submitting;

  // Missing token — show an error state immediately, no form.
  if (!inviteToken) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-12">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex justify-center">
            <img
              src={theme === "dark" ? "/Logo-Dark.svg" : "/Logo-Light.svg"}
              alt="AgentIQ"
              className="h-10 w-auto"
            />
          </div>
          <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-6 py-8 text-center shadow-xl shadow-black/20">
            <p className="text-sm font-medium text-red-400" data-testid="missing-token-error">
              Invalid invite link. Please ask your administrator to send a new invitation.
            </p>
          </div>
        </div>
      </div>
    );
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (passwordMismatch) return;
    setError(null);
    setSubmitting(true);
    try {
      await acceptInvite(inviteToken, password);
      navigate("/integration-hub", { replace: true });
    } catch (err) {
      setError(acceptInviteErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-12">
      <div className="w-full max-w-sm">

        {/* Logo */}
        <div className="mb-8 flex justify-center">
          <img
            src={theme === "dark" ? "/Logo-Dark.svg" : "/Logo-Light.svg"}
            alt="AgentIQ"
            className="h-10 w-auto"
          />
        </div>

        {/* Card */}
        <div className="rounded-xl border border-border bg-panel px-6 py-8 shadow-xl shadow-black/20">
          <h1 className="mb-1 text-center text-xl font-semibold text-text">Set your password</h1>
          <p className="mb-6 text-center text-sm text-muted">
            You've been invited to AgentIQ. Choose a password to activate your account.
          </p>

          <form onSubmit={handleSubmit} noValidate className="space-y-4">
            <div>
              <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-text">
                Password
                <span className="ml-1 text-xs font-normal text-muted">(min. 8 characters)</span>
              </label>
              <input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className={INPUT_CLS}
                placeholder="••••••••"
                disabled={submitting}
              />
            </div>

            <div>
              <label htmlFor="confirm-password" className="mb-1.5 block text-sm font-medium text-text">
                Confirm password
              </label>
              <input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                required
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                className={`${INPUT_CLS} ${passwordMismatch ? "border-red-500/50 focus:ring-red-500/20" : ""}`}
                placeholder="••••••••"
                disabled={submitting}
              />
              {passwordMismatch && (
                <p className="mt-1 text-xs text-red-400">Passwords do not match.</p>
              )}
            </div>

            {error && (
              <p
                role="alert"
                data-testid="accept-invite-error"
                className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400"
              >
                {error}
              </p>
            )}

            <button type="submit" disabled={!canSubmit} className={SUBMIT_CLS}>
              {submitting ? "Activating account…" : "Activate account"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
