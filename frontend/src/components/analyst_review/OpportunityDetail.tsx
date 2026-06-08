import React, { useEffect, useRef, useState } from "react";
import {
  OpportunityCandidate,
  ReviewAuditEvent,
} from "../../types/analystReview";
import { ArrowRight, Hash } from "lucide-react";
import {
  fetchOppEnrichment,
  OppEnrichment,
  type EntitySummary,
} from "../../api/enrichmentApi";
import { useRunContext } from "../../context/RunContext";
import BaselineContextPanel from "./BaselineContextPanel";

function BulletList({
  items,
  emptyText,
}: {
  items: string[];
  emptyText?: string;
}) {
  if (!items || items.length === 0) {
    return emptyText ? (
      <p className="text-xs text-muted italic">{emptyText}</p>
    ) : null;
  }
  return (
    <ul className="opportunity-round-bullets space-y-1.5">
      {items.map((item, i) => (
        <li key={i} className="flex items-start gap-2 text-xs text-text">
          <span className="mt-0.5 shrink-0 text-muted font-bold">›</span>
          <span className="leading-relaxed">{item}</span>
        </li>
      ))}
    </ul>
  );
}

// ── LLM enrichment panel

function EnrichmentPanel({
  opp,
  enrichment,
}: {
  opp: OpportunityCandidate;
  enrichment: OppEnrichment | null;
}) {
  const isLlm = enrichment?.llmGenerated === true;

  // Use LLM summary if available, fall back to aiRationale
  const summary = enrichment?.aiSummary || opp.aiRationale;

  return (
    <div className="space-y-4">
      {/* AI Analysis */}
      <div>
        <div className="mb-2">
          <span className="text-xs font-semibold text-text">AI Analysis</span>
        </div>

        {/* Added overflow-y-auto and max-h-[140px] to scroll after 6 lines */}
        <div className="rounded-lg border border-border bg-bg/30 p-3 text-xs text-text leading-relaxed overflow-y-auto max-h-[140px]">
          {summary}
        </div>
      </div>

      {/* Why bullets — only when LLM generated */}
      {isLlm && enrichment.aiWhyBullets.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-text mb-2">
            Why This Matters
          </div>
          <div className="rounded-lg border border-border bg-bg/30 p-3">
            <BulletList items={enrichment.aiWhyBullets} />
          </div>
        </div>
      )}

      {/* Risks — only when LLM generated */}
      {isLlm && enrichment.aiRisks.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-text mb-2">
            Risks if Not Addressed
          </div>
          <div className="rounded-lg border border-border bg-bg/30 p-3">
            <BulletList items={enrichment.aiRisks} />
          </div>
        </div>
      )}

      {/* Suggested next steps — only when LLM generated */}
      {isLlm && enrichment.aiSuggestedNextSteps.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-text mb-2">
            Suggested Next Steps
          </div>
          <div className="rounded-lg border border-border bg-bg/30 p-3">
            <BulletList items={enrichment.aiSuggestedNextSteps} />
          </div>
        </div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

function labelize(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function confidenceLabel(value: number): string {
  if (!Number.isFinite(value)) return "";
  return `${Math.round(value * 100)}%`;
}

const SOURCE_LABELS: Record<string, string> = {
  agentiq: "AgentIQ",
  integration_hub: "Integration Hub",
  jira: "Jira",
  salesforce: "Salesforce",
  servicenow: "ServiceNow",
};

function sourceLabel(value: string): string {
  return SOURCE_LABELS[value.toLowerCase()] ?? labelize(value);
}

export function EntityTracePanel({
  entities,
}: {
  entities: EntitySummary[] | undefined;
}) {
  if (!entities || entities.length === 0) return null;

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Entities</span>
        <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
          {entities.length} linked
        </span>
      </div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {entities.map((entity) => {
            const isAmbiguous = entity.resolution_status === "ambiguous";
            return (
              <div
                key={entity.entity_id}
                data-testid={`entity-trace-${entity.entity_id}`}
                className={`min-w-0 rounded-md border px-3 py-2 ${
                  isAmbiguous
                    ? "border-border/60 bg-panel/40 text-muted opacity-75"
                    : "border-border/70 bg-panel/70 text-text"
                }`}
              >
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="truncate text-xs font-semibold leading-snug">
                      {entity.display_name}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] leading-tight text-muted">
                      <span>{labelize(entity.entity_type)}</span>
                      <span aria-hidden="true">/</span>
                      <span>{sourceLabel(entity.source_system)}</span>
                    </div>
                  </div>
                  <span className="shrink-0 rounded border border-border/70 px-1.5 py-0.5 text-[11px] leading-tight">
                    {confidenceLabel(entity.resolution_confidence)}
                  </span>
                </div>
                {isAmbiguous && (
                  <div className="mt-1.5 text-[11px] leading-tight text-muted">
                    Ambiguous
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function EvidenceIdsBox({ ids }: { ids: string[] }) {
  const hasOverflow = ids.length > 4;

  return (
    <div className="rounded-lg border border-border bg-bg/20 p-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs font-semibold text-text">
          Evidence IDs
        </div>
        <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
          {ids.length} linked
        </span>
      </div>

      <div className={hasOverflow ? "max-h-[74px] overflow-y-auto pr-1" : ""}>
        <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
          {ids.map((id) => (
            <div
              key={id}
              title={id}
              className="min-w-0 rounded-md border border-border/60 bg-panel/60 px-2.5 py-1.5 font-mono text-[11px] leading-tight text-text"
            >
              <span className="block truncate">{id}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default function OpportunityDetail({
  opp,
  audit,
  onNavigate,
  hideTitleBar = false,
  // T41-7: suppressPermissions is DEPRECATED — the Required Permissions section
  // was removed in T41-7. This prop is retained only for backward compatibility
  // with existing call sites (OpportunityReviewPage.tsx line 213). It has no
  // effect on rendering. Do NOT add it to new call sites.
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  suppressPermissions = false,
  footer,
}: {
  opp: OpportunityCandidate | null;
  audit: ReviewAuditEvent[];
  onNavigate?: () => void;
  hideTitleBar?: boolean;
  /** @deprecated T41-7: has no effect. Permissions section removed from this component. */
  suppressPermissions?: boolean;
  footer?: React.ReactNode;
}) {
  const { runId } = useRunContext();
  const [enrichment, setEnrichment] = useState<OppEnrichment | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [opp?.id]);

  // Fetch enrichment when selected opportunity changes.
  // T41-7: suppressPermissions removed from deps — it is deprecated and
  // has no effect on rendering. Including it was causing spurious refetches
  // when callers toggled it, which was misleading and wasteful.
  useEffect(() => {
    // FIX: Guard against network fetches during tests.
    // This prevents ECONNREFUSED errors from crashing Vitest act() blocks.
    const isTest = import.meta.env.MODE === "test";
    if (!runId || !opp?.id || isTest) {
      setEnrichment(null);
      return;
    }

    let cancelled = false;
    fetchOppEnrichment(runId, opp.id)
      .then((data) => {
        if (!cancelled) setEnrichment(data);
      })
      .catch((err) => {
        if (!cancelled) setEnrichment(null);
        console.warn("[T7] OpportunityDetail enrichment fetch failed:", err);
      });

    return () => {
      cancelled = true;
    };
  }, [runId, opp?.id]);

  if (!opp) {
    return (
      <div className="flex flex-col rounded-xl border border-border bg-panel h-full items-center justify-center">
        <div className="text-sm text-muted">
          Select an opportunity to review.
        </div>
      </div>
    );
  }

  return (
    <div
      className={`flex h-full min-h-0 w-full flex-col overflow-hidden bg-panel ${
        hideTitleBar ? "" : "rounded-xl border border-border"
      }`}
    >
      {!hideTitleBar && (
        <div className="flex items-center justify-between px-5 py-4 border-b border-border bg-panel shrink-0">
          <h2 className="min-w-0 pr-4 text-lg font-semibold leading-snug text-text">
            {opp.title}
          </h2>
          {onNavigate && (
            <button
              onClick={onNavigate}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border text-muted transition-colors hover:bg-panel2 hover:text-text"
              aria-label="Open opportunity report"
            >
              <ArrowRight size={14} />
            </button>
          )}
        </div>
      )}

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-5 py-5 space-y-5">
        {/* Identifier */}
        {opp.identifier && (
          <div className="flex items-center gap-4 text-sm">
            <span className="text-muted w-24 shrink-0">Identifier</span>
            <span className="text-text font-medium font-mono text-xs bg-panel2 border border-border px-2 py-0.5 rounded">
              {opp.identifier}
            </span>
          </div>
        )}

        {/* Evidence */}
        {opp.evidenceItems && opp.evidenceItems.length > 0 && (
          <div className="flex items-start gap-4 text-sm">
            <span className="text-muted w-24 shrink-0 pt-0.5">Evidence</span>
            <div className="space-y-1.5">
              {opp.evidenceItems.map((ev) => (
                <div key={ev.id} className="flex items-start gap-2">
                  <svg
                    className="w-3.5 h-3.5 text-muted shrink-0 mt-0.5"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                  >
                    <circle cx="12" cy="12" r="10" strokeWidth="2" />
                    <path
                      d="M12 16v-4M12 8h.01"
                      strokeWidth="2"
                      strokeLinecap="round"
                    />
                  </svg>
                  <span className="text-xs text-text leading-relaxed">
                    {ev.label}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        {opp.evidenceIds &&
          opp.evidenceIds.length > 0 &&
          !opp.evidenceItems && <EvidenceIdsBox ids={opp.evidenceIds} />}

        <div className="border-t border-border" />

        {/* T7: LLM enrichment panel */}
        <EnrichmentPanel opp={opp} enrichment={enrichment} />

        {/* T10: Temporal baseline context panel */}
        <BaselineContextPanel enrichment={enrichment} />

        {/* T3-S12-A: Entity trace shown after temporal baseline context. */}
        <EntityTracePanel entities={enrichment?.entities} />

        {/* T41-7: Required Permissions section removed from Opportunity Review.
            Permissions are now shown on the Agent Blueprint screen in
            forward-looking framing: "To implement this agent, the agent user
            profile will need:". The suppressPermissions prop is retained for
            backward-compat but the section no longer renders anywhere. */}

        {/* Audit Trail - REDESIGNED */}
        <div>
          <div className="text-xs font-semibold text-text mb-2">
            Audit Trail
          </div>
          {/* Increased max-h to 210px to safely fit 3 items. 4+ items will trigger scroll. */}
          <div className="rounded-lg border border-border bg-bg/30 overflow-y-auto max-h-[210px]">
            {(() => {
              const filtered = audit
                .filter((a) => !a.opportunityId || a.opportunityId === opp.id)
                .slice(0, 20);

              return filtered.length === 0 ? (
                <div className="px-4 py-4 text-xs text-muted text-center">
                  No actions recorded yet.
                </div>
              ) : (
                filtered.map((e, i) => {
                  // Attempt to format the ISO timestamp cleanly
                  let formattedDate = e.tsLabel;
                  try {
                    const d = new Date(e.tsLabel);
                    if (!isNaN(d.getTime())) {
                      formattedDate = d.toLocaleString(undefined, {
                        month: "short",
                        day: "numeric",
                        year: "numeric",
                        hour: "2-digit",
                        minute: "2-digit",
                        second: "2-digit",
                      });
                    }
                  } catch (err) {
                    // Fallback to original string if parse fails
                  }

                  return (
                    <div
                      key={e.id}
                      className={`flex flex-col gap-1.5 px-4 py-3 text-xs ${i !== 0 ? "border-t border-border" : ""}`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <span className="font-medium text-text leading-tight">
                          {e.action}
                        </span>
                        <span className="text-xs border border-bg rounded px-1.5 py-0.5 text-text shrink-0">
                          {e.by}
                        </span>
                      </div>
                      <span className="text-xs text-muted leading-relaxed">
                        {formattedDate}
                      </span>
                    </div>
                  );
                })
              );
            })()}
          </div>
        </div>
      </div>
      {footer && (
        <div className="shrink-0 border-t border-border px-5 py-4">
          {footer}
        </div>
      )}
    </div>
  );
}
