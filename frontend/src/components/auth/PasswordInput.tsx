/**
 * AUTH-1 / AT-239 — Password input with a show/hide eye toggle.
 *
 * Shared by LoginPage and RegisterPage. Keeps the placeholder faint (so it does
 * not read like already-typed credentials) and lets the user reveal the value.
 *
 * The toggle button's accessible name is "Show password" / "Hide password".
 * The field's own label lives in the parent (htmlFor → id), so queries by the
 * exact label text ("Password", "Confirm password") still resolve the input
 * unambiguously.
 */
import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";

interface PasswordInputProps {
  id: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  autoComplete?: string;
  disabled?: boolean;
  /** Renders the field with a danger border (e.g. password mismatch). */
  invalid?: boolean;
  minLength?: number;
  required?: boolean;
}

const BASE_CLS =
  "w-full rounded-lg border bg-bg px-3 py-2.5 pr-10 text-sm text-text " +
  "placeholder:text-muted/40 focus:outline-none focus:ring-2 disabled:opacity-50";

export default function PasswordInput({
  id,
  value,
  onChange,
  placeholder = "••••••••",
  autoComplete = "current-password",
  disabled = false,
  invalid = false,
  minLength,
  required = false,
}: PasswordInputProps) {
  const [show, setShow] = useState(false);

  const borderCls = invalid
    ? "border-red-500/50 focus:ring-red-500/20"
    : "border-border focus:border-accent/60 focus:ring-accent/30";

  return (
    <div className="relative">
      <input
        id={id}
        type={show ? "text" : "password"}
        autoComplete={autoComplete}
        required={required}
        minLength={minLength}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className={`${BASE_CLS} ${borderCls}`}
      />
      <button
        type="button"
        onClick={() => setShow((s) => !s)}
        aria-label={show ? "Hide password" : "Show password"}
        title={show ? "Hide password" : "Show password"}
        className="absolute right-2 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md text-muted transition-colors hover:text-text focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
      >
        {show ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
      </button>
    </div>
  );
}
