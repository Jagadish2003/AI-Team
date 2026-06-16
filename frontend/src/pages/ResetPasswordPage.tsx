/**
 * CS-3 — Reset password page.
 *
 * Reached from the password-reset email link: /reset-password?token=<value>.
 * The user sets a new password (gated by the shared PasswordStrengthIndicator,
 * exactly like RegisterPage and AcceptInvitePage) and submits it to
 * POST /api/auth/reset-password. On success they are sent to /login to sign in
 * with the new password — reset does NOT return a session.
 *
 * Layout/theme mirror AcceptInvitePage: shared input/button classes, the
 * PasswordInput show/hide toggle, the strength checklist, and a fixed-height
 * region for the mismatch hint / submit error so the card height stays constant.
 *
 * NOTE: the backend POST /api/auth/reset-password endpoint is CS-3 task T9 and
 * is not implemented yet, so this page will surface an error on submit until
 * that lands. The frontend contract (token in query, {reset_token, new_password}
 * body, 200 / 400 expired / 422 weak) is already wired here.
 */
import React, { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import PasswordInput from "../components/auth/PasswordInput";
import PasswordStrengthIndicator, {
  getPasswordRequirements,
} from "../components/auth/PasswordStrengthIndicator";
import { resetPassword } from "../api/authApi";
import { useTheme } from "../context/ThemeContext";
import { useToast } from "../components/common/Toast";
import { ApiError } from "../lib/apiClient";

// ── Shared styling constants (kept in sync with Login/Register/AcceptInvite) ──

const SUBMIT_CLS =
  "w-full inline-flex min-h-9 items-center justify-center whitespace-nowrap rounded-md " +
  "px-3 py-2 text-sm font-medium transition-[border-color,background-color,box-shadow,color,opacity] " +
  "focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 " +
  "disabled:cursor-not-allowed disabled:opacity-40 " +
  "border border-accent bg-accent text-textwhite shadow-sm hover:bg-accent/90";

// ── Error message resolver ────────────────────────────────────────────────────

function resetPasswordErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 400) return "This reset link has expired. Request a new one.";
    if (err.status === 422) return "Please check your password and try again.";
  }
  return "Something went wrong. Please try again.";
}

// ── Page shell (logo + centred card) ──────────────────────────────────────────

function PageShell({ theme, children }: { theme: string; children: React.ReactNode }) {
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
        {children}
      </div>
    </div>
  );
}

// ── Component ────────────────────────────────────────────────────────────────

export default function ResetPasswordPage() {
  const { theme } = useTheme();
  const { push } = useToast();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const resetToken = searchParams.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // CS-3: same locked strength rule + helper as the other password-creation pages.
  const passwordValid = getPasswordRequirements(password).every((r) => r.met);
  const passwordMismatch =
    confirmPassword.length > 0 && password !== confirmPassword;

  const canSubmit =
    passwordValid && password === confirmPassword && !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await resetPassword(resetToken, password);
      // Reset does not establish a session — send the user to sign in afresh.
      push("Password reset. Please log in.", "success");
      navigate("/login");
    } catch (err) {
      setError(resetPasswordErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  // ── Missing token — error card, no form. ────────────────────────────────────
  if (!resetToken) {
    return (
      <PageShell theme={theme}>
        <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-6 py-8 text-center shadow-xl shadow-black/20">
          <p className="text-sm font-medium text-red-400" data-testid="reset-invalid-error">
            This reset link is invalid. Request a new one.
          </p>
          <p className="mt-4 text-sm text-muted">
            <Link
              to="/login"
              className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
            >
              Back to sign in
            </Link>
          </p>
        </div>
      </PageShell>
    );
  }

  // ── Token present — new-password form. ──────────────────────────────────────
  return (
    <PageShell theme={theme}>
      <div className="rounded-xl border border-border bg-panel px-6 py-8 shadow-xl shadow-black/20">
        <h1 className="mb-1 text-center text-xl font-semibold text-text">Reset your password</h1>
        <p className="mb-6 text-center text-sm text-muted">
          Choose a new password for your AgentIQ account.
        </p>

        <form onSubmit={handleSubmit} noValidate>
          {/* New password */}
          <div className="mb-3">
            <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-text">
              New password
            </label>
            <PasswordInput
              id="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={password}
              onChange={setPassword}
              disabled={submitting}
            />
            {/* CS-3: same live requirement checklist as register / accept-invite. */}
            <PasswordStrengthIndicator password={password} />
          </div>

          {/* Confirm new password */}
          <div>
            <label htmlFor="confirm-password" className="mb-1.5 block text-sm font-medium text-text">
              Confirm new password
            </label>
            <PasswordInput
              id="confirm-password"
              autoComplete="new-password"
              required
              invalid={passwordMismatch}
              value={confirmPassword}
              onChange={setConfirmPassword}
              disabled={submitting}
            />
          </div>

          {/*
           * One fixed-height region for the mismatch hint and the submit error.
           * They can never co-occur (submit only fires when passwords match), so
           * sharing the slot keeps the card height constant.
           */}
          <div className="mb-2 mt-1 min-h-[2rem]">
            {passwordMismatch ? (
              <p className="text-xs leading-4 text-red-400">Passwords do not match.</p>
            ) : error ? (
              <p
                role="alert"
                data-testid="reset-password-error"
                className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-1.5 text-xs text-red-400"
              >
                {error}
              </p>
            ) : null}
          </div>

          <button type="submit" disabled={!canSubmit} className={SUBMIT_CLS}>
            {submitting ? "Resetting password…" : "Reset password"}
          </button>
        </form>
      </div>
    </PageShell>
  );
}
