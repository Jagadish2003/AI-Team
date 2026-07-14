/**
 * AUTH-1 / AT-239 — Accept invite page.
 *
 * Section 6 behaviour:
 *   Reads invite_token from the URL query param `?token=<value>`.
 *   On mount it resolves the token via GET /api/auth/invite-info (without
 *   consuming it) so the page can greet the invitee by org name AND show an
 *   "invalid / expired / already used" state immediately — reopening a spent
 *   link no longer renders the empty password form.
 *   POST /api/auth/accept-invite via AuthContext.acceptInvite() activates the
 *   account (is_active=False → True), returns a JWT, and redirects to
 *   /integration-hub. Single-use — a second attempt returns 400.
 *
 * Layout/theme mirrors LoginPage and RegisterPage: shared input/button classes,
 * the PasswordInput show/hide toggle, and fixed-height hint slots so the card
 * height stays constant as inline errors appear and clear.
 */
import React, { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import PasswordInput from "../components/auth/PasswordInput";
import PasswordStrengthIndicator, {
  getPasswordRequirements,
} from "../components/auth/PasswordStrengthIndicator";
import { getInviteInfo } from "../api/authApi";
import { useAuth } from "../context/AuthContext";
import { useTheme } from "../context/ThemeContext";
import { ApiError } from "../lib/apiClient";

// ── Validation ────────────────────────────────────────────────────────────────

const MIN_PASSWORD_LENGTH = 8;

// ── Shared styling constants (kept in sync with Login/Register) ───────────────

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

type InviteState = "loading" | "valid" | "invalid";

export default function AcceptInvitePage() {
  const { acceptInvite } = useAuth();
  const { theme } = useTheme();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();

  const inviteToken = searchParams.get("token") ?? "";

  // Token resolution (mount-time, non-consuming).
  const [inviteState, setInviteState] = useState<InviteState>(
    inviteToken ? "loading" : "invalid"
  );
  const [orgName, setOrgName] = useState<string | null>(null);
  const [inviteError, setInviteError] = useState<string>(
    "Invalid invite link. Please ask your administrator to send a new invitation."
  );

  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Resolve the token once on mount. A used/expired/invalid token (400) flips the
  // page straight to the error state — no empty form for a spent link.
  useEffect(() => {
    if (!inviteToken) return;
    let cancelled = false;
    getInviteInfo(inviteToken)
      .then((info) => {
        if (cancelled) return;
        setOrgName(info.org_name ?? null);
        setInviteState("valid");
      })
      .catch((err) => {
        if (cancelled) return;
        setInviteError(acceptInviteErrorMessage(err));
        setInviteState("invalid");
      });
    return () => {
      cancelled = true;
    };
  }, [inviteToken]);

  // CS-3: the full strength rule (length + upper + lower + special) replaces the
  // old length-only check, shared with the indicator below the field. The confirm
  // match and not-submitting conditions are unchanged.
  const passwordValid = getPasswordRequirements(password).every((r) => r.met);
  const passwordMismatch =
    confirmPassword.length > 0 && password !== confirmPassword;

  const canSubmit =
    passwordValid &&
    password === confirmPassword &&
    !submitting;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;
    setError(null);
    setSubmitting(true);
    try {
      await acceptInvite(inviteToken, password);
      // SPA navigate — no document reload. App.tsx's SessionBoundary is keyed on
      // the auth token, so the new session remounts the data-provider subtree
      // once with a clean per-user slate (what the old hardRedirect() full reload
      // provided), in a single load.
      navigate("/integration-hub", { replace: true });
    } catch (err) {
      setError(acceptInviteErrorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  // ── Invalid / used / expired token — error card, no form. ───────────────────
  if (inviteState === "invalid") {
    return (
      <PageShell theme={theme}>
        <div className="rounded-xl border border-red-500/20 bg-red-500/10 px-6 py-8 text-center shadow-xl shadow-black/20">
          <p className="text-sm font-medium text-red-400" data-testid="invite-invalid-error">
            {inviteError}
          </p>
        </div>
      </PageShell>
    );
  }

  // ── Resolving the token. ────────────────────────────────────────────────────
  if (inviteState === "loading") {
    return (
      <PageShell theme={theme}>
        <div className="rounded-xl border border-border bg-panel px-6 py-8 text-center shadow-xl shadow-black/20">
          <p className="text-sm text-muted" data-testid="invite-loading">
            Checking your invitation…
          </p>
        </div>
      </PageShell>
    );
  }

  // ── Valid token — password setup form. ──────────────────────────────────────
  const greeting = orgName
    ? `You have been invited to ${orgName}'s AgentIQ. Set password to activate your account.`
    : "You have been invited to AgentIQ. Set password to activate your account.";

  return (
    <PageShell theme={theme}>
      <div className="rounded-xl border border-border bg-panel px-6 py-8 shadow-xl shadow-black/20">
        <h1 className="mb-1 text-center text-xl font-semibold text-text">Set your password</h1>
        <p className="mb-6 text-center text-sm text-muted">{greeting}</p>

        <form onSubmit={handleSubmit} noValidate>
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
              value={password}
              onChange={setPassword}
              disabled={submitting}
            />
            {/* CS-3: live requirement checklist replaces the old length-only hint. */}
            <PasswordStrengthIndicator password={password} />
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
           * sharing the slot keeps the card height constant.
           */}
          <div className="mb-2 mt-1 min-h-[2rem]">
            {passwordMismatch ? (
              <p className="text-xs leading-4 text-red-400">Passwords do not match.</p>
            ) : error ? (
              <p
                role="alert"
                data-testid="accept-invite-error"
                className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-1.5 text-xs text-red-400"
              >
                {error}
              </p>
            ) : null}
          </div>

          <button type="submit" disabled={!canSubmit} className={SUBMIT_CLS}>
            {submitting ? "Activating account…" : "Activate account"}
          </button>
        </form>
      </div>
    </PageShell>
  );
}
