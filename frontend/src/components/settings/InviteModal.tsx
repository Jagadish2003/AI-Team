import React, { useEffect, useRef, useState } from "react";
import { X, Copy, Check } from "lucide-react";
import { apiPost, ApiError } from "../../lib/apiClient";
import { useToast } from "../common/Toast";

interface InviteResponse {
  invite_token?: string;
}

interface Props {
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

type Role = "analyst" | "viewer";

function isValidEmail(email: string): boolean {
  const at = email.indexOf("@");
  if (at < 1) return false;
  const dot = email.indexOf(".", at + 1);
  return dot > at + 1 && dot < email.length - 1;
}

export default function InviteModal({ open, onClose, onSuccess }: Props) {
  const toast = useToast();

  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("analyst");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [inviteLink, setInviteLink] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset all state whenever the modal closes
  useEffect(() => {
    if (!open) {
      setEmail("");
      setRole("analyst");
      setSubmitting(false);
      setError(null);
      setInviteLink(null);
      setCopied(false);
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    }
  }, [open]);

  if (!open) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!isValidEmail(email)) {
      setError("Enter a valid email address.");
      return;
    }

    setSubmitting(true);
    try {
      const result = await apiPost<InviteResponse>("/api/auth/invite", { email, role });

      if (result.invite_token) {
        const link = `${window.location.protocol}//${window.location.host}/accept-invite?token=${result.invite_token}`;
        setInviteLink(link);
      } else {
        // Production: backend returns 501 until email delivery is wired.
        // When a tokenless 201 eventually ships, this branch handles it.
        // NOTE: The AC says "production returns 201 no token", but the current
        // backend returns 501 ("Email delivery not configured") — reconcile when
        // email delivery is implemented.
        toast.push(`Invitation sent to ${email}.`, "success");
        onSuccess();
        onClose();
      }
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as Record<string, unknown> | null;
        const detail =
          typeof body?.detail === "string"
            ? body.detail
            : `Request failed (${err.status}).`;
        setError(detail);
      } else {
        setError("An unexpected error occurred.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  function handleCopy() {
    if (!inviteLink) return;
    navigator.clipboard.writeText(inviteLink).then(() => {
      setCopied(true);
      copyTimerRef.current = setTimeout(() => setCopied(false), 2000);
    });
  }

  function handleDone() {
    onSuccess();
    onClose();
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="relative w-full max-w-md rounded-2xl border border-white/10 bg-[#0f1117] p-6 shadow-2xl">
        {/* Header */}
        <div className="mb-5 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-white">Invite team member</h2>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-white/10 hover:text-white"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        {inviteLink ? (
          /* ── Link display state ── */
          <div className="space-y-4">
            <p className="text-sm text-slate-300">
              Invitation created. Share this link with <span className="font-medium text-white">{email}</span>:
            </p>

            <div className="flex items-center gap-2 rounded-lg border border-white/10 bg-white/5 p-2">
              <code className="flex-1 select-all overflow-x-auto whitespace-nowrap text-xs text-slate-200">
                {inviteLink}
              </code>
              <button
                onClick={handleCopy}
                className="flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors
                  bg-indigo-600 text-white hover:bg-indigo-500 active:bg-indigo-700"
              >
                {copied ? <Check size={13} /> : <Copy size={13} />}
                {copied ? "Copied!" : "Copy link"}
              </button>
            </div>

            <div className="flex justify-end pt-1">
              <button
                onClick={handleDone}
                className="rounded-lg bg-indigo-600 px-5 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-500"
              >
                Done
              </button>
            </div>
          </div>
        ) : (
          /* ── Form state ── */
          <form onSubmit={handleSubmit} noValidate className="space-y-4">
            {/* Email */}
            <div className="space-y-1.5">
              <label htmlFor="invite-email" className="block text-sm font-medium text-slate-300">
                Email address
              </label>
              <input
                id="invite-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="colleague@company.com"
                className="w-full rounded-lg border border-white/10 bg-white/5 px-3 py-2 text-sm text-white
                  placeholder-slate-500 outline-none transition focus:border-indigo-500 focus:ring-1
                  focus:ring-indigo-500"
              />
            </div>

            {/* Role */}
            <div className="space-y-1.5">
              <label htmlFor="invite-role" className="block text-sm font-medium text-slate-300">
                Role
              </label>
              <select
                id="invite-role"
                value={role}
                onChange={(e) => setRole(e.target.value as Role)}
                className="w-full rounded-lg border border-white/10 bg-[#1a1d27] px-3 py-2 text-sm text-white
                  outline-none transition focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
              >
                <option value="analyst">Analyst</option>
                <option value="viewer">Viewer</option>
              </select>
              <p className="text-xs text-slate-500">Owner role is not assignable via invite.</p>
            </div>

            {/* Inline error */}
            {error && (
              <p className="rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {error}
              </p>
            )}

            {/* Actions */}
            <div className="flex justify-end gap-3 pt-1">
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg px-4 py-2 text-sm font-medium text-slate-400 transition-colors hover:bg-white/10 hover:text-white"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting}
                className="rounded-lg bg-indigo-600 px-5 py-2 text-sm font-medium text-white transition-colors
                  hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {submitting ? "Sending…" : "Send invite"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
