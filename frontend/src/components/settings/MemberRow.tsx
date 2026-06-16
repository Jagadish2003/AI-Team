import { apiPost, ApiError } from "../../lib/apiClient";
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
  owner: "bg-purple-500/20 text-purple-300 border border-purple-400/30",
  analyst: "bg-blue-500/20 text-blue-300 border border-blue-400/30",
  viewer: "bg-slate-500/20 text-slate-300 border border-slate-400/30",
};

function formatJoinedDate(raw: string): string {
  if (!raw) return "—";
  const d = new Date(raw);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", { month: "short", year: "numeric" });
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
      await apiPost(`/api/auth/workspace/members/${member.user_id}/remove`, {});
      onRemove();
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        // Backend self-removal guard (belt-and-suspenders for the isSelf check above)
        push("You cannot remove yourself from the workspace.", "error");
      } else if (err instanceof ApiError) {
        const body = err.body as Record<string, unknown> | null;
        const detail =
          typeof body?.detail === "string"
            ? body.detail
            : `Failed to remove member (${err.status}).`;
        push(detail, "error");
      } else {
        push("An unexpected error occurred.", "error");
      }
    }
  }

  return (
    <div className="flex items-center justify-between gap-4 rounded-lg border border-white/10 bg-white/5 px-4 py-3">
      {/* Left: email + joined date */}
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-white">{member.email}</p>
        <p className="mt-0.5 text-xs text-slate-500">
          Joined {formatJoinedDate(member.created_at)}
        </p>
      </div>

      {/* Role badge */}
      <span
        className={`shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium capitalize ${ROLE_BADGE[member.role]}`}
      >
        {member.role}
      </span>

      {/* Remove button — visible to Owners only */}
      {isOwner && (
        <button
          onClick={handleRemove}
          className="shrink-0 rounded-md px-3 py-1.5 text-xs font-medium text-red-400 transition-colors
            hover:bg-red-500/10 hover:text-red-300 active:bg-red-500/20"
        >
          Remove
        </button>
      )}
    </div>
  );
}
