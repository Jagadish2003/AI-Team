/**
 * 2.0-B2 T3 — Entity Match review surface (Owner/Analyst).
 *
 * The cross-source resolution engine auto-merges only where identity is STATED:
 * an explicit cross-reference in the source data, or the org's alias table. A
 * match that rests on a shared name is never merged — a wrongly merged entity
 * corrupts every finding built on it, and the corruption is invisible. Those
 * matches are queued here instead, each with the evidence behind it, for a person
 * to confirm or reject.
 *
 * What this screen does NOT do: apply a merge. Confirming records a durable,
 * attributable statement that two entities are the same thing, and stops the pair
 * being proposed again. The graph merge (with its provenance) is a separate step
 * that consumes confirmed pairs — so the copy here says "recorded", never
 * "merged".
 *
 * Viewer never mounts the content (analyst+ write workflow); the endpoints are
 * analyst-gated server-side too.
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Navigate } from "react-router-dom";

import PageShell from "../components/common/PageShell";
import LoadingPanel from "../components/common/LoadingPanel";
import ErrorPanel from "../components/common/ErrorPanel";
import Button from "../components/common/Button";
import { useToast } from "../components/common/Toast";
import { useAuthOptional } from "../context/AuthContext";
import { isViewerRole } from "../utils/roles";
import { ApiError } from "../lib/apiClient";
import {
  decideEntityMatchProposal,
  fetchEntityMatchProposals,
  scanEntityMatchProposals,
} from "../api/entityMatchProposalsApi";
import type {
  EntityMatchProposal,
  ProposalEntityView,
  ProposalStatus,
} from "../api/entityMatchProposalsApi";

const TABS: { key: ProposalStatus; label: string }[] = [
  { key: "pending", label: "Pending" },
  { key: "confirmed", label: "Confirmed" },
  { key: "rejected", label: "Rejected" },
];

const STATUS_TONE: Record<ProposalStatus, string> = {
  pending: "border-amber-500/30 bg-amber-500/15 text-amber-200",
  confirmed: "border-emerald-500/30 bg-emerald-500/15 text-emerald-300",
  rejected: "border-red-500/30 bg-red-500/15 text-red-300",
};

/** Human label for a resolution tier. Only propose-only tiers reach this screen. */
function tierLabel(tier: string): string {
  if (tier === "name_similarity") return "Name match";
  // Tier 4 (opt-in): the full names DIFFER and only a leading word matched, so the
  // label says "partial" rather than "name match" — a reviewer scanning the queue
  // should be able to see at a glance which pairs rest on the weaker signal.
  if (tier === "name_prefix_similarity") return "Partial name match";
  return tier.replace(/_/g, " ");
}

function errorDetail(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    const body = err.body;
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
    }
    return `${fallback} (${err.status}).`;
  }
  return fallback;
}

