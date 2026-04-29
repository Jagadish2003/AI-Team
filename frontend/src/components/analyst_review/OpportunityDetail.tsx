import React, { useEffect, useState } from 'react';
import { OpportunityCandidate, ReviewAuditEvent } from '../../types/analystReview';
import { fetchOppEnrichment, OppEnrichment } from '../../api/enrichmentApi';
import { useRunContext } from '../../context/RunContext';

function BulletList({
  items,
  emptyText,
}: {
  items: string[];
  emptyText?: string;
}) {
  if (!items || items.length === 0) {
    return emptyText
    ? <p className="text-xs text-muted italic">{emptyText}</p>
    : null;
  }
  return (
    <ul className="space-y-1.5">
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
    <div className="flex items-center justify-between gap-2 mb-2">
    <span className="text-xs font-semibold text-text">AI Analysis</span>
    {isLlm && (
      <span className="text-[10px] font-medium border border-border bg-bg/50 rounded px-1.5 py-0.5 text-muted tracking-wide">
      Claude
      </span>
    )}
    </div>
    {/* Added overflow-y-auto and max-h-[140px] to scroll after 6 lines */}
    <div className="rounded-lg border border-border bg-bg/30 p-3 text-xs text-text leading-relaxed overflow-y-auto max-h-[140px]">
    {summary}
    </div>
    </div>

    {/* Why bullets — only when LLM generated */}
    {isLlm && enrichment.aiWhyBullets.length > 0 && (
      <div>
      <div className="text-xs font-semibold text-text mb-2">Why This Matters</div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
      <BulletList items={enrichment.aiWhyBullets} />
      </div>
      </div>
    )}

    {/* Risks — only when LLM generated */}
    {isLlm && enrichment.aiRisks.length > 0 && (
      <div>
      <div className="text-xs font-semibold text-text mb-2">Risks if Not Addressed</div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
      <BulletList items={enrichment.aiRisks} />
      </div>
      </div>
    )}

    {/* Suggested next steps — only when LLM generated */}
    {isLlm && enrichment.aiSuggestedNextSteps.length > 0 && (
      <div>
      <div className="text-xs font-semibold text-text mb-2">Suggested Next Steps</div>
      <div className="rounded-lg border border-border bg-bg/30 p-3">
      <BulletList items={enrichment.aiSuggestedNextSteps} />
      </div>
      </div>
    )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function OpportunityDetail({
  opp,
  audit,
  onNavigate,
  suppressPermissions = false,
}: {
  opp: OpportunityCandidate | null;
  audit: ReviewAuditEvent[];
  onNavigate?: () => void;
  suppressPermissions?: boolean;
}) {
  const { runId } = useRunContext();
  const [enrichment, setEnrichment] = useState<OppEnrichment | null>(null);

  // Fetch enrichment when selected opportunity changes
  useEffect(() => {
    // FIX: Guard against network fetches during tests or when suppressed.
    // This prevents ECONNREFUSED errors from crashing Vitest act() blocks.
    const isTest = import.meta.env.MODE === 'test';
  if (!runId || !opp?.id || suppressPermissions || isTest) {
    setEnrichment(null);
    return;
  }

  let cancelled = false;
  fetchOppEnrichment(runId, opp.id)
  .then(data => { if (!cancelled) setEnrichment(data); })
  .catch((err) => {
    if (!cancelled) setEnrichment(null);
    console.warn('[T7] OpportunityDetail enrichment fetch failed:', err);
  });

  return () => { cancelled = true; };
  }, [runId, opp?.id, suppressPermissions]);

  if (!opp) {
    return (
      <div className="flex flex-col rounded-xl border border-border bg-panel h-full items-center justify-center">
      <div className="text-sm text-muted">Select an opportunity to review.</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col rounded-xl border border-border bg-panel overflow-hidden max-h-full w-full">

    {/* Title bar */}
    <div className="flex items-center justify-between px-5 py-4 border-b border-border bg-panel shrink-0">
    <h2 className="text-lg font-semibold text-text truncate pr-4">{opp.title}</h2>
    {onNavigate && (
      <button
      onClick={onNavigate}
      className="w-7 h-7 border border-border rounded-md flex items-center justify-center text-muted hover:bg-panel2 hover:text-text text-sm transition-colors shrink-0"
      >
      →
      </button>
    )}
    </div>

    <div className="flex-1 overflow-y-auto px-5 py-5 space-y-5">

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
      {opp.evidenceItems.map(ev => (
        <div key={ev.id} className="flex items-start gap-2">
        <svg className="w-3.5 h-3.5 text-muted shrink-0 mt-0.5" viewBox="0 0 24 24" fill="none" stroke="currentColor">
        <circle cx="12" cy="12" r="10" strokeWidth="2" />
        <path d="M12 16v-4M12 8h.01" strokeWidth="2" strokeLinecap="round" />
        </svg>
        <span className="text-xs text-text leading-relaxed">{ev.label}</span>
        </div>
      ))}
      </div>
      </div>
    )}
    {opp.evidenceIds && opp.evidenceIds.length > 0 && !opp.evidenceItems && (
      <div className="flex items-start gap-4 text-sm">
      <span className="text-muted w-24 shrink-0 pt-0.5">Evidence IDs</span>
      <div className="space-y-1 text-xs text-text font-mono">
      {opp.evidenceIds.map(id => <div key={id}>{id}</div>)}
      </div>
      </div>
    )}

    <div className="border-t border-border" />

    {/* T7: LLM enrichment panel */}
    <EnrichmentPanel opp={opp} enrichment={enrichment} />

    {/* Required Data Permissions */}
    {!suppressPermissions && opp.permissions && opp.permissions.length > 0 && (
      <div>
      <div className="text-xs font-semibold text-text mb-2">Required Data Permissions</div>
      <div className="rounded-lg border border-border overflow-hidden">
      {opp.permissions.map((p, i) => (
        <div key={i} className={`flex items-center px-3 py-2 gap-3 text-xs ${i !== 0 ? 'border-t border-border' : ''}`}>
        {p.satisfied ? (
          <span className="w-4 h-4 rounded-full bg-emerald-500/20 border border-emerald-500/50 flex items-center justify-center shrink-0">
          <svg className="w-2.5 h-2.5 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
          </svg>
          </span>
        ) : (
          <span className="w-4 h-4 rounded-full bg-amber-500/10 border border-amber-500/40 flex items-center justify-center shrink-0">
          <span className="text-amber-400 text-xs leading-none">!</span>
          </span>
        )}
        <span className="flex-1 text-text">{p.label}</span>
        {p.satisfied ? (
          <span className="text-emerald-400 text-xs">✓ Granted</span>
        ) : p.required ? (
          <span className="text-amber-400 text-xs">◇ Missing</span>
        ) : (
          <span className="text-xs border border-border rounded px-1.5 py-0.5 text-muted">Recommended</span>
        )}
        </div>
      ))}
      </div>
      </div>
    )}

    {/* Audit Trail - REDESIGNED */}
    <div>
    <div className="text-xs font-semibold text-text mb-2">Audit Trail</div>
    {/* Increased max-h to 210px to safely fit 3 items. 4+ items will trigger scroll. */}
    <div className="rounded-lg border border-border bg-bg/30 overflow-y-auto max-h-[210px]">
    {(() => {
      const filtered = audit
      .filter(a => !a.opportunityId || a.opportunityId === opp.id)
      .slice(0, 20);

      return filtered.length === 0 ? (
        <div className="px-4 py-4 text-xs text-muted text-center">No actions recorded yet.</div>
      ) : (
        filtered.map((e, i) => {
          // Attempt to format the ISO timestamp cleanly
          let formattedDate = e.tsLabel;
          try {
            const d = new Date(e.tsLabel);
            if (!isNaN(d.getTime())) {
              formattedDate = d.toLocaleString(undefined, {
                month: 'short',
                day: 'numeric',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
              });
            }
          } catch (err) {
            // Fallback to original string if parse fails
          }

          return (
            <div key={e.id} className={`flex flex-col gap-1.5 px-4 py-3 text-xs ${i !== 0 ? 'border-t border-border' : ''}`}>
            <div className="flex items-start justify-between gap-3">
            <span className="font-medium text-text leading-tight">{e.action}</span>
            <span className="text-muted shrink-0 bg-panel border border-border px-2 py-0.5 rounded text-[10px]">{e.by}</span>
            </div>
            <span className="text-muted text-[10px] font-mono">{formattedDate}</span>
            </div>
          );
        })
      );
    })()}
    </div>
    </div>

    </div>
    </div>
  );
}
