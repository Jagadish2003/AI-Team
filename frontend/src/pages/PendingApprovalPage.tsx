/**
 * AUTH-2 T6 — Pending approval confirmation page.
 *
 * Shown after a successful registration. In AUTH-2 a new org is created with
 * approval_status='pending_approval' and NO JWT is issued, so the registrant is
 * not logged in. This page is a static confirmation only:
 *   - it stores nothing in browser storage (there is no token to store),
 *   - it does NOT poll any endpoint,
 *   - it does NOT auto-redirect.
 * The registrant learns the outcome by email (org_approved.html /
 * org_rejected.html). The only navigation offered is a manual "back to sign in"
 * link the user can choose to click.
 */
import React from "react";
import { Link } from "react-router-dom";
import { Clock } from "lucide-react";

import { useTheme } from "../context/ThemeContext";

export default function PendingApprovalPage() {
  const { theme } = useTheme();

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-12">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="mb-8 flex justify-center">
          <img
            src={theme === "dark" ? "/Logo-Dark.svg" : "/Logo-Light.svg"}
            alt="AgentIQ"
            className="h-10 w-auto"
          />
        </div>

        {/* Card */}
        <div className="rounded-xl border border-border bg-panel px-6 py-8 text-center shadow-xl shadow-black/20">
          <div className="mb-5 flex justify-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-full border border-accent/30 bg-accent/10">
              <Clock className="h-6 w-6 text-accent" aria-hidden="true" />
            </div>
          </div>

          <h1 className="mb-3 text-xl font-semibold text-text">
            Registration submitted
          </h1>

          <p className="text-sm leading-6 text-muted">
            Your organisation has been submitted for approval. You will receive an email once it is reviewed.
          </p>

          <p className="mt-6 text-sm text-muted">
            <Link
              to="/login"
              className="font-medium text-accent hover:underline focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
            >
              Back to sign in
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}
