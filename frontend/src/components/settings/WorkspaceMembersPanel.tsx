import { useEffect, useState } from "react";
import { AlertCircle, Loader2, UserPlus, UsersRound } from "lucide-react";
import { apiGet, ApiError } from "../../lib/apiClient";
import { useAuth } from "../../context/AuthContext";
import MemberRow, { WorkspaceMember } from "./MemberRow";
import InviteModal from "./InviteModal";

export default function WorkspaceMembersPanel() {
  const { user } = useAuth();
  const isOwner = user?.role === "owner";

  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [inviteModalOpen, setInviteModalOpen] = useState(false);

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

  const ownerCount = members.filter((member) => member.role === "owner").length;
  const analystCount = members.filter((member) => member.role === "analyst").length;
  const viewerCount = members.filter((member) => member.role === "viewer").length;
  const memberListScrollable = members.length > 10;

  return (
    <section className="overflow-hidden rounded-xl border border-border/50 bg-panel shadow-sm">
      <div className="flex flex-col gap-4 border-b border-border/50 px-4 py-4 md:flex-row md:items-center md:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border/60 bg-bg/30 text-accent">
            <UsersRound size={18} />
          </span>
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-text">Workspace members</h2>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              Review who can access this workspace and adjust membership as teams change.
            </p>
          </div>
        </div>

        {isOwner && (
          <button
            type="button"
            onClick={() => setInviteModalOpen(true)}
            className="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg border border-accent/30 bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent/90 active:bg-accent/80 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30"
          >
            <UserPlus size={16} />
            Invite member
          </button>
        )}
      </div>

      <div className="grid grid-cols-2 gap-px border-b border-border/50 bg-border/50 md:grid-cols-4">
        <div className="bg-panel px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Members</div>
          <div className="mt-1 text-xl font-semibold text-text">{members.length}</div>
        </div>
        <div className="bg-panel px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Owners</div>
          <div className="mt-1 text-xl font-semibold text-text">{ownerCount}</div>
        </div>
        <div className="bg-panel px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Analysts</div>
          <div className="mt-1 text-xl font-semibold text-text">{analystCount}</div>
        </div>
        <div className="bg-panel px-4 py-3">
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted">Viewers</div>
          <div className="mt-1 text-xl font-semibold text-text">{viewerCount}</div>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 px-4 py-6 text-sm text-muted">
          <Loader2 size={16} className="animate-spin text-accent" />
          Loading members...
        </div>
      ) : error ? (
        <div className="m-4 flex items-start gap-2 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
          <AlertCircle size={16} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      ) : members.length === 0 ? (
        <div className="px-4 py-10 text-center">
          <UsersRound size={24} className="mx-auto text-muted/70" />
          <p className="mt-3 text-sm font-medium text-text">No members in this workspace yet.</p>
          <p className="mt-1 text-xs text-muted">Invite teammates to start collaborating in AgentIQ.</p>
        </div>
      ) : (
        <div
          role="list"
          className={`grid grid-cols-1 gap-px bg-border/50 md:grid-cols-2 ${
            memberListScrollable
              ? "max-h-[430px] overflow-y-auto [scrollbar-gutter:stable]"
              : ""
          }`}
        >
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

      <InviteModal
        open={inviteModalOpen}
        onClose={() => setInviteModalOpen(false)}
        onSuccess={onRefresh}
      />
    </section>
  );
}