function StatusPill({ status }: { status: ProposalStatus }) {
  return (
    <span
      data-testid="proposal-status"
      data-status={status}
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[11px] font-semibold uppercase leading-none ${STATUS_TONE[status]}`}
    >
      {status}
    </span>
  );
}

/** One side of the pair: what it is called, in which system, and which record. */
function EntitySide({
  side,
  entity,
  fallbackId,
}: {
  side: "A" | "B";
  entity: ProposalEntityView | undefined;
  fallbackId: string;
}) {
  return (
    <div className="min-w-0 flex-1 rounded-lg border border-border bg-panel/60 px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted">
        Entity {side}
      </div>
      <div className="truncate text-sm font-medium text-text" title={entity?.display_name ?? fallbackId}>
        {entity?.display_name ?? fallbackId}
      </div>
      <dl className="mt-1 space-y-0.5 text-xs text-muted">
        <div className="flex gap-1">
          <dt>Source:</dt>
          <dd className="font-medium text-text">{entity?.source_system ?? "—"}</dd>
        </div>
        <div className="flex gap-1">
          <dt>Record:</dt>
          <dd className="truncate font-mono text-[11px]" title={entity?.source_record_id ?? ""}>
            {entity?.source_record_id ?? "—"}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function ProposalCard({
  proposal,
  busy,
  onDecide,
}: {
  proposal: EntityMatchProposal;
  busy: boolean;
  onDecide: (id: string, action: "confirm" | "reject") => void;
}) {
  const evidence = proposal.evidence ?? {};
  const corroborating = evidence.corroborating_relationships ?? [];
  const pending = proposal.status === "pending";

  return (
    <article
      data-testid="proposal-card"
      data-proposal-id={proposal.proposal_id}
      className="rounded-xl border border-border bg-panel px-4 py-3 shadow-sm"
    >
      <header className="mb-3 flex flex-wrap items-center gap-2">
        <StatusPill status={proposal.status} />
        <span className="rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-muted">
          {tierLabel(proposal.tier)}
        </span>
        <span className="rounded-full border border-border px-2 py-0.5 text-[11px] font-medium text-muted">
          {proposal.entity_type}
        </span>
        <span className="text-[11px] text-muted">
          confidence {proposal.confidence.toFixed(2)}
        </span>
      </header>

      <div className="flex flex-col gap-2 sm:flex-row sm:items-stretch">
        <EntitySide side="A" entity={evidence.subject} fallbackId={proposal.left_entity_id} />
        <div className="flex items-center justify-center px-1 text-xs font-semibold text-muted">
          ↔
        </div>
        <EntitySide side="B" entity={evidence.target} fallbackId={proposal.right_entity_id} />
      </div>

      {/* Why this was proposed — the reviewer decides from this, not from the
          name alone. */}
      <section className="mt-3 rounded-lg border border-border/70 bg-bg/40 px-3 py-2">
        <h3 className="text-[11px] font-semibold uppercase tracking-wide text-muted">
          Why this was proposed
        </h3>
        <p data-testid="proposal-reason" className="mt-1 text-xs leading-relaxed text-text">
          {evidence.reason || "Exact normalised name match across sources."}
        </p>
        {corroborating.length > 0 ? (
          <ul data-testid="proposal-corroboration" className="mt-1.5 space-y-0.5">
            {corroborating.map((rel) => (
              <li key={`${rel.relationship_type}:${rel.entity_id}`} className="text-xs text-muted">
                Both <span className="font-medium text-text">{rel.relationship_type}</span>{" "}
                <span className="font-mono text-[11px]">{rel.entity_id}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      {pending ? (
        <footer className="mt-3 flex flex-wrap items-center gap-2">
          <Button
            onClick={() => onDecide(proposal.proposal_id, "confirm")}
            disabled={busy}
            ariaLabel="Confirm this match"
          >
            Confirm match
          </Button>
          <Button
            variant="danger"
            onClick={() => onDecide(proposal.proposal_id, "reject")}
            disabled={busy}
            ariaLabel="Reject this match"
          >
            Not the same
          </Button>
        </footer>
      ) : (
        <footer data-testid="proposal-decided" className="mt-3 text-xs text-muted">
          {proposal.status === "confirmed" ? "Confirmed" : "Rejected"}
          {proposal.decided_by ? ` by ${proposal.decided_by}` : ""}
          {proposal.decided_at ? ` · ${proposal.decided_at}` : ""}
          {" · will not be proposed again"}
        </footer>
      )}
    </article>
  );
}

export default function EntityMatchReviewPage() {
  const auth = useAuthOptional();
  const { push } = useToast();

  const [tab, setTab] = useState<ProposalStatus>("pending");
  const [proposals, setProposals] = useState<EntityMatchProposal[]>([]);
  const [counts, setCounts] = useState<Record<ProposalStatus, number>>({
    pending: 0,
    confirmed: 0,
    rejected: 0,
  });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  const viewer = isViewerRole(auth?.user?.role);

  const load = useCallback(
    async (status: ProposalStatus) => {
      setLoading(true);
      setLoadError(null);
      try {
        const data = await fetchEntityMatchProposals(status);
        setProposals(data.proposals ?? []);
        setCounts(
          data.counts ?? { pending: 0, confirmed: 0, rejected: 0 },
        );
      } catch (err) {
        setLoadError(errorDetail(err, "Could not load entity match proposals"));
      } finally {
        setLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (viewer) return;
    void load(tab);
  }, [load, tab, viewer]);

  // Viewer never sees the content — this is an analyst+ write workflow.
  if (viewer) {
    return <Navigate to="/integration-hub" replace />;
  }

  async function handleDecide(proposalId: string, action: "confirm" | "reject") {
    if (busyId) return;
    setBusyId(proposalId);
    try {
      const outcome = await decideEntityMatchProposal(proposalId, action);
      push(
        outcome.changed
          ? action === "confirm"
            ? "Match confirmed. Recorded — it will not be proposed again."
            : "Match rejected. Recorded — it will not be proposed again."
          : "That decision was already recorded.",
        "success",
      );
      await load(tab);
    } catch (err) {
      push(errorDetail(err, "Could not record the decision"), "error");
    } finally {
      setBusyId(null);
    }
  }

  async function handleScan() {
    if (scanning) return;
    setScanning(true);
    try {
      const result = await scanEntityMatchProposals();
      const parts = [`${result.created} new`, `${result.refreshed} refreshed`];
      if (result.skipped_already_decided > 0) {
        // Reported, not hidden: the queue did not grow because those pairs were
        // already answered.
        parts.push(`${result.skipped_already_decided} already decided`);
      }
      push(`Scan complete — ${parts.join(", ")}.`, "success");
      await load(tab);
    } catch (err) {
      push(errorDetail(err, "Could not scan for matches"), "error");
    } finally {
      setScanning(false);
    }
  }

  const emptyCopy = useMemo(() => {
    if (tab === "pending") {
      return "No matches are waiting for review. Entities with an explicit cross-reference or an alias mapping resolve automatically and never appear here.";
    }
    return tab === "confirmed"
      ? "No confirmed matches yet."
      : "No rejected matches yet.";
  }, [tab]);

  return (
    <PageShell
      title="Entity Matches"
      description="Cross-source matches that the resolution engine will not merge on its own. An explicit cross-reference or an alias mapping resolves automatically; a name match is only ever proposed — confirm or reject it here."
      actions={
        <Button variant="secondary" onClick={handleScan} disabled={scanning}>
          {scanning ? "Scanning…" : "Scan for matches"}
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="flex flex-wrap gap-2" role="tablist" aria-label="Proposal status">
          {TABS.map((entry) => (
            <button
              key={entry.key}
              type="button"
              role="tab"
              aria-selected={tab === entry.key}
              data-testid={`proposal-tab-${entry.key}`}
              onClick={() => setTab(entry.key)}
              className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${
                tab === entry.key
                  ? "border-accent bg-accent/10 text-accent"
                  : "border-border text-muted hover:text-text"
              }`}
            >
              {entry.label} ({counts[entry.key] ?? 0})
            </button>
          ))}
        </div>

        {loading ? (
          <LoadingPanel
            title="Loading proposed matches…"
            subtitle="Reading this workspace's review queue."
          />
        ) : loadError ? (
          <ErrorPanel message={loadError} onRetry={() => void load(tab)} />
        ) : proposals.length === 0 ? (
          <p data-testid="proposals-empty" className="rounded-xl border border-border bg-panel px-4 py-6 text-sm text-muted">
            {emptyCopy}
          </p>
        ) : (
          <div className="space-y-3">
            {proposals.map((proposal) => (
              <ProposalCard
                key={proposal.proposal_id}
                proposal={proposal}
                busy={busyId === proposal.proposal_id}
                onDecide={handleDecide}
              />
            ))}
          </div>
        )}

        {/* Honest about the boundary: this screen records identity decisions; it
            does not itself rewrite the graph. */}
        <p className="text-xs leading-relaxed text-muted">
          Confirming records that two entities are the same thing and stops the pair
          being proposed again. Nothing is merged from this screen — the graph merge
          is a separate step that consumes confirmed matches.
        </p>
      </div>
    </PageShell>
  );
}
