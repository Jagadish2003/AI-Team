import { useEffect, useState } from "react";
import { apiGet, ApiError } from "../../lib/apiClient";
import { useAuth } from "../../context/AuthContext";
import MemberRow, { WorkspaceMember } from "./MemberRow";
import InviteModal from "./InviteModal";

/**
 * CS-1 T4 — Workspace members orchestration panel.
 *
 * Owns the member list, the refresh trigger (refreshKey), and the invite-modal
 * open state. MemberRow (T6) and InviteModal (T5) are presentational children
 * that receive their data and callbacks from here.
 *
 * Backend caveat (raised in review): GET /api/workspace/members is currently
 * gated require_role("owner"), so Analyst/Viewer users receive a 403 here and
 * will not see a read-only list as AC9 implies. The 403 is surfaced as a clear
 * permission message rather than swallowed; whether the backend should relax
 * the gate (or AC9 change) is a follow-up for the team.
 */
export default function WorkspaceMembersPanel() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";

  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [inviteModalOpen, setInviteModalOpen] = useState(false);

  // Incrementing refreshKey re-runs the fetch effect — used after a successful
  // invite or removal so the displayed list always reflects server truth.
  const onRefresh = () => setRefreshKey((k) => k + 1);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    apiGet<WorkspaceMember[]>("/api/workspace/members")
      .then((data) => {
        if (!cancelled) setMembers(data);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 403) {
          setError("You don't have permission to view workspace members.");
        } else {
          setError("Failed to load workspace members. Please try again.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  return (
    <section className="mt-8">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-base font-semibold text-white">Workspace members</h2>
        {isOwner && (
          <button
            type="button"
            onClick={() => setInviteModalOpen(true)}
            className="shrink-0 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-indigo-500 active:bg-indigo-700"
          >
            Invite member
          </button>
        )}
      </div>

      <div className="mt-4">
        {loading ? (
          <p className="text-sm text-slate-400">Loading members…</p>
        ) : error ? (
          <p className="rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
            {error}
          </p>
        ) : members.length === 0 ? (
          <p className="text-sm text-slate-400">No members in this workspace yet.</p>
        ) : (
          <div role="list" className="space-y-2">
            {members.map((m) => (
              <MemberRow
                key={m.user_id}
                member={m}
                isOwner={isOwner}
                onRemove={onRefresh}
              />
            ))}
          </div>
        )}
      </div>

      <InviteModal
        open={inviteModalOpen}
        onClose={() => setInviteModalOpen(false)}
        onSuccess={onRefresh}
      />
    </section>
  );
}
