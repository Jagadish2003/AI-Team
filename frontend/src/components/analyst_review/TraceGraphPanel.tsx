// 2.0-B1 T3 — Interrogation UI (drill-down).
//
// Expands a finding down to its full provenance chain: finding -> evidence ->
// source record, each hop showing its origin (observed/inferred), connector,
// and timestamp; where MSP-B7 correlated a claim by a time-windowed join, the
// join type and window used are shown alongside the hop it corroborates; and
// the retrieval candidates context assembly considered (used and unused) are
// listed underneath — "retrieval proposes, assembly decides", both sides
// visible. A leaf hop opens a deep link into its originating system when the
// underlying provenance data supplies one (most connectors don't yet — no
// link is fabricated when absent).
//
// This ticket carries no acceptance criteria of its own (it supports 2.0-B1's
// AC1/AC3 usability) — it is a thin, self-fetching panel over the trace-graph
// endpoint built in T1/T2, styled to match the existing EntityTracePanel /
// RelationshipTracePanel sections in OpportunityDetail.tsx.
import React, { useMemo, useState } from 'react';
import { ChevronDown, ExternalLink } from 'lucide-react';
import { fetchTraceGraph } from '../../api/traceGraphApi';
import { cacheKeys } from '../../lib/cacheKeys';
import { useResource } from '../../lib/dataCache';
import { hopDeepLink } from '../../types/traceGraph';
import type { JoinTrace, RetrievalCandidate, TraceHop } from '../../types/traceGraph';

