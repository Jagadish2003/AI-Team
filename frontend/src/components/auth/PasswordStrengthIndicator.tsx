/**
 * CS-3 (Section 2) — Password strength indicator.
 *
 * Renders a real-time, four-requirement checklist beneath a password input.
 * Each requirement shows a green tick once met and a grey circle until then.
 * Used on RegisterPage, AcceptInvitePage, and ResetPasswordPage.
 *
 * The rules here MUST stay in sync with the backend `validate_password_strength`
 * (backend/app/auth/user_auth.py): minimum 8 characters, at least one uppercase
 * letter, one lowercase letter, and one special character. `getPasswordRequirements`
 * is exported so a page can gate its submit button on `.every(r => r.met)` using
 * exactly the same predicate the checklist renders.
 */

// The special-character set is kept character-for-character identical to the
// backend PASSWORD_RULES pattern so the FE indicator and BE validation agree.
export const SPECIAL_CHAR_RE = /[!@#$%^&*()_+\-=[\]{}|;:,.<>?]/;

export interface PasswordRequirement {
  label: string;
  met: boolean;
}

/**
 * Evaluate the four password-strength requirements against `password`.
 * Returns them in display order; an empty input yields all-unmet.
 */
export function getPasswordRequirements(password: string): PasswordRequirement[] {
  return [
    { label: "At least 8 characters", met: password.length >= 8 },
    { label: "One uppercase letter (A-Z)", met: /[A-Z]/.test(password) },
    { label: "One lowercase letter (a-z)", met: /[a-z]/.test(password) },
    {
      label: "One special character (!@#$%^&*…)",
      met: SPECIAL_CHAR_RE.test(password),
    },
  ];
}

/** True when every strength requirement is satisfied — the submit-gate predicate. */
export function isPasswordStrong(password: string): boolean {
  return getPasswordRequirements(password).every((r) => r.met);
}

export default function PasswordStrengthIndicator({
  password,
}: {
  password: string;
}) {
  const requirements = getPasswordRequirements(password);

  return (
    <ul className="mt-2 space-y-1" data-testid="password-strength" aria-live="polite">
      {requirements.map((req) => (
        <li
          key={req.label}
          className={`flex items-center gap-2 text-xs leading-4 transition-colors ${
            req.met ? "text-green-500" : "text-muted"
          }`}
          data-testid="password-requirement"
          data-met={req.met}
        >
          <span aria-hidden="true">{req.met ? "✓" : "○"}</span>
          <span>{req.label}</span>
        </li>
      ))}
    </ul>
  );
}
