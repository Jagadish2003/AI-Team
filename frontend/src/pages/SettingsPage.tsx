import React from "react";
import { ArrowLeft, ShieldCheck } from "lucide-react";
import { useNavigate } from "react-router-dom";
import PageShell from "../components/common/PageShell";
import WorkspaceMembersPanel from "../components/settings/WorkspaceMembersPanel";

export default function SettingsPage() {
  const navigate = useNavigate();

  function handleBack() {
    if (window.history.length > 1) {
      navigate(-1);
      return;
    }
    navigate("/integration-hub");
  }

  return (
    <PageShell
      title="Settings"
      description="Manage workspace access and keep team membership aligned with review responsibilities."
      actions={
        <button
          type="button"
          onClick={handleBack}
          className="inline-flex items-center justify-center gap-2 rounded-lg border border-accent/20 bg-accent/5 px-4 py-2 text-sm font-medium text-accent transition-colors hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
        >
          <ArrowLeft size={16} />
          Back
        </button>
      }
    >
      <div className="mb-4 flex items-start gap-3 rounded-xl border border-border bg-panel px-4 py-3 shadow-sm">
        <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-accent/25 bg-accent/10 text-accent">
          <ShieldCheck size={18} />
        </span>
        <div className="min-w-0">
          <div className="text-sm font-semibold text-text">Workspace administration</div>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted">
            Owners can invite teammates and remove workspace access. Analysts and viewers keep their existing role-based permissions.
          </p>
        </div>
      </div>

      <WorkspaceMembersPanel />
    </PageShell>
  );
}
