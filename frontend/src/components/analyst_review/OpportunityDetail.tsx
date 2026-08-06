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
  type RelationshipSummary,
  type CausalHypothesisSummary,
} from "../../api/enrichmentApi";
import { useRunContext } from "../../context/RunContext";
import BaselineContextPanel from "./BaselineContextPanel";
import ProjectionAssumptionLedger from "../projection/ProjectionAssumptionLedger";
import ProjectionBandPanel from "../projection/ProjectionBand";
import ProjectionBasisPanel from "../projection/ProjectionBasis";
import ProjectionRecommendationPanel from "../projection/ProjectionRecommendation";
import { showRelease2ArcAUi } from "../../config/releaseFlags";
import { RankingAdjustmentPanel } from "../learning/RankingAdjustment";

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

// ENT-3 / T3-S15-A: each aiWhyBullet may carry a leading [OBSERVED] or
// [INFERRED: <basis>] tag. parseObservationTag splits the tag from the body so
// the UI can render a pill and show the clean text. Untagged bullets render
// with no pill.
type ObservationKind = "observed" | "inferred" | null;

export function parseObservationTag(bullet: string): {
  kind: ObservationKind;
  basis: string | null;
  text: string;
} {
  const match = /^\s*\[(OBSERVED|INFERRED)(?::\s*([^\]]*))?\]\s*/i.exec(bullet);
  if (!match) return { kind: null, basis: null, text: bullet };
  const kind = match[1].toLowerCase() === "observed" ? "observed" : "inferred";
  const basis = match[2]?.trim() || null;
  return { kind, basis, text: bullet.slice(match[0].length) };
}

