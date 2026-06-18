/**
 * CS-3 — Password strength indicator.
 *
 * A real-time checklist of the password rules, rendered below the password
 * input on every flow where a user *creates* a password: RegisterPage,
 * AcceptInvitePage, and ResetPasswordPage. It is intentionally NOT used on
 * LoginPage — login does not enforce the strength rule, so existing users with
 * older passwords are never blocked.
 *
 * The rule is locked (CS-3 §1): minimum 8 characters with at least one
 * uppercase letter, one lowercase letter, and one special character. The four
 * patterns here mirror the backend's validate_password_strength() so the
 * frontend and backend agree on what "valid" means, and every page gates its
 * submit button on the same getPasswordRequirements() helper instead of
 * re-deriving the regexes locally.
 */
import { Check, Circle } from "lucide-react";

export interface Requirement {
  label: string;
  met: boolean;
}

// Mirrors PASSWORD_RULES in backend/app/auth/user_auth.py. Keep in sync.
const SPECIAL_CHAR_RE = /[!@#$%^&*()_+\-=[\]{}|;:,.<>?]/;

/**
 * The four locked password requirements, each tagged with whether the supplied
 * password currently satisfies it. An empty password yields four unmet rules.
 * Pages call `.every(r => r.met)` on the result to decide whether to enable
 * their submit button.
 */
export function getPasswordRequirements(password: string): Requirement[] {
  return [
    { label: "At least 8 characters", met: password.length >= 8 },
    { label: "One uppercase letter (A-Z)", met: /[A-Z]/.test(password) },
    { label: "One lowercase letter (a-z)", met: /[a-z]/.test(password) },
    {
      label: "One special character (!@#$%^&*...)",
      met: SPECIAL_CHAR_RE.test(password),
    },
  ];
}

export default function PasswordStrengthIndicator({
  password,
}: {
  password: string;
}) {
  const requirements = getPasswordRequirements(password);

  return (
    <ul className="mt-2 space-y-1" aria-label="Password requirements">
      {requirements.map((req) => (
        <li
          key={req.label}
          data-testid="password-requirement"
          data-met={req.met}
          className={`flex items-center gap-2 text-xs leading-4 ${
            req.met ? "text-green-500" : "text-muted"
          }`}
        >
          {req.met ? (
            <Check className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          ) : (
            <Circle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          )}
          <span>{req.label}</span>
          {/* Screen-reader-only state so the checklist is meaningful without colour. */}
          <span className="sr-only">{req.met ? "(met)" : "(not met)"}</span>
        </li>
      ))}
    </ul>
  );
}
