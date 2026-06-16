import React, { useEffect, useRef, useState } from "react";
import { Check, Copy, Send, X } from "lucide-react";
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 px-4 backdrop-blur-sm">
      <div className="relative w-full max-w-md rounded-xl border border-border bg-panel shadow-2xl shadow-black/25">
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-text">Invite team member</h2>
            <p className="mt-1 text-xs text-muted">Add a teammate to this workspace.</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-panel2 hover:text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-5">
          {inviteLink ? (
            <div className="space-y-4">
              <p className="text-sm leading-relaxed text-muted">
                Invitation created for <span className="font-medium text-text">{email}</span>.
              </p>

              <div className="rounded-lg border border-border bg-bg/30 p-2">
                <div className="flex items-center gap-2">
                  <code className="min-w-0 flex-1 select-all overflow-x-auto whitespace-nowrap rounded-md bg-panel/60 px-2 py-1.5 text-xs text-text">
                    {inviteLink}
                  </code>
                  <button
                    type="button"
                    onClick={handleCopy}
                    className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-md border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent transition-colors hover:bg-accent/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                  >
                    {copied ? <Check size={13} /> : <Copy size={13} />}
                    {copied ? "Copied" : "Copy"}
                  </button>
                </div>
              </div>

              <div className="flex justify-end pt-1">
                <button
                  type="button"
                  onClick={handleDone}
                  className="rounded-lg border border-accent/30 bg-accent px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                >
                  Done
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={handleSubmit} noValidate className="space-y-4">
              <div className="space-y-1.5">
                <label htmlFor="invite-email" className="block text-sm font-medium text-text">
                  Email address
                </label>
                <input
                  id="invite-email"
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="colleague@company.com"
                  className="w-full rounded-lg border border-border bg-bg/30 px-3 py-2 text-sm text-text placeholder:text-muted/60 outline-none transition-colors hover:border-accent/40 focus:border-accent focus:ring-2 focus:ring-accent/20"
                />
              </div>

              <div className="space-y-1.5">
                <label htmlFor="invite-role" className="block text-sm font-medium text-text">
                  Role
                </label>
                <select
                  id="invite-role"
                  value={role}
                  onChange={(e) => setRole(e.target.value as Role)}
                  className="w-full rounded-lg border border-border bg-bg/30 px-3 py-2 text-sm text-text outline-none transition-colors hover:border-accent/40 focus:border-accent focus:ring-2 focus:ring-accent/20"
                >
                  <option value="analyst">Analyst</option>
                  <option value="viewer">Viewer</option>
                </select>
                <p className="text-xs text-muted">Owner role is not assignable via invite.</p>
              </div>

              {error && (
                <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                  {error}
                </p>
              )}

              <div className="flex justify-end gap-3 pt-1">
                <button
                  type="button"
                  onClick={onClose}
                  className="rounded-lg border border-border bg-panel px-4 py-2 text-sm font-medium text-muted transition-colors hover:bg-panel2 hover:text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting}
                  className="inline-flex items-center justify-center gap-2 rounded-lg border border-accent/30 bg-accent px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                >
                  <Send size={15} />
                  {submitting ? "Sending..." : "Send invite"}
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
