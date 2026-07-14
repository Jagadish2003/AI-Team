/**
 * R18-A3 T5 (AT-558) — Salesforce JWT-bearer outbound setup form.
 *
 * The outbound-only connect path shown in a NETWORK_PROFILE=no_public_inbound
 * deployment for a connector whose outbound mode is `jwt_bearer` (Salesforce /
 * nCino — R18-A3 T2 / AT-555). An Owner enters the connected-app login URL,
 * Salesforce username, and the cert PEM private key; they are POSTed to the
 * backend, which Fernet-encrypts them into the caller's org vault. The access
 * token then mints outbound from the assertion — no browser redirect, no inbound
 * callback.
 *
 * WRITE-ONLY (AC5): the private key is never pre-filled and no endpoint ever
 * returns it, so an Owner can REPLACE the key but never read one back.
 *
 * Owner-gating lives in the caller (OutboundAuthSetup) — this modal is only
 * opened for Owners.
 */
import React, { useEffect, useState } from 'react';
import { X, KeyRound, Trash2 } from 'lucide-react';
import { Connector } from '../../types/connector';
import { ApiError } from '../../lib/apiClient';
import {
  deleteJwtBearerCredentials,
  saveJwtBearerCredentials,
} from '../../services/staticApi';
import { useToast } from '../common/Toast';

interface Props {
  open: boolean;
  connector: Connector;
  configured?: boolean;
  existingBaseUrl?: string | null;
  onClose: () => void;
  onSuccess: () => void;
}

const INPUT_CLS =
  'w-full rounded-lg border border-border bg-bg/30 px-3 py-2 text-sm text-text ' +
  'placeholder:text-muted/60 outline-none transition-colors hover:border-accent/40 ' +
  'focus:border-accent focus:ring-2 focus:ring-accent/20';

export default function JwtBearerCredentialModal({
  open,
  connector,
  configured = false,
  existingBaseUrl = null,
  onClose,
  onSuccess,
}: Props) {
  const toast = useToast();

  const [loginUrl, setLoginUrl] = useState('');
  const [username, setUsername] = useState('');
  const [privateKey, setPrivateKey] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);

  useEffect(() => {
    if (open) {
      setLoginUrl(existingBaseUrl ?? '');
      setUsername('');
      setPrivateKey('');
      setError(null);
      setSubmitting(false);
      setConfirmRemove(false);
      setRemoving(false);
    }
  }, [open, existingBaseUrl]);

  if (!open) return null;

  async function handleRemove() {
    if (!confirmRemove) {
      setConfirmRemove(true);
      return;
    }
    setError(null);
    setRemoving(true);
    try {
      await deleteJwtBearerCredentials(connector.id);
      toast.push(`${connector.name} outbound key removed.`, 'success');
      onSuccess();
      onClose();
    } catch (err) {
      const detail =
        err instanceof ApiError && typeof (err.body as { detail?: unknown })?.detail === 'string'
          ? ((err.body as { detail?: string }).detail as string)
          : 'Could not remove the key.';
      setError(detail);
    } finally {
      setRemoving(false);
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    const trimmedUrl = loginUrl.trim();
    const trimmedUser = username.trim();
    if (!trimmedUrl || !trimmedUser || !privateKey.trim()) {
      setError('Enter the login URL, Salesforce username, and cert private key.');
      return;
    }

    setSubmitting(true);
    try {
      await saveJwtBearerCredentials(connector.id, {
        login_url: trimmedUrl,
        username: trimmedUser,
        private_key: privateKey,
      });
      toast.push(
        `${connector.name} outbound access ${configured ? 'updated' : 'saved'}.`,
        'success',
      );
      onSuccess();
      onClose();
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as Record<string, unknown> | null;
        setError(
          typeof body?.detail === 'string'
            ? body.detail
            : `Request failed (${err.status}).`,
        );
      } else {
        setError('An unexpected error occurred.');
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
      aria-label={`${connector.name} outbound access`}
    >
      <div className="relative w-full max-w-md rounded-xl border border-border bg-panel shadow-2xl shadow-black/25">
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-semibold text-text">
              <KeyRound size={16} className="shrink-0 text-accent" />
              {configured ? 'Update' : 'Set up'} {connector.name} outbound access
            </h2>
            <p className="mt-1 text-xs text-muted">
              JWT bearer — the cert private key is encrypted into your workspace
              vault and used to mint tokens outbound. No inbound callback.
            </p>
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
              A private key is already stored for {connector.name}. Saving replaces
              it — the existing key can never be shown.
            </p>
          )}

          <form onSubmit={handleSubmit} noValidate className="space-y-4">
            <div className="space-y-1.5">
              <label htmlFor="jwt-url" className="block text-sm font-medium text-text">
                Login URL
              </label>
              <input
                id="jwt-url"
                type="text"
                value={loginUrl}
                onChange={(e) => setLoginUrl(e.target.value)}
                placeholder="https://login.salesforce.com"
                autoComplete="off"
                className={INPUT_CLS}
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="jwt-username" className="block text-sm font-medium text-text">
                Salesforce username
              </label>
              <input
                id="jwt-username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="integration.user@company.com"
                autoComplete="off"
                className={INPUT_CLS}
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="jwt-key" className="block text-sm font-medium text-text">
                Cert private key (PEM)
              </label>
              {/* Never pre-filled; write-only (AC5). */}
              <textarea
                id="jwt-key"
                value={privateKey}
                onChange={(e) => setPrivateKey(e.target.value)}
                placeholder={'-----BEGIN PRIVATE KEY-----\n...'}
                autoComplete="off"
                spellCheck={false}
                rows={5}
                disabled={submitting}
                className={`${INPUT_CLS} font-mono text-xs`}
              />
            </div>

            {error && (
              <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {error}
              </p>
            )}

            <div className="flex items-center justify-between gap-3 pt-1">
              {/* Delete lives here (with Set up/Update) so the tile's setup flow
                  owns all writes; the right panel is read-only status. */}
              {configured ? (
                <button
                  type="button"
                  onClick={handleRemove}
                  disabled={submitting || removing}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-sm font-medium text-muted transition-colors hover:border-red-500/40 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
                >
                  <Trash2 size={14} />
                  {confirmRemove ? 'Click to confirm' : 'Remove'}
                </button>
              ) : (
                <span />
              )}
              <div className="flex gap-3">
                <button
                  type="button"
                  onClick={onClose}
                  className="rounded-lg border border-border bg-panel px-4 py-2 text-sm font-medium text-muted transition-colors hover:bg-panel2 hover:text-text focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={submitting || removing}
                  className="inline-flex items-center justify-center gap-2 rounded-lg border border-accent/30 bg-accent px-5 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
                >
                  {submitting ? 'Saving...' : configured ? 'Replace key' : 'Save key'}
                </button>
              </div>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
