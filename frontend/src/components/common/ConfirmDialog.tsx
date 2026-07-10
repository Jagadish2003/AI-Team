/**
 * ConfirmDialog — reusable confirmation modal (R18-C0 P4 / AT-566).
 *
 * A small, dark-theme confirmation dialog for destructive/irreversible actions
 * that need an explicit confirm step (e.g. disconnecting a connector). Follows
 * the same overlay + panel markup as StaticCredentialModal so it looks native to
 * the Integration Hub, and uses only existing surface tokens (bg-panel,
 * border-border, text-muted) so it themes correctly.
 *
 * Controlled: the parent owns `open` and both callbacks. While `busy` is true the
 * confirm button shows a pending label and both actions are disabled so the
 * request cannot be double-fired. Escape and the overlay backdrop trigger
 * onCancel (ignored while busy).
 */
import React, { useEffect } from 'react';
import { AlertTriangle, X } from 'lucide-react';

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  /** Body copy — string or arbitrary node (e.g. a highlighted connector name). */
  message: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Pending label shown on the confirm button while the action runs. */
  busyLabel?: string;
  /** true → confirm button uses the danger styling (default true). */
  danger?: boolean;
  /** true → confirm/cancel disabled and confirm shows busyLabel. */
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  busyLabel = 'Working…',
  danger = true,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  // Close on Escape (unless a request is in flight).
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape' && !busy) onCancel();
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;

  const confirmCls = danger
    ? 'border-red-500/40 bg-red-500/90 text-white hover:bg-red-500'
    : 'border-accent/30 bg-accent text-white hover:bg-accent/90';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 px-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onClick={() => { if (!busy) onCancel(); }}
    >
      <div
        className="relative w-full max-w-md rounded-xl border border-border bg-panel shadow-2xl shadow-black/25"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
          <h2 className="flex items-center gap-2 text-base font-semibold text-text">
            {danger && <AlertTriangle size={16} className="shrink-0 text-red-300" />}
            {title}
          </h2>
          <button
            type="button"
            onClick={() => { if (!busy) onCancel(); }}
            disabled={busy}
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-muted transition-colors hover:bg-panel2 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            aria-label="Close"
          >
            <X size={18} />
          </button>
        </div>

        <div className="px-5 py-5">
          <div className="text-sm leading-relaxed text-muted">{message}</div>

          <div className="mt-5 flex justify-end gap-3">
            <button
              type="button"
              onClick={onCancel}
              disabled={busy}
              className="rounded-lg border border-border bg-panel px-4 py-2 text-sm font-medium text-muted transition-colors hover:bg-panel2 hover:text-text disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
            >
              {cancelLabel}
            </button>
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy}
              className={`inline-flex items-center justify-center gap-2 rounded-lg border px-5 py-2 text-sm font-semibold transition-colors disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30 ${confirmCls}`}
            >
              {busy ? busyLabel : confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
