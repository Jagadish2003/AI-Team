/**
 * AUTH-1 / AT-239 — Registration page.
 *
 * Section 6 behaviour:
 *   POST /api/auth/register via AuthContext.register().
 *   Creates an org, user (identity only), and workspace_member (owner) in one transaction.
 *   409 → email already registered.
 *   Redirects to /integration-hub on success.
 *
 * Layout note: every inline message (email format, password length, mismatch)
 * and the submit error live in fixed-height slots, so the card height stays
 * constant and never jumps as messages appear or clear.
 */
import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import PasswordInput from "../components/auth/PasswordInput";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { ApiError } from "../lib/apiClient";

// ── Validation ────────────────────────────────────────────────────────────────

// Accepts abc@xyz.com, abc.m@xy.org, etc. — a local part, "@", a domain, a dot,
// and a TLD, with no whitespace anywhere.
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MIN_PASSWORD_LENGTH = 8;

// ── Shared styling constants ──────────────────────────────────────────────────

const INPUT_CLS =
  "w-full rounded-lg border border-border bg-bg px-3 py-2.5 text-sm text-text " +
  "placeholder:text-muted/40 focus:border-accent/60 focus:outline-none focus:ring-2 " +
  "focus:ring-accent/30 disabled:opacity-50";

const SUBMIT_CLS =
  "w-full inline-flex min-h-9 items-center justify-center whitespace-nowrap rounded-md " +
  "px-3 py-2 text-sm font-medium transition-[border-color,background-color,box-shadow,color,opacity] " +
  "focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40 " +
  "disabled:cursor-not-allowed disabled:opacity-40 " +
  "border border-accent bg-accent text-textwhite shadow-sm hover:bg-accent/90";

// Fixed-height slot for a single line of inline hint/error text.
const HINT_SLOT_CLS = "mt-1 h-4 text-xs leading-4";

// ── Error message resolver ────────────────────────────────────────────────────

function registerErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return "That email address is already registered. Please sign in.";
    if (err.status === 422) return "Please check your inputs and try again.";
  }
  return "Something went wrong. Please try again.";
}

// ── Component ────────────────────────────────────────────────────────────────

export default function RegisterPage() {
  const { register } = useAuth();
  const { theme } = useTheme();
  const navigate = useNavigate();

  const [orgName, setOrgName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Inline validation — each only surfaces once the user has typed something.
  const emailInvalid = email.trim().length > 0 && !EMAIL_RE.test(email.trim());
  const passwordTooShort =
    password.length > 0 && password.length < MIN_PASSWORD_LENGTH;
  const passwordMismatch =
    confirmPassword.length > 0 && password !== confirmPassword;

  const canSubmit =
    orgName.trim().length > 0 &&
    EMAIL_RE.test(email.trim()) &&
    password.length >= MIN_PASSWORD_LENGTH &&
    password === confirmPassword &&
    !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await register(orgName.trim(), email.trim().toLowerCase(), password);
      navigate("/integration-hub", { replace: true });
    } catch (err) {
      setError(registerErrorMessage(err));
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
          <h1 className="mb-6 text-center text-xl font-semibold text-text">Create account</h1>

          <form onSubmit={handleSubmit} noValidate>
            {/* Organisation name */}
            <div className="mb-3">
              <label htmlFor="org-name" className="mb-1.5 block text-sm font-medium text-text">
                Organisation name
              </label>
              <input
                id="org-name"
                type="text"
                autoComplete="organization"
                required
                value={orgName}
                onChange={(e) => setOrgName(e.target.value)}
                className={INPUT_CLS}
                placeholder="Acme Corp"
                disabled={submitting}
              />
              {/* Reserved slot keeps the field rhythm even (no hint for this field). */}
              <div className={HINT_SLOT_CLS} aria-hidden="true" />
            </div>

            {/* Email */}
            <div className="mb-3">
              <label htmlFor="email" className="mb-1.5 block text-sm font-medium text-text">
                Email
              </label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className={`${INPUT_CLS} ${emailInvalid ? "border-red-500/50 focus:ring-red-500/20" : ""}`}
                placeholder="you@company.com"
                disabled={submitting}
              />
              <div className={`${HINT_SLOT_CLS} text-red-400`}>
                {emailInvalid && "Enter a valid email address (e.g. you@company.com)."}
              </div>
            </div>

            {/* Password */}
            <div className="mb-3">
              <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-text">
                Password
              </label>
              <PasswordInput
                id="password"
                autoComplete="new-password"
                required
                minLength={MIN_PASSWORD_LENGTH}
                invalid={passwordTooShort}
                value={password}
                onChange={setPassword}
                disabled={submitting}
              />
              {/* Length hint — red, like the other errors. Shown only while too short. */}
              <div className={`${HINT_SLOT_CLS} text-red-400`}>
                {passwordTooShort && "Enter minimum of 8 characters"}
              </div>
            </div>

            {/* Confirm password */}
            <div>
              <label htmlFor="confirm-password" className="mb-1.5 block text-sm font-medium text-text">
                Confirm password
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
             * sharing the slot keeps the card height constant while trimming the
             * gap before the Create account button.
             */}
            <div className="mb-2 mt-1 min-h-[2rem]">
              {passwordMismatch ? (
                <p className="text-xs leading-4 text-red-400">Passwords do not match.</p>
              ) : error ? (
                <p
                  role="alert"
                  data-testid="register-error"
                  className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-1.5 text-xs text-red-400"
                >
                  {error}
                </p>
              ) : null}
            </div>

            <button type="submit" disabled={!canSubmit} className={SUBMIT_CLS}>
              {submitting ? "Creating account…" : "Create account"}
            </button>
          </form>

          <p className="mt-5 text-center text-sm text-muted">
            Already have an account?{" "}
            <Link
              to="/login"
              className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
            >
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
