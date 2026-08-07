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
import { ChevronDown, Download, ExternalLink, FileText } from 'lucide-react';
import { fetchTraceGraph } from '../../api/traceGraphApi';
import {
  downloadEvidenceReportForFinding,
  downloadFindingEvidenceBundle,
} from '../../api/evidenceExportApi';
import { ApiError } from '../../lib/apiClient';
import { useAuthOptional } from '../../context/AuthContext';
import { isViewerRole } from '../../utils/roles';
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
                {/* AC1 names four things every hop must carry — origin, connector,
                    run id, timestamp. The first three were rendered and the run id
                    was not, even though the API has always returned it. It matters
                    once a chain spans runs (an entity first seen in an earlier run):
                    without it a reviewer cannot tell which run observed which hop. */}
                {hop.run_id && (
                  <>
                    <span aria-hidden="true">/</span>
                    <span
                      data-testid={`trace-hop-run-${hop.hop_id}`}
                      title={`Observed by run ${hop.run_id}`}
                      className="font-mono text-[10px]"
                    >
                      run {hop.run_id}
                    </span>
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

  // AC3 is "the trace shows retrieval candidates both used AND not used". An
  // empty list used to render nothing at all, which left a reviewer unable to
  // distinguish "retrieval never ran for this finding" from "retrieval ran and
  // proposed nothing" — the same ambiguity the incomplete-chain notice below
  // exists to remove, left unfixed one component down. So the section always
  // renders and says which case it is.
  if (candidates.length === 0) {
    return (
      <div
        className="mt-3 rounded-md border border-border/70 bg-panel/50 px-2 py-2 text-[11px] leading-relaxed text-muted"
        data-testid="trace-retrieval-empty"
      >
        No retrieval candidates were proposed for this finding — its context was
        composed from the knowledge graph alone, not from indexed content.
      </div>
    );
  }

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

/**
 * 2.0-B1 T4 (AC4/AC6) — download this finding's signed evidence bundle.
 *
 * The bundle is built and signed on demand and streamed; nothing is stored
 * server-side except the audit record of the disclosure (AC6), so there is no
 * "previous exports" list to offer.
 *
 * Two things this button is deliberately careful about:
 *
 *   * it does NOT hide itself when the chain is incomplete. An auditor is
 *     entitled to the bundle for a finding whose provenance stops at the
 *     evidence layer — but the bundle then attests to a partial chain, so the
 *     button says so BEFORE the download rather than letting a signed artifact
 *     reach a third party looking more authoritative than it is;
 *   * the failure message is specific. A 403 (not analyst), a 400 (the
 *     installation has no license report_key, or a content-discipline
 *     violation blocked the export) and a network fault are different problems
 *     with different fixes, and "export failed" sends the reader nowhere.
 */
function ExportEvidenceButton({
  runId,
  oppId,
  complete,
}: {
  runId: string;
  oppId: string;
  complete: boolean;
}) {
  const [busy, setBusy] = useState<'report' | 'bundle' | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(kind: 'report' | 'bundle', action: () => Promise<void>) {
    if (busy) return;
    setBusy(kind);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(exportErrorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const partial = complete
    ? ''
    : ' This chain stops short of its source records, and the export records that.';

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-1.5">
        {/* Primary: what a person reads. Listed first because that is what
            almost every click wants — the verifiable artifact is for the auditor
            who asks for it, not the default. */}
        <button
          type="button"
          onClick={() =>
            run('report', () => downloadEvidenceReportForFinding(runId, oppId))
          }
          disabled={busy !== null}
          data-testid="trace-export-report"
          title={`Download a readable PDF of this finding's evidence and provenance chain.${partial}`}
          className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs font-medium text-text hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-60"
        >
          <FileText size={11} />
          {busy === 'report' ? 'Preparing…' : 'Evidence report (PDF)'}
        </button>
        {/* Secondary: the artifact that actually verifies. */}
        <button
          type="button"
          onClick={() =>
            run('bundle', () => downloadFindingEvidenceBundle(runId, oppId))
          }
          disabled={busy !== null}
          data-testid="trace-export-bundle"
          title="Download the signed bundle (.json) an auditor verifies offline with scripts/verify_evidence_export.py. Do not edit or reformat it — any altered byte fails verification."
          className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs font-medium text-muted hover:border-accent hover:text-accent disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Download size={11} />
          {busy === 'bundle' ? 'Preparing…' : 'Signed bundle'}
        </button>
      </div>
      {error && (
        <span
          data-testid="trace-export-error"
          role="alert"
          className="max-w-[18rem] text-right text-[10px] leading-tight text-red-500"
        >
          {error}
        </span>
      )}
    </div>
  );
}

/** Turn an export failure into something the reader can act on. */
function exportErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) {
      return 'Signed exports are analyst-only.';
    }
    if (err.status === 404) {
      return 'This run is no longer available.';
    }
    const body = err.body;
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail?: unknown }).detail;
      if (typeof detail === 'string' && detail.trim()) return detail;
    }
    return `Export failed (${err.status}).`;
  }
  return 'Export failed — could not reach the server.';
}

export default function TraceGraphPanel({
  runId,
  oppId,
}: {
  runId: string | null | undefined;
  oppId: string | null | undefined;
}) {
  const auth = useAuthOptional();
  const viewer = isViewerRole(auth?.user?.role);
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
        <div className="flex shrink-0 items-center gap-2">
          {/* Analyst+ only, mirroring the endpoint. The trace itself is viewer-
              readable; issuing a SIGNED artifact that leaves the deployment is a
              disclosure, and is gated one level higher. */}
          {data && data.available && !viewer && (
            <ExportEvidenceButton
              runId={runId}
              oppId={oppId}
              complete={data.complete}
            />
          )}
          {data && data.available && (
            <span className="shrink-0 rounded border border-bg px-1.5 py-0.5 text-xs text-text">
              {data.hops.length} hop{data.hops.length === 1 ? '' : 's'}
            </span>
          )}
        </div>
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
            {!data.complete && (
              // A chain that stops above the source records is shown, not hidden —
              // it is the one a reviewer most needs to see. But it must say so,
              // otherwise a short chain is indistinguishable from a complete one.
              <div
                className="mt-2 text-[11px] leading-relaxed text-muted"
                data-testid="trace-graph-incomplete"
              >
                {data.incompleteReason === 'no_source_record'
                  ? 'This chain stops at the evidence layer — no originating source records were recorded for this run. Re-running discovery populates them.'
                  : 'This chain does not yet reach its source records.'}
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
