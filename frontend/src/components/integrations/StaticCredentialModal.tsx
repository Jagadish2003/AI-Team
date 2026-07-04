/**
 * R17-D3 Addendum A (T12 / AC10) — static-credential entry form.
 *
 * The credential form for connectors that authenticate with a URL + username +
 * token/password (Jira, ServiceNow, native DBs). An Owner enters the values and
 * they are POSTed to the backend, which Fernet-encrypts them into the caller's
 * org vault (services/staticApi.saveConnectorCredentials).
 *
 * WRITE-ONLY (AC10): the secret field is never pre-filled and no endpoint ever
 * returns the secret or username, so an admin can REPLACE a credential but never
 * read one back. The non-secret base_url MAY be pre-filled from the status
 * endpoint purely as a convenience when replacing an existing credential.
 *
 * Owner-gating lives in the caller (StaticCredentialManager) — this modal is only
 * opened for Owners.
 */
import React, { useEffect, useState } from "react";
import { X, KeyRound } from "lucide-react";
import { Connector } from "../../types/connector";
import { ApiError } from "../../lib/apiClient";
import { saveConnectorCredentials } from "../../services/staticApi";
import { useToast } from "../common/Toast";
import PasswordInput from "../auth/PasswordInput";
import { staticCredentialFields } from "./staticCredentialConnectors";

interface Props {
  open: boolean;
  connector: Connector;
  /** Whether a credential already exists (drives the "replace" copy). */
  configured?: boolean;
  /** Non-secret instance URL to pre-fill when replacing. Never the secret. */
  existingBaseUrl?: string | null;
  onClose: () => void;
  /** Called after a successful save so the caller can refresh status. */
  onSuccess: () => void;
}

const INPUT_CLS =
  "w-full rounded-lg border border-border bg-bg/30 px-3 py-2 text-sm text-text " +
  "placeholder:text-muted/60 outline-none transition-colors hover:border-accent/40 " +
  "focus:border-accent focus:ring-2 focus:ring-accent/20";

export default function StaticCredentialModal({
  open,
  connector,
  configured = false,
  existingBaseUrl = null,
  onClose,
  onSuccess,
}: Props) {
  const toast = useToast();
  const fields = staticCredentialFields(connector.id);

  const [baseUrl, setBaseUrl] = useState("");
  const [username, setUsername] = useState("");
  const [secret, setSecret] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset on open/close. The secret is ALWAYS blank — it is never read back
  // (AC10). base_url (non-secret) is seeded from status only as a convenience.
  useEffect(() => {
    if (open) {
      setBaseUrl(existingBaseUrl ?? "");
      setUsername("");
      setSecret("");
      setError(null);
      setSubmitting(false);
    }
  }, [open, existingBaseUrl]);

  if (!open || !fields) return null;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    const trimmedUrl = baseUrl.trim();
    const trimmedUser = username.trim();
    if (!trimmedUrl || !trimmedUser || !secret.trim()) {
      setError(
        `Enter the ${fields!.urlLabel}, ${fields!.usernameLabel}, and ${fields!.secretLabel}.`,
      );
      return;
    }

    setSubmitting(true);
    try {
      await saveConnectorCredentials(connector.id, {
        base_url: trimmedUrl,
        username: trimmedUser,
        secret,
      });
      toast.push(
        `${connector.name} credentials ${configured ? "updated" : "saved"}.`,
        "success",
      );
      onSuccess();
      onClose();
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as Record<string, unknown> | null;
        setError(
          typeof body?.detail === "string"
            ? body.detail
            : `Request failed (${err.status}).`,
        );
      } else {
        setError("An unexpected error occurred.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 px-4 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label={`${connector.name} credentials`}
    >
      <div className="relative w-full max-w-md rounded-xl border border-border bg-panel shadow-2xl shadow-black/25">
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-semibold text-text">
              <KeyRound size={16} className="shrink-0 text-accent" />
              {configured ? "Update" : "Enter"} {connector.name} credentials
            </h2>
            <p className="mt-1 text-xs text-muted">{fields.hint}</p>
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
          {configured && (
            <p className="mb-4 rounded-lg border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
              A credential is already stored for {connector.name}. Saving replaces
              it — the existing value can never be shown.
            </p>
          )}

          <form onSubmit={handleSubmit} noValidate className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="cred-url" className="block text-sm font-medium text-text">
                {fields.urlLabel}
              </label>
              <input
                id="cred-url"
                type="text"
                value={baseUrl}
                onChange={(e) => setBaseUrl(e.target.value)}
                placeholder={fields.urlPlaceholder}
                autoComplete="off"
                className={INPUT_CLS}
              />
            </div>

            <div className="space-y-1.5">
              <label
                htmlFor="cred-username"
                className="block text-sm font-medium text-text"
              >
                {fields.usernameLabel}
              </label>
              <input
                id="cred-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder={fields.usernamePlaceholder}
                autoComplete="off"
                className={INPUT_CLS}
              />
            </div>

            <div className="space-y-1.5">
              <label
                htmlFor="cred-secret"
                className="block text-sm font-medium text-text"
              >
                {fields.secretLabel}
              </label>
              {/* Never pre-filled; autoComplete=new-password so browsers don't
                  autofill a stored value into this write-only field (AC10). */}
              <PasswordInput
                id="cred-secret"
                value={secret}
                onChange={setSecret}
                placeholder={`Enter ${fields.secretLabel.toLowerCase()}`}
                autoComplete="new-password"
                disabled={submitting}
              />
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
                {submitting ? "Saving..." : configured ? "Replace credential" : "Save credential"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
