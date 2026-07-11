/**
 * R17-D3 Addendum A (T12 / AC10) — Integration Hub credential management for a
 * static-credential connector (Jira, ServiceNow, native DBs).
 *
 * Renders inside ConnectorDetailPanel. Shows whether a credential is configured
 * (non-secret status only) and, for OWNERS, a button to enter/replace it and to
 * remove it. The actual values are entered in StaticCredentialModal and are
 * write-only end to end — the status shown here comes from a metadata-only
 * endpoint that never returns the username or secret (AC10).
 *
 * Owner-gating uses the real logged-in role from AuthContext (NOT the env-based
 * dev shim the scope pickers use): only `user.role === "owner"` may manage
 * credentials, matching "entered per org through the Integration Hub by an Owner".
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, KeyRound, Lock, Trash2 } from "lucide-react";
import { Connector, OutboundSetupRequest } from "../../types/connector";
import {
  ConnectorCredentialStatus,
  deleteConnectorCredentials,
  fetchConnectorCredentialStatus,
} from "../../services/staticApi";
import { ApiError } from "../../lib/apiClient";
import { useAuthOptional } from "../../context/AuthContext";
import { useToast } from "../common/Toast";
import { staticCredentialFields } from "./staticCredentialConnectors";
import StaticCredentialModal from "./StaticCredentialModal";

function formatUpdated(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export default function StaticCredentialManager({
  connector,
  outboundSetupRequest = null,
}: {
  connector: Connector;
  outboundSetupRequest?: OutboundSetupRequest | null;
}) {
  const auth = useAuthOptional();
  const isOwner = auth?.user?.role === "owner";
  const toast = useToast();
  const fields = staticCredentialFields(connector.id);

  const [status, setStatus] = useState<ConnectorCredentialStatus | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [removing, setRemoving] = useState(false);

  const loadStatus = useCallback(() => {
    let alive = true;
    fetchConnectorCredentialStatus(connector.id)
      .then((s) => {
        if (alive) setStatus(s);
      })
      .catch(() => {
        // Non-fatal: leave status unknown; the Owner can still enter credentials.
        if (alive) setStatus(null);
      });
    return () => {
      alive = false;
    };
  }, [connector.id]);

  useEffect(() => {
    setConfirmRemove(false);
    return loadStatus();
  }, [loadStatus]);

  // R18-A3 follow-up: pop the credential form when the tile's outbound/credential
  // setup button is clicked (the parent bumps outboundSetupRequest.nonce). Guarded
  // by connectorId + a consumed-nonce ref so it fires once per click and never on
  // an unrelated connector's mount.
  const consumedNonce = useRef(0);
  useEffect(() => {
    if (!outboundSetupRequest) return;
    if (outboundSetupRequest.nonce === consumedNonce.current) return;
    consumedNonce.current = outboundSetupRequest.nonce;
    if (outboundSetupRequest.connectorId === connector.id && fields) {
      setModalOpen(true);
    }
  }, [outboundSetupRequest, connector.id, fields]);

  if (!fields) return null;

  const configured = status?.configured ?? false;
  const updatedLabel = formatUpdated(status?.updated_at ?? null);

  async function handleRemove() {
    if (!confirmRemove) {
      setConfirmRemove(true);
      return;
    }
    setRemoving(true);
    try {
      await deleteConnectorCredentials(connector.id);
      toast.push(`${connector.name} credentials removed.`, "success");
      setConfirmRemove(false);
      loadStatus();
    } catch (err) {
      const detail =
        err instanceof ApiError && typeof (err.body as any)?.detail === "string"
          ? (err.body as any).detail
          : "Could not remove credentials.";
      toast.push(detail, "error");
    } finally {
      setRemoving(false);
    }
  }

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-medium text-text">
          <KeyRound size={14} className="shrink-0 text-accent" />
          Connection credentials
        </div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          Vault-encrypted
        </div>
      </div>

      {/* Status — non-secret metadata only. */}
      <div className="flex items-center gap-2 rounded-md border border-border bg-bg/20 px-3 py-2 text-xs">
        {configured ? (
          <>
            <CheckCircle2 size={14} className="shrink-0 text-emerald-400" />
            <span className="min-w-0 flex-1 break-words text-text">
              Credentials configured
              {updatedLabel ? ` · updated ${updatedLabel}` : ""}
              {status?.base_url ? (
                <span className="block truncate text-muted">{status.base_url}</span>
              ) : null}
            </span>
          </>
        ) : (
          <>
            <Lock size={14} className="shrink-0 text-muted" />
            <span className="min-w-0 flex-1 text-muted">Not configured yet</span>
          </>
        )}
      </div>

      {isOwner ? (
        <div className="mt-2 flex items-center gap-2">
          <button
            type="button"
            onClick={() => setModalOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md border border-accent/30 bg-accent/10 px-3 py-1.5 text-xs font-semibold text-accent transition-colors hover:bg-accent/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
          >
            <KeyRound size={13} />
            {configured ? "Update credentials" : "Enter credentials"}
          </button>
          {configured && (
            <button
              type="button"
              onClick={handleRemove}
              disabled={removing}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted transition-colors hover:border-red-500/40 hover:text-red-300 disabled:cursor-not-allowed disabled:opacity-60 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
            >
              <Trash2 size={13} />
              {confirmRemove ? "Click to confirm" : "Remove"}
            </button>
          )}
        </div>
      ) : (
        <p className="mt-2 text-[11px] leading-relaxed text-muted">
          Only workspace Owners can manage connection credentials.
        </p>
      )}

      <StaticCredentialModal
        open={modalOpen}
        connector={connector}
        configured={configured}
        existingBaseUrl={status?.base_url ?? null}
        onClose={() => setModalOpen(false)}
        onSuccess={loadStatus}
      />
    </div>
  );
}
