import { Mail, Trash2 } from "lucide-react";
import { apiDelete, ApiError } from "../../lib/apiClient";
import { useAuth } from "../../context/AuthContext";
import { useToast } from "../common/Toast";

export interface WorkspaceMember {
  user_id: string;
  email: string;
  role: "owner" | "analyst" | "viewer";
  created_at: string;
}

interface Props {
  member: WorkspaceMember;
  isOwner: boolean;
  onRemove: () => void;
}

const ROLE_BADGE: Record<WorkspaceMember["role"], string> = {
  owner: "border-amber-500/35 bg-amber-500/10 text-amber-300",
  analyst: "border-accent/35 bg-accent/10 text-accent",
  viewer: "border-border/60 bg-bg/30 text-muted",
};

function formatJoinedDate(raw: string): string {
  if (!raw) return "Unavailable";
  const d = new Date(raw);
  if (isNaN(d.getTime())) return "Unavailable";
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric" });
}

function roleLabel(role: WorkspaceMember["role"]): string {
  return role.charAt(0).toUpperCase() + role.slice(1);
}

export default function MemberRow({ member, isOwner, onRemove }: Props) {
  const { user } = useAuth();
  const { push } = useToast();

  const isSelf =
    member.user_id === user?.id ||
    member.email === user?.email;

  async function handleRemove() {
    const confirmed = window.confirm(
      `Remove ${member.email} from this workspace?`
    );
    if (!confirmed) return;

    if (isSelf) {
      push("You cannot remove yourself from the workspace.", "error");
      return;
    }

    try {
      await apiDelete(`/api/workspace/members/${member.user_id}`);
      onRemove();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        push("You cannot remove yourself from the workspace.", "error");
      } else {
        push("Failed to remove member. Please try again.", "error");
      }
    }
  }

  return (
    <div
      role="listitem"
      className="flex min-h-[82px] flex-col gap-3 bg-panel px-4 py-3 transition-colors hover:bg-accent/5 lg:flex-row lg:items-center lg:justify-between"
    >
      <div className="flex min-w-0 items-center gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-border/60 bg-bg/30 text-muted">
          <Mail size={16} />
        </span>
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <p className="max-w-full truncate text-sm font-semibold text-text">{member.email}</p>
            {isSelf && (
              <span className="rounded-full border border-border/60 bg-bg/30 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted">
                You
              </span>
            )}
          </div>
          <p className="mt-0.5 text-xs text-muted">
            Joined {formatJoinedDate(member.created_at)}
          </p>
        </div>
      </div>

      <div className="flex shrink-0 items-center justify-between gap-3 pl-12 lg:pl-0">
        <span
          className={`inline-flex min-w-[74px] items-center justify-center rounded-full border px-2.5 py-1 text-xs font-semibold ${ROLE_BADGE[member.role]}`}
        >
          {roleLabel(member.role)}
        </span>

        {isOwner && (
          <button
            type="button"
            onClick={handleRemove}
            className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md border border-transparent px-2.5 text-xs font-semibold text-red-300 transition-colors hover:border-red-500/30 hover:bg-red-500/10 hover:text-red-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-red-500/30"
          >
            <Trash2 size={13} />
            Remove
          </button>
        )}
      </div>
    </div>
  );
}
