/**
 * CS-3 (Section 6) — Forgot password page.
 *
 * Behaviour:
 *   A single email input. On submit, POST /api/auth/forgot-password.
 *   The backend ALWAYS returns 200 whether or not the email is registered
 *   (anti-enumeration, AC11), so on success we always render the same neutral
 *   confirmation: "If that email is registered, a reset link has been sent."
 *   A reset link is delivered out-of-band by email (CS-3 email service); this
 *   page never reveals whether an account exists.
 *
 * Layout/theme mirrors LoginPage and RegisterPage: shared input/button classes,
 * fixed-height hint slots, logo + centred card.
 */
import React, { useState } from "react";
import { Link } from "react-router-dom";

import { forgotPassword } from "../api/authApi";
import { useTheme } from "../context/ThemeContext";

// ── Validation ────────────────────────────────────────────────────────────────

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// ── Shared styling constants (kept in sync with Login/Register) ───────────────

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
const HINT_SLOT_CLS = "mt-1 h-4 text-xs leading-4 text-red-400";

// One fixed message shown after a successful submit, regardless of whether the
// email is registered — never reveals account existence (AC11).
const CONFIRMATION =
  "If that email is registered, a reset link has been sent.";

// ── Component ────────────────────────────────────────────────────────────────

export default function ForgotPasswordPage() {
  const { theme } = useTheme();

  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const emailInvalid = email.trim().length > 0 && !EMAIL_RE.test(email.trim());
  const canSubmit = EMAIL_RE.test(email.trim()) && !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await forgotPassword(email.trim().toLowerCase());
      // Any completed response (the backend always 200s) → same neutral message.
      setSubmitted(true);
    } catch {
      // Only a transport-level failure (request never reached the server) lands
      // here. Surface a generic retry prompt — this leaks nothing about the email.
      setError("Something went wrong. Please try again.");
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
          {submitted ? (
            // ── Confirmation state — same message for every email. ────────────
            <>
              <h1 className="mb-3 text-center text-xl font-semibold text-text">
                Check your email
              </h1>
              <p
                className="text-center text-sm text-muted"
                data-testid="forgot-password-confirmation"
              >
                {CONFIRMATION}
              </p>
              <p className="mt-6 text-center text-sm text-muted">
                <Link
                  to="/login"
                  className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                >
                  Back to sign in
                </Link>
              </p>
            </>
          ) : (
            // ── Request form. ─────────────────────────────────────────────────
            <>
              <h1 className="text-center text-xl font-semibold text-text">
                Forgot password?
              </h1>
              <p className="mb-4 mt-2 text-center text-sm text-muted">
                Enter your email and we'll send you a link to reset it.
              </p>

              {/* Submit error sits below the intro, in a fixed-height slot. */}
              <div className="mb-2 min-h-[2rem]">
                {error && (
                  <p
                    role="alert"
                    data-testid="forgot-password-error"
                    className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-1.5 text-xs text-red-400"
                  >
                    {error}
                  </p>
                )}
              </div>

              <form onSubmit={handleSubmit} noValidate>
                <div className="mb-1">
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
                  <div className={HINT_SLOT_CLS}>
                    {emailInvalid && "Enter a valid email address (e.g. you@company.com)."}
                  </div>
                </div>

                <button type="submit" disabled={!canSubmit} className={SUBMIT_CLS}>
                  {submitting ? "Sending…" : "Send reset link"}
                </button>
              </form>

              <p className="mt-5 text-center text-sm text-muted">
                Remembered it?{" "}
                <Link
                  to="/login"
                  className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
                >
                  Sign in
                </Link>
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