function OriginPill({ origin }: { origin: 'observed' | 'inferred' }) {
  const isObserved = origin === 'observed';
  return (
    <span
      data-testid={`trace-origin-${origin}`}
      className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase leading-tight tracking-wide ${
        isObserved
          ? 'border border-green-500/40 bg-green-500/10 text-green-600'
          : 'border border-amber-500/40 bg-amber-500/10 text-amber-600'
      }`}
    >
      {isObserved ? 'Observed' : 'Inferred'}
    </span>
  );
}

function formatHopType(type: string): string {
  if (type === 'finding') return 'Finding';
  if (type === 'evidence') return 'Evidence';
  if (type === 'source_record') return 'Source record';
  return type;
}

function formatJoin(join: JoinTrace): string {
  const window = join.window_seconds != null ? `${join.window_seconds}s window` : 'configured window';
  const delta = join.delta_seconds != null ? ` (${Math.round(join.delta_seconds)}s apart)` : '';
  return `within ${window}${delta}`;
}

function HopNode({
  hop,
  childrenByParent,
  joinsByHop,
  depth,
}: {
  hop: TraceHop;
  childrenByParent: Map<string, TraceHop[]>;
  joinsByHop: Map<string, JoinTrace[]>;
  depth: number;
}) {
  const children = childrenByParent.get(hop.hop_id) ?? [];
  const joins = joinsByHop.get(hop.hop_id) ?? [];
  const [expanded, setExpanded] = useState(true);
  const deepLink = hopDeepLink(hop);

  return (
    <div style={depth > 0 ? { marginLeft: 16 } : undefined} data-testid={`trace-hop-${hop.hop_id}`}>
      <div className="min-w-0 rounded-md border border-border/70 bg-panel/70 px-3 py-2">
        <div className="flex min-w-0 items-start justify-between gap-3">
          <div className="flex min-w-0 items-start gap-2">
            {children.length > 0 ? (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                aria-label={expanded ? `Collapse ${hop.label}` : `Expand ${hop.label}`}
                aria-expanded={expanded}
                data-testid={`trace-hop-toggle-${hop.hop_id}`}
                className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center text-muted"
              >
                <ChevronDown size={12} className={expanded ? '' : '-rotate-90'} />
              </button>
            ) : (
              <span className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
            )}
            <div className="min-w-0">
              <div className="truncate text-xs font-semibold leading-snug text-text">
                {hop.label}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] leading-tight text-muted">
                <span>{formatHopType(hop.hop_type)}</span>
                {hop.connector && (
                  <>
                    <span aria-hidden="true">/</span>
                    <span>{hop.connector}</span>
                  </>
                )}
                {hop.timestamp && (
                  <>
                    <span aria-hidden="true">/</span>
                    <span>{hop.timestamp}</span>
                  </>
                )}
              </div>
            </div>
          </div>
          <OriginPill origin={hop.origin} />
        </div>

        {joins.length > 0 && (
          <div className="mt-2 space-y-1 border-t border-border/50 pt-2">
            {joins.map((join, i) => (
              <div
                key={i}
                data-testid={`trace-join-${hop.hop_id}-${i}`}
                className="text-[11px] leading-tight text-muted"
              >
                <span className="font-semibold text-text">{join.join_type}</span>
                {' — '}
                {formatJoin(join)}
              </div>
            ))}
          </div>
        )}

        {deepLink && (
          <a
            href={deepLink}
            target="_blank"
            rel="noreferrer"
            data-testid={`trace-hop-link-${hop.hop_id}`}
            className="mt-2 inline-flex items-center gap-1.5 text-[11px] font-medium text-accent hover:underline"
          >
            <ExternalLink size={11} />
            View in {hop.connector ?? 'source system'}
          </a>
        )}
      </div>

      {expanded && children.length > 0 && (
        <div className="mt-2 space-y-2">
          {children.map((child) => (
            <HopNode
              key={child.hop_id}
              hop={child}
              childrenByParent={childrenByParent}
              joinsByHop={joinsByHop}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function RetrievalCandidatesSection({
  candidates,
  usedCount,
  unusedCount,
}: {
  candidates: RetrievalCandidate[];
  usedCount: number;
  unusedCount: number;
}) {
  const [expanded, setExpanded] = useState(false);
  if (candidates.length === 0) return null;

  return (
    <div className="mt-3 rounded-md border border-border/70 bg-panel/50 p-2">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        data-testid="trace-retrieval-toggle"
        className="flex w-full items-center justify-between gap-2 text-[11px] font-semibold text-text"
      >
        <span>
          Retrieval candidates ({usedCount} used, {unusedCount} not used)
        </span>
        <ChevronDown size={12} className={expanded ? '' : '-rotate-90'} />
      </button>
      {expanded && (
        <div className="mt-2 space-y-1.5" data-testid="trace-retrieval-list">
          {candidates.map((c) => (
            <div
              key={c.chunk_id}
              data-testid={`trace-retrieval-${c.chunk_id}`}
              className={`rounded border px-2 py-1.5 text-[11px] leading-tight ${
                c.used
                  ? 'border-green-500/30 bg-green-500/5 text-text'
                  : 'border-border/50 bg-bg/30 text-muted'
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">
                  {c.used ? 'Used' : 'Not used'}
                  {c.source_system ? ` — ${c.source_system}` : ''}
                </span>
                {c.reason && <span className="text-[10px] text-muted">{c.reason}</span>}
              </div>
              {c.content_snippet && (
                <div className="mt-1 line-clamp-2 text-muted">{c.content_snippet}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function TraceGraphPanel({
  runId,
  oppId,
}: {
  runId: string | null | undefined;
  oppId: string | null | undefined;
}) {
  const key = runId && oppId ? cacheKeys.runTraceGraph(runId, oppId) : null;
  const { data, loading, error } = useResource(key, () =>
    fetchTraceGraph(runId as string, oppId as string)
  );

  const { rootHops, childrenByParent, joinsByHop } = useMemo(() => {
    const hops = data?.hops ?? [];
    const joins = data?.joins ?? [];
    const byParent = new Map<string, TraceHop[]>();
    const roots: TraceHop[] = [];
    for (const hop of hops) {
      if (hop.from_hop_id) {
        const list = byParent.get(hop.from_hop_id) ?? [];
        list.push(hop);
        byParent.set(hop.from_hop_id, list);
      } else {
        roots.push(hop);
      }
    }
    const byHop = new Map<string, JoinTrace[]>();
    for (const join of joins) {
      if (!join.hop_id) continue;
      const list = byHop.get(join.hop_id) ?? [];
      list.push(join);
      byHop.set(join.hop_id, list);
    }
    return { rootHops: roots, childrenByParent: byParent, joinsByHop: byHop };
  }, [data]);

  if (!runId || !oppId) return null;

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-text">Source Trace</span>
        {data && data.available && (
          <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
            {data.hops.length} hop{data.hops.length === 1 ? '' : 's'}
          </span>
        )}
      </div>
      <div className="rounded-lg border border-border bg-bg/30 p-3" data-testid="trace-graph-panel">
        {loading && (
          <div className="px-1 py-1 text-xs text-muted" data-testid="trace-graph-loading">
            Loading source trace…
          </div>
        )}
        {!loading && error && (
          <div className="px-1 py-1 text-xs text-muted" data-testid="trace-graph-error">
            Unable to load the source trace right now.
          </div>
        )}
        {!loading && !error && data && !data.available && (
          <div data-testid="trace-graph-empty" className="px-1 py-1">
            <div className="text-sm font-bold leading-snug text-text">
              No source trace available yet.
            </div>
            <p className="mt-2 text-xs leading-relaxed text-muted">
              AgentIQ will show the full finding-to-source-record chain here once this
              opportunity's evidence has been materialized.
            </p>
          </div>
        )}
        {!loading && !error && data && data.available && (
          <>
            <div
              className="max-h-[20rem] overflow-y-auto pr-1 [scrollbar-gutter:stable]"
              data-testid="trace-hop-scroll"
            >
              <div className="space-y-2">
                {rootHops.map((hop) => (
                  <HopNode
                    key={hop.hop_id}
                    hop={hop}
                    childrenByParent={childrenByParent}
                    joinsByHop={joinsByHop}
                    depth={0}
                  />
                ))}
              </div>
            </div>
            {data.truncated && (
              <div className="mt-2 text-[11px] text-muted">
                This chain is large — showing the first {data.hops.length} hops.
              </div>
            )}
            <RetrievalCandidatesSection
              candidates={data.retrieval_candidates}
              usedCount={data.retrieval_candidates_used_count}
              unusedCount={data.retrieval_candidates_unused_count}
            />
          </>
        )}
      </div>
    </div>
  );
}