function ObservationPill({ kind, basis }: { kind: ObservationKind; basis: string | null }) {
  if (!kind) return null;
  const isObserved = kind === "observed";
  return (
    <span
      data-testid={`observation-pill-${kind}`}
      title={basis ? `Inferred from: ${basis}` : undefined}
      className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase leading-tight tracking-wide ${
        isObserved
          ? "border border-green-500/40 bg-green-500/10 text-green-600"
          : "border border-amber-500/40 bg-amber-500/10 text-amber-600"
      }`}
    >
      {isObserved ? "Observed" : "Inferred"}
    </span>
  );
}

// Why-bullets renderer with OBSERVED/INFERRED pills (ENT-3 / T3-S15-A).
function WhyBulletList({ items }: { items: string[] }) {
  if (!items || items.length === 0) return null;
  return (
    <ul className="opportunity-round-bullets space-y-1.5">
      {items.map((item, i) => {
        const { kind, basis, text } = parseObservationTag(item);
        return (
          <li key={i} className="flex items-start gap-2 text-xs text-text">
            <span className="mt-0.5 shrink-0 text-muted font-bold">›</span>
            <ObservationPill kind={kind} basis={basis} />
            <span className="leading-relaxed">{text}</span>
          </li>
        );
      })}
    </ul>
  );
}

export function EnrichmentPanel({
  opp,
  enrichment,
}: {
  opp: OpportunityCandidate;
  enrichment: OppEnrichment | null;
}) {
  const isLlm = enrichment?.llmGenerated === true;

  // Use LLM summary if available, fall back to aiRationale
  const summary = enrichment?.aiSummary || opp.aiRationale;

  // ENT-3: a preliminary finding has not cleared all quality gates and needs
  // analyst review before it can be treated as confirmed.
  const isPreliminary = enrichment?.preliminary === true;
  const corroboration = enrichment?.corroboration_label;

  return (
    <div className="space-y-4">
      {/* ENT-3: Analyst-review-required banner for preliminary findings */}
      {isPreliminary && (
        <div
          data-testid="preliminary-banner"
          role="status"
          className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-700"
        >
          <span className="font-semibold">Analyst review required</span>
          {enrichment?.preliminary_reason && (
            <span className="text-amber-700/90"> — {enrichment.preliminary_reason}</span>
          )}
        </div>
      )}

      {/* AI Analysis */}
      <div>
        <div className="mb-2">
          <span className="text-xs font-semibold text-text">AI Analysis</span>
        </div>

        {/* Added overflow-y-auto and max-h-[140px] to scroll after 6 lines */}
        <div className="rounded-lg border border-border bg-bg/30 p-3 text-xs text-text leading-relaxed overflow-y-auto max-h-[140px]">
          {summary}
        </div>

        {/* ENT-3: corroboration label (from ENT-2) below the analysis block */}
        {corroboration && (
          <div
            data-testid="corroboration-label"
            className="mt-2 inline-flex items-center rounded border border-border/70 bg-panel/60 px-2 py-0.5 text-[11px] leading-tight text-muted"
          >
            {corroboration}
          </div>
        )}
      </div>

      {/* Why bullets — only when LLM generated */}
      {isLlm && enrichment.aiWhyBullets.length > 0 && (
        <div>
          <div className="text-xs font-semibold text-text mb-2">
            Why This Matters
          </div>
          <div className="rounded-lg border border-border bg-bg/30 p-3">
            <WhyBulletList items={enrichment.aiWhyBullets} />
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

// P2 (R18-C0): Opportunity Review must read as names, never raw record IDs like
// '005WG00000ZkgMfYAJ owns 00003515'. The backend resolves a human display_name
// whenever the entity-resolution layer holds one, so a readable name is used as
// soon as it exists. This guards only the remaining case — an entity whose sole
// known label is an opaque source ID — degrading to the entity's type label
// instead of leaking the identifier. Mirrors the identifier heuristic in
// backend app/graph_query.py so both layers agree on what counts as an ID.
function looksLikeRawId(value: string): boolean {
  const text = (value ?? "").trim();
  if (!text || /\s/.test(text)) return false; // any whitespace => a readable phrase
  if (/^\d{4,}$/.test(text)) return true; // long numeric id (e.g. 00003515)
  // compact, space-free alphanumeric token with a digit (e.g. Salesforce IDs)
  return /^[A-Za-z0-9][A-Za-z0-9_-]{5,}$/.test(text) && /\d/.test(text);
}

// Prefer the resolved, human-readable entity name; when only a raw ID is
// available, fall back to the entity's type label ('Account', 'Case', 'Person')
// so the user never sees a bare record identifier.
function readableEntityName(name: string, entityType: string): string {
  const trimmed = (name ?? "").trim();
  if (trimmed && !looksLikeRawId(trimmed)) return trimmed;
  const typeLabel = labelize(entityType ?? "").trim();
  return typeLabel || "Unknown";
}

function isValidRunThreshold(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function runLabel(count: number): string {
  return `${count} run${count === 1 ? "" : "s"}`;
}

function shouldShowEntity(
  entity: EntitySummary,
  entityMinRunCount: number | null | undefined
): boolean {
  if (!isValidRunThreshold(entityMinRunCount)) return true;
  return (
    typeof entity.run_count !== "number" ||
    entity.run_count >= entityMinRunCount
  );
}

function isEarlyEntity(
  entity: EntitySummary,
  entityMinRunCount: number | null | undefined
): boolean {
  if (!isValidRunThreshold(entityMinRunCount)) return false;
  return (
    typeof entity.run_count === "number" &&
    entity.run_count < entityMinRunCount
  );
}

export function EntityTracePanel({
  entities,
  runCount,
  entityMinRunCount,
  enrichmentLoaded = false,
}: {
  entities: EntitySummary[] | null | undefined;
  runCount?: number | null;
  entityMinRunCount?: number | null;
  enrichmentLoaded?: boolean;
}) {
  if (!enrichmentLoaded && !entities) return null;

  const entityList = entities ?? [];
  const hasRunThreshold = isValidRunThreshold(entityMinRunCount);
  const uniqueEntities = Array.from(
    new Map(
      entityList
        .filter((entity) => shouldShowEntity(entity, entityMinRunCount))
        .map((entity) => [entity.entity_id, entity])
    ).values()
  );
  const isWaitingForRunHistory =
    hasRunThreshold &&
    uniqueEntities.length === 0 &&
    (
      (typeof runCount === "number" && runCount < entityMinRunCount) ||
      entityList.some((entity) => isEarlyEntity(entity, entityMinRunCount))
    );
  const thresholdLabel = hasRunThreshold ? runLabel(entityMinRunCount) : null;
  const emptyTitle = isWaitingForRunHistory && thresholdLabel
    ? `Entities will appear after ${entityMinRunCount} or more discovery runs.`
    : "No entities linked to this opportunity.";
  const emptyDescription = isWaitingForRunHistory
    ? "AgentIQ is already retaining early entity signals for graph completeness. They stay hidden here until enough run history is available, so this section shows stable, repeatable entities."
    : "AgentIQ will show linked people, systems, and process entities here when eligible entity evidence is available.";

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Entities</span>
        <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
          {uniqueEntities.length > 0
            ? `${uniqueEntities.length} linked`
            : isWaitingForRunHistory
              ? `Hidden until ${thresholdLabel}`
              : "0 linked"}
        </span>
      </div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
        {uniqueEntities.length === 0 ? (
          <div data-testid="entity-trace-empty" className="px-1 py-1">
            <div className="text-sm font-bold leading-snug text-text">
              {emptyTitle}
            </div>
            <p className="mt-2 text-xs leading-relaxed text-muted">
              {emptyDescription}
            </p>
          </div>
        ) : (
          <div
            data-testid="entity-trace-scroll"
            className="max-h-[13.5rem] overflow-y-auto pr-1 [scrollbar-gutter:stable]"
          >
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {uniqueEntities.map((entity) => {
                const isAmbiguous = entity.resolution_status === "ambiguous";
                return (
                  <div
                    key={entity.entity_id}
                    data-testid={`entity-trace-${entity.entity_id}`}
                    className={`min-h-16 min-w-0 rounded-md border px-3 py-2 ${
                      isAmbiguous
                        ? "border-border/60 bg-panel/40 text-muted opacity-75"
                        : "border-border/70 bg-panel/70 text-text"
                    }`}
                  >
                    <div className="flex min-w-0 items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-xs font-semibold leading-snug">
                          {readableEntityName(entity.display_name, entity.entity_type)}
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
        )}
      </div>
    </div>
  );
}

// T3-S13-A relationship trace panel

function relationshipText(rel: RelationshipSummary): string {
  const from = readableEntityName(rel.from_entity_name, rel.from_entity_type);
  const to = readableEntityName(rel.to_entity_name, rel.to_entity_type);
  switch (rel.relationship_type) {
    case "owns":
      return `${from} owns ${to}`;
    case "member_of":
      return `${from} is a member of ${to}`;
    case "escalates_to":
      return `${from} escalates to ${to}`;
    case "depends_on":
      return `${from} depends on ${to}`;
    case "routes_to":
      return `${from} routes to ${to}`;
    default:
      return `${from} ${labelize(rel.relationship_type).toLowerCase()} ${to}`;
  }
}

export function RelationshipTracePanel({
  relationships,
}: {
  relationships: RelationshipSummary[] | undefined;
}) {
  const relationshipKey = (rel: RelationshipSummary) =>
    [
      rel.from_entity_type,
      rel.from_entity_name.trim().toLowerCase(),
      rel.relationship_type,
      rel.to_entity_type,
      rel.to_entity_name.trim().toLowerCase(),
    ].join("|");
  const uniqueRelationships = Array.from(
    new Map(
      (relationships ?? []).map((relationship) => [
        relationshipKey(relationship),
        relationship,
      ])
    ).values()
  );
  if (uniqueRelationships.length === 0) return null;

  return (
    <div data-testid="relationship-trace-panel">
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Relationships</span>
        <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
          {uniqueRelationships.length} linked
        </span>
      </div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
        <div
          data-testid="relationship-trace-scroll"
          className="max-h-[13.5rem] overflow-y-auto pr-1 [scrollbar-gutter:stable]"
        >
          <div
            data-testid="relationship-trace-grid"
            className="grid grid-cols-1 gap-2 sm:grid-cols-2"
          >
            {uniqueRelationships.map((rel, index) => {
              const confidence = confidenceLabel(rel.confidence);
              return (
                <div
                  key={relationshipKey(rel)}
                  data-testid={`relationship-trace-${index}`}
                  className="min-w-0 rounded-md border border-border/70 bg-panel/70 px-3 py-2 text-text"
                >
                  <div className="flex min-w-0 items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="text-xs font-semibold leading-relaxed">
                        {relationshipText(rel)}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] leading-tight text-muted">
                        <span>{labelize(rel.from_entity_type)}</span>
                        <span aria-hidden="true">-&gt;</span>
                        <span>{labelize(rel.to_entity_type)}</span>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5">
                      {rel.inferred && (
                        <span
                          data-testid={`relationship-inferred-${index}`}
                          className="rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase leading-tight text-amber-600"
                        >
                          inferred
                        </span>
                      )}
                      {confidence && (
                        <span className="rounded border border-border/70 px-1.5 py-0.5 text-[11px] leading-tight">
                          {confidence}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

// ENT-6 / T3-S16-A causal hypothesis panel

// Maps the backend preliminary_reason string produced by T5 quality gates to
// the Section 5 banner copy. The reason string format is:
//   gate1_insufficient_run_count: {X} of {N} runs completed
//   gate2_unresolved_entities: {N} entities require resolution
//   gate3_inferred_primary_step: step {N}
function parsePreliminaryBanner(reason: string | null): string {
  if (!reason) return "analyst review required.";

  const gate1 = /^gate1_insufficient_run_count:\s*(\d+)\s+of\s+(\d+)/.exec(reason);
  if (gate1) {
    return `analyst review required. Baseline context is still accumulating (${gate1[1]} of ${gate1[2]} runs completed).`;
  }

  const gate2 = /^gate2_unresolved_entities:\s*(\d+)/.exec(reason);
  if (gate2) {
    return `${gate2[1]} entities require resolution before this finding is confirmed.`;
  }

  if (reason.startsWith("gate3_inferred_primary_step")) {
    return "this causal chain includes inferred relationships that have not yet been validated.";
  }

  return "analyst review required.";
}

// Splits a cause-chain step into its [inferred:…] prefix label (if any) and
// clean body text. The backend T4 parser tags inferred steps with the prefix
// "[inferred: confidence=X]" (matches the causal prompt spec in Section 2b).
function parseCausalStep(step: string): { inferredLabel: string | null; text: string } {
  const match = /^\s*\[inferred(?::[^\]]*)?\]\s*/i.exec(step);
  if (!match) return { inferredLabel: null, text: step };
  return { inferredLabel: "[inferred]", text: step.slice(match[0].length) };
}

export function CausalHypothesisPanel({
  causal_hypothesis,
}: {
  causal_hypothesis: CausalHypothesisSummary | null | undefined;
}) {
  if (!causal_hypothesis) return null;

  const {
    cause_chain,
    falsifiability_condition,
    confidence,
    preliminary,
    preliminary_reason,
  } = causal_hypothesis;

  const bannerText = preliminary ? parsePreliminaryBanner(preliminary_reason) : null;
  const confidencePct = Number.isFinite(confidence)
    ? `${Math.round(confidence * 100)}%`
    : null;

  return (
    <div data-testid="causal-hypothesis-panel">
      {/* Header — matches Relationships / Entities section headers exactly */}
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Causal Hypothesis</span>
        {confidencePct && (
          <span
            data-testid="causal-confidence-badge"
            className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text"
          >
            {confidencePct}
          </span>
        )}
      </div>

      <div className="rounded-lg border border-border bg-bg/30 p-3 space-y-2">
        {/* Amber preliminary banner — matches ENT-3 analyst-review banner style */}
        {preliminary && bannerText && (
          <div
            data-testid="causal-preliminary-banner"
            role="status"
            className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-700"
          >
            <span className="font-semibold">Preliminary</span>
            {" — "}{bannerText}
          </div>
        )}

        {/* Numbered cause chain — each step is a card matching relationship rows */}
        {cause_chain && cause_chain.length > 0 && (
          <div className="space-y-1.5">
            {cause_chain.map((step, i) => {
              const { inferredLabel, text } = parseCausalStep(step);
              return (
                <div
                  key={i}
                  data-testid={`causal-step-${i}`}
                  className="min-w-0 rounded-md border border-border/70 bg-panel/70 px-3 py-2 text-text"
                >
                  <div className="flex min-w-0 items-start justify-between gap-3">
                    <div className={`min-w-0 flex items-start gap-2${preliminary ? " opacity-75" : ""}`}>
                      <span className="shrink-0 text-xs font-semibold text-muted w-4 text-right leading-relaxed">
                        {i + 1}.
                      </span>
                      <span className="text-xs font-semibold leading-relaxed">{text}</span>
                    </div>
                    {inferredLabel && (
                      <span
                        data-testid={`causal-inferred-label-${i}`}
                        className="shrink-0 rounded border border-amber-500/40 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase leading-tight text-amber-600"
                      >
                        inferred
                      </span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Falsifiability — italic text-muted, matches AI Analysis footnote style */}
        {falsifiability_condition && (
          <p
            data-testid="causal-falsifiability"
            className="text-xs italic text-muted leading-relaxed px-1"
          >
            {!preliminary && (
              <span className="not-italic font-semibold text-muted">
                How to disprove this:{" "}
              </span>
            )}
            {falsifiability_condition}
          </p>
        )}
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

  const projection = enrichment?.projection ?? opp.projection ?? null;

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

        {/* 2.0-A1 T5: the intervention-language recommendation sits directly
            under the AI analysis, so the honest statement of what the agent
            handles is read alongside the narrative rather than after the band. */}
        {showRelease2ArcAUi && (
          <>
            <ProjectionRecommendationPanel projection={projection} />

        {/* 2.0-A1 T4: the resulting band and its evidence label, above the
            basis — the band is the answer, the basis is the working. */}
            <ProjectionBandPanel projection={projection} />

        {/* 2.0-A1 T3: every visible projection shows its computation basis. */}
            <ProjectionBasisPanel projection={projection} />

        {/* 2.0-A1 T2: projection assumptions are rendered with the opportunity. */}
            <ProjectionAssumptionLedger projection={projection} />
          </>
        )}

        <RankingAdjustmentPanel ranking={opp._ranking} />

        {/* T10: Temporal baseline context panel */}
        <BaselineContextPanel enrichment={enrichment} />

        {/* T3-S12-A: Entity trace shown after temporal baseline context. */}
        <EntityTracePanel
          entities={enrichment?.entities}
          runCount={enrichment?.run_count}
          entityMinRunCount={enrichment?.entity_min_run_count}
          enrichmentLoaded={Boolean(enrichment)}
        />

        {/* T3-S13-A: Relationship trace shown after entity trace. */}
        <RelationshipTracePanel relationships={enrichment?.relationships} />

        {/* ENT-6/T9: Causal hypothesis evidence trace — after entity trace. */}
        <CausalHypothesisPanel causal_hypothesis={enrichment?.causal_hypothesis} />

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
