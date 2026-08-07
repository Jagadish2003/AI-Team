import React from "react";
import { ArrowDown, ArrowUp, ExternalLink, Gauge } from "lucide-react";
import type {
  AdjustmentReason,
  ContributingDecisionRef,
  ContributingOutcomeRef,
  OpportunityRanking,
} from "../../types/analystReview";

function movementLabel(ranking: OpportunityRanking): string {
  const moved = ranking.reason?.ranksMoved ?? Math.abs(ranking.moved ?? 0);
  const direction = (ranking.reason?.direction ?? (ranking.moved < 0 ? "up" : "down")) === "up"
    ? "higher"
    : "lower";
  return `Ranked ${direction}${moved ? ` by ${moved} rank${moved === 1 ? "" : "s"}` : ""}`;
}

function hasReason(ranking: OpportunityRanking | null | undefined): ranking is OpportunityRanking & {
  reason: AdjustmentReason;
} {
  return Boolean(
    ranking?.adjusted &&
      ranking.moved !== undefined &&
      ranking.reason?.summary,
  );
}

function referenceLabel(ref: ContributingDecisionRef | ContributingOutcomeRef): string {
  if (ref.kind === "decision") {
    return ref.action ? `Decision: ${ref.action}` : "Decision signal";
  }
  return ref.verdict ? `Outcome: ${ref.verdict}` : "Outcome signal";
}

function ReferenceLinks({
  references,
}: {
  references: Array<ContributingDecisionRef | ContributingOutcomeRef>;
}) {
  if (references.length === 0) return null;

  return (
    <div className="mt-3 flex flex-wrap gap-2" data-testid="ranking-adjustment-links">
      {references.map((ref, index) => {
        const label = referenceLabel(ref);
        const key = `${ref.kind}-${ref.href ?? label}-${index}`;
        if (!ref.href) {
          return (
            <span
              key={key}
              className="rounded-md border border-border bg-bg/30 px-2 py-1 text-[11px] text-muted"
            >
              {label}
            </span>
          );
        }

        return (
          <a
            key={key}
            href={ref.href}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 rounded-md border border-accent/25 bg-accent/5 px-2 py-1 text-[11px] font-medium text-accent hover:border-accent/45 hover:bg-accent/10 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40"
          >
            {label}
            <ExternalLink size={11} aria-hidden />
          </a>
        );
      })}
    </div>
  );
}

export function RankingAdjustmentCompact({
  ranking,
}: {
  ranking?: OpportunityRanking | null;
}) {
  if (!hasReason(ranking)) return null;

  const DirectionIcon = ranking.reason.direction === "up" ? ArrowUp : ArrowDown;

  return (
    <div
      data-testid="ranking-adjustment-compact"
      className="mt-2 rounded-md border border-accent/20 bg-accent/5 px-2.5 py-2 text-xs text-text"
    >
      <div className="flex items-center gap-1.5 font-semibold text-accent">
        <DirectionIcon size={13} aria-hidden />
        <span>{movementLabel(ranking)}</span>
      </div>
      <p className="mt-1 line-clamp-2 leading-relaxed text-muted">
        {ranking.reason.summary}
      </p>
    </div>
  );
}

export function RankingAdjustmentPanel({
  ranking,
}: {
  ranking?: OpportunityRanking | null;
}) {
  if (!hasReason(ranking)) return null;

  const reason = ranking.reason;
  const references = [
    ...(reason.contributingDecisions ?? []),
    ...(reason.contributingOutcomes ?? []),
  ];
  const capLabel = reason.wasCapped
    ? `Cap applied: ${reason.cappedBy ? reason.cappedBy.replace(/_/g, " ") : "configured cap"}`
    : "Within configured caps";

  return (
    <div data-testid="ranking-adjustment-panel">
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Ranking Adjustment</span>
        <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
          {movementLabel(ranking)}
        </span>
      </div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
        <div className="flex items-start gap-2">
          <Gauge size={15} className="mt-0.5 shrink-0 text-accent" aria-hidden />
          <div className="min-w-0">
            <p className="text-xs font-semibold leading-relaxed text-text">
              {reason.summary}
            </p>
            <p className="mt-1 text-xs leading-relaxed text-muted">
              Ordering only. Evidence, confidence, and corroboration are unchanged.
            </p>
          </div>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-2 text-xs sm:grid-cols-4">
          <div className="rounded-md border border-border bg-panel/60 px-2 py-1.5">
            <div className="text-[10px] font-semibold uppercase text-muted">Base Rank</div>
            <div className="mt-0.5 font-semibold text-text">{reason.baseRank}</div>
          </div>
          <div className="rounded-md border border-border bg-panel/60 px-2 py-1.5">
            <div className="text-[10px] font-semibold uppercase text-muted">Adjusted</div>
            <div className="mt-0.5 font-semibold text-text">{reason.adjustedRank}</div>
          </div>
          <div className="rounded-md border border-border bg-panel/60 px-2 py-1.5">
            <div className="text-[10px] font-semibold uppercase text-muted">Signals</div>
            <div className="mt-0.5 font-semibold text-text">{reason.totalSignals}</div>
          </div>
          <div className="rounded-md border border-border bg-panel/60 px-2 py-1.5">
            <div className="text-[10px] font-semibold uppercase text-muted">Cap</div>
            <div className="mt-0.5 font-semibold text-text">{capLabel}</div>
          </div>
        </div>

        <ReferenceLinks references={references} />
      </div>
    </div>
  );
}
