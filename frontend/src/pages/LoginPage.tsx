/**
 * AUTH-1 / AT-239 — Login page.
 *
 * Section 6 behaviour:
 *   POST /api/auth/login via AuthContext.login().
 *   Shows the submit error (401 / 429) directly below the "Sign in" heading,
 *   in a fixed-height slot so the card never grows/jumps.
 *   Redirects to /integration-hub on success.
 *   Page refresh = re-login (token lives in React state only, Section 3).
 *
 * Client-side validation mirrors RegisterPage: the email must look like
 * you@company.com and the password must be at least 8 characters. Inline hints
 * sit in fixed-height slots beneath each field, and submit stays disabled until
 * both are satisfied.
 */
import React, { useState } from "react";
import { Link } from "react-router-dom";

import PasswordInput from "../components/auth/PasswordInput";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { hardRedirect } from "../utils/navigation";
import { ApiError } from "../lib/apiClient";

// ── Validation ────────────────────────────────────────────────────────────────

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MIN_PASSWORD_LENGTH = 8;

// ── Shared input/button class constants ──────────────────────────────────────

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

// ── Error message resolver ────────────────────────────────────────────────────

/**
 * Pull retry_after (seconds) out of a 429 body and convert to whole minutes,
 * rounded up. The backend nests it under `detail` (FastAPI HTTPException), and
 * carries it in the body — not just the Retry-After header — because that header
 * is not CORS-exposed to the SPA. Returns null when the value is absent/invalid.
 */
function retryAfterMinutes(body: unknown): number | null {
  const detail = (body as { detail?: unknown } | null)?.detail;
  const seconds =
    detail && typeof detail === "object"
      ? (detail as { retry_after?: unknown }).retry_after
      : undefined;
  if (typeof seconds === "number" && Number.isFinite(seconds) && seconds > 0) {
    return Math.max(1, Math.ceil(seconds / 60));
  }
  return null;
}

function loginErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "Invalid email or password.";
    if (err.status === 429) {
      const minutes = retryAfterMinutes(err.body);
      if (minutes != null) {
        return `Too many failed attempts. Wait for ${minutes} ${
          minutes === 1 ? "minute" : "minutes"
        }.`;
      }
      return "Too many failed attempts. Please wait before trying again.";
    }
  }
  return "Something went wrong. Please try again.";
}

// ── Component ────────────────────────────────────────────────────────────────

export default function LoginPage() {
  const { login } = useAuth();
  const { theme } = useTheme();

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Inline validation — each only surfaces once the user has typed something.
  const emailInvalid = email.trim().length > 0 && !EMAIL_RE.test(email.trim());
  const passwordTooShort =
    password.length > 0 && password.length < MIN_PASSWORD_LENGTH;

  const canSubmit =
    EMAIL_RE.test(email.trim()) &&
    password.length >= MIN_PASSWORD_LENGTH &&
    !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await login(email.trim().toLowerCase(), password);
      // Full reload (not SPA navigate) so all in-session context is rebuilt for
      // this user — otherwise the previous user's connector/run state leaks.
      hardRedirect("/integration-hub");
    } catch (err) {
      setError(loginErrorMessage(err));
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
          <h1 className="text-center text-xl font-semibold text-text">Sign in</h1>

          {/* Submit error sits directly below the heading, in a fixed-height slot. */}
          <div className="mb-2 mt-2 min-h-[2rem]">
            {error && (
              <p
                role="alert"
                data-testid="login-error"
                className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-1.5 text-xs text-red-400"
              >
                {error}
              </p>
            )}
          </div>

          <form onSubmit={handleSubmit} noValidate>
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
              <div className={HINT_SLOT_CLS}>
                {emailInvalid && "Enter a valid email address (e.g. you@company.com)."}
              </div>
            </div>

            {/* Password */}
            <div className="mb-1">
              <label htmlFor="password" className="mb-1.5 block text-sm font-medium text-text">
                Password
              </label>
              <PasswordInput
                id="password"
                autoComplete="current-password"
                required
                minLength={MIN_PASSWORD_LENGTH}
                invalid={passwordTooShort}
                value={password}
                onChange={setPassword}
                disabled={submitting}
              />
              <div className={HINT_SLOT_CLS}>
                {passwordTooShort && "Enter minimum of 8 characters"}
              </div>
            </div>

            <button type="submit" disabled={!canSubmit} className={SUBMIT_CLS}>
              {submitting ? "Signing in…" : "Sign in"}
            </button>
          </form>

          <p className="mt-5 text-center text-sm text-muted">
            Don't have an account?{" "}
            <Link
              to="/register"
              className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
            >
              Register
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
