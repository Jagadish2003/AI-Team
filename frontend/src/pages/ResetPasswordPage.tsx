/**
 * CS-3 (Section 6) — Reset password page.
 *
 * Behaviour:
 *   Reads the reset token from the URL query param `?token=<value>`.
 *   Shows a new-password field with the live PasswordStrengthIndicator and a
 *   confirm field. Submit stays disabled until all four strength requirements
 *   are met and the two entries match.
 *   POST /api/auth/reset-password. On success, SPA-navigates to /login with a
 *   success toast ("Password reset. Please log in."). On 400 (invalid/expired
 *   token) shows "This reset link has expired. Request a new one." On 422 the
 *   backend rejected the password as weak — surfaced inline.
 *
 * SPA navigation (useNavigate) is intentional here, not the hardRedirect used by
 * login/register/accept-invite: the user has NO session after a reset, so there
 * is no per-user context to rebuild, and a full reload would discard the toast.
 *
 * Layout/theme mirrors LoginPage, RegisterPage, and AcceptInvitePage.
 */
import React, { useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";

import PasswordInput from "../components/auth/PasswordInput";
import PasswordStrengthIndicator, {
  isPasswordStrong,
} from "../components/auth/PasswordStrengthIndicator";
import { resetPassword } from "../api/authApi";
import { useTheme } from "../context/ThemeContext";
import { useToast } from "../components/common/Toast";
import { ApiError } from "../lib/apiClient";

// ── Shared styling constants (kept in sync with the other auth pages) ─────────

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
    if (err.status === 422) return "Please choose a stronger password and try again.";
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

  // Inline validation — each only surfaces once the user has typed something.
  const passwordStrong = isPasswordStrong(password);
  const passwordMismatch =
    confirmPassword.length > 0 && password !== confirmPassword;

  const canSubmit =
    passwordStrong && password === confirmPassword && !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await resetPassword(resetToken, password);
      push("Password reset. Please log in.", "success");
      navigate("/login", { replace: true });
    } catch (err) {
      setError(resetPasswordErrorMessage(err));
      setSubmitting(false);
    }
    // On success we navigate away, so `submitting` is intentionally left true —
    // the button stays disabled during the transition rather than flashing back.
  }

  // ── Missing token — error card, no form. ────────────────────────────────────
  if (!resetToken) {
    return (
      <PageShell theme={theme}>
        <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-6 py-8 text-center shadow-xl shadow-black/20">
          <p className="text-sm font-medium text-red-400" data-testid="reset-invalid-error">
            This reset link is invalid. Request a new one.
          </p>
          <p className="mt-6 text-sm text-muted">
            <Link
              to="/forgot-password"
              className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
            >
              Request a new reset link
            </Link>
          </p>
        </div>
      </PageShell>
    );
  }

  // ── Valid token present — new-password form. ────────────────────────────────
  return (
    <PageShell theme={theme}>
      <div className="rounded-xl border border-border bg-panel px-6 py-8 shadow-xl shadow-black/20">
        <h1 className="mb-1 text-center text-xl font-semibold text-text">
          Set a new password
        </h1>
        <p className="mb-6 text-center text-sm text-muted">
          Choose a new password for your account.
        </p>

        <form onSubmit={handleSubmit} noValidate>
          {/* New password */}
          <div className="mb-1">
            <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-text">
              New password
            </label>
            <PasswordInput
              id="password"
              autoComplete="new-password"
              required
              invalid={password.length > 0 && !passwordStrong}
              value={password}
              onChange={setPassword}
              disabled={submitting}
            />
            <PasswordStrengthIndicator password={password} />
          </div>

          {/* Confirm password */}
          <div className="mt-4">
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
            {submitting ? "Resetting…" : "Reset password"}
          </button>
        </form>

        <p className="mt-5 text-center text-sm text-muted">
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
