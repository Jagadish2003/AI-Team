/**
 * LIC-1 / T8 (AT-349) — Admin License page (Owner only).
 *
 * Read-only status panel (§7): who the license is issued to, the term, the
 * expiry date and days remaining, plus a status badge mirroring LicenseStatus
 * (green = valid, amber = grace, red = read-only / invalid). An Owner can paste
 * a renewal key, which is validated immediately by the backend and, on success,
 * updates the stored license and refreshes status with no restart (AC7).
 *
 * Owner-only (AC9): Analyst and Viewer are redirected away and never see the
 * page content. The data endpoints are also Owner-gated server-side (T6).
 */
import React, { useEffect, useState } from "react";
import { Navigate } from "react-router-dom";

import PageShell from "../components/common/PageShell";
import LoadingPanel from "../components/common/LoadingPanel";
import ErrorPanel from "../components/common/ErrorPanel";
import Button from "../components/common/Button";
import { useToast } from "../components/common/Toast";
import { useAuth } from "../context/AuthContext";
import { ApiError } from "../lib/apiClient";
import { fetchLicenseStatus, updateLicenseKey } from "../api/licenseApi";
import type { LicenseStatusResponse, LicenseStatusValue } from "../types/license";

type BadgeTone = "green" | "amber" | "red";

const STATUS_BADGE: Record<LicenseStatusValue, { label: string; tone: BadgeTone }> = {
  valid: { label: "Valid", tone: "green" },
  grace: { label: "Grace", tone: "amber" },
  readonly: { label: "Read-only", tone: "red" },
  invalid: { label: "Invalid", tone: "red" },
};

const TONE_CLS: Record<BadgeTone, string> = {
  green: "bg-green-500/15 text-green-300 border-green-500/30",
  amber: "bg-amber-500/15 text-amber-200 border-amber-500/30",
  red: "bg-red-500/15 text-red-300 border-red-500/30",
};

function StatusBadge({ status }: { status: string }) {
  // Any non valid/grace/readonly value (no_license, clock_rollback, …) reads as
  // a red "invalid"-style badge — there is no usable license in those states.
  const meta = STATUS_BADGE[status as LicenseStatusValue] ?? STATUS_BADGE.invalid;
  return (
    <span
      data-testid="license-status-badge"
      data-tone={meta.tone}
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-xs font-semibold leading-none ${TONE_CLS[meta.tone]}`}
    >
      {meta.label}
    </span>
  );
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs uppercase tracking-wide text-muted">{label}</dt>
      <dd className="text-sm font-medium text-text">{value ?? "—"}</dd>
    </div>
  );
}

export default function LicensePage() {
  const { user } = useAuth();
  const { push } = useToast();

  const [status, setStatus] = useState<LicenseStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [keyInput, setKeyInput] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function load() {
    setLoading(true);
    setLoadError(null);
    try {
      setStatus(await fetchLicenseStatus());
    } catch (err) {
      const msg =
        err instanceof ApiError
          ? `Could not load license status (${err.status}).`
          : "Could not load license status.";
      setLoadError(msg);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // Only the Owner ever loads license data (the route is Owner-gated below).
    if (user?.role === "owner") {
      void load();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.role]);

  // AC9 — Owner only. Analyst/Viewer never mount the content.
  if (user && user.role !== "owner") {
    return <Navigate to="/integration-hub" replace />;
  }

  async function handleUpdate() {
    const key = keyInput.trim();
    if (!key || submitting) return;
    setSubmitting(true);
    try {
      const refreshed = await updateLicenseKey(key);
      setStatus(refreshed); // refresh immediately, no restart (AC7)
      setKeyInput("");
      push("License updated.", "success");
    } catch (err) {
      // Validate-before-store: a rejected key changes nothing server-side, and
      // we keep the displayed status untouched.
      if (err instanceof ApiError) {
        push("This key is not valid", "error");
      } else {
        push("Could not update the license key.", "error");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <PageShell
      title="License"
      description="Term-based AgentIQ license issued by CloudFulcrum. Paste a renewal key here when your term is up — it takes effect immediately."
    >
      {loading ? (
        <LoadingPanel title="Loading license…" subtitle="Reading the installed license." />
      ) : loadError ? (
        <ErrorPanel message={loadError} onRetry={load} />
      ) : (
        <div className="space-y-5">
          {/* Status + details panel */}
          <section className="rounded-xl border border-border bg-panel px-5 py-4 shadow-sm">
            <div className="mb-4 flex items-center gap-3">
              <h2 className="text-sm font-semibold text-text">Status</h2>
              <StatusBadge status={status?.status ?? "invalid"} />
            </div>
            <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <Detail label="Issued to" value={status?.customer} />
              <Detail
                label="Term"
                value={status?.term != null ? `${status.term} months` : null}
              />
              <Detail label="Expiry" value={status?.expires_at} />
              <Detail
                label="Days remaining"
                value={status?.days_remaining != null ? `${status.days_remaining}` : null}
              />
            </dl>
          </section>

          {/* Update key panel */}
          <section className="rounded-xl border border-border bg-panel px-5 py-4 shadow-sm">
            <h2 className="text-sm font-semibold text-text">Update license key</h2>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              Paste the new key from CloudFulcrum. It is validated before it is stored — an
              invalid key is rejected and your current license is left untouched.
            </p>
            <textarea
              aria-label="License key"
              value={keyInput}
              onChange={(e) => setKeyInput(e.target.value)}
              placeholder="payload.signature"
              rows={3}
              className="mt-3 w-full resize-y rounded-md border border-border bg-buttonbg px-3 py-2 font-mono text-xs text-text focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
            />
            <div className="mt-3 flex justify-end">
              <Button
                onClick={handleUpdate}
                disabled={submitting || keyInput.trim().length === 0}
              >
                {submitting ? "Validating…" : "Update key"}
              </Button>
            </div>
          </section>
        </div>
      )}
    </PageShell>
  );
}
