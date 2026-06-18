import React, { forwardRef } from 'react';
import type { OpportunityCandidate } from '../../types/analystReview';
import SnapshotMatrix, { LIGHT_MATRIX_PALETTE } from './SnapshotMatrix';
import { LEADERSHIP_ACTIONS } from './KeyInsights';

/**
 * ExecutiveReportPdfDocument — the print/PDF layout for the Executive Report.
 *
 * This is a presentational, always-light, single-column A4-shaped document. It
 * is rendered OFF-SCREEN by ExecutiveReportPage and captured by
 * utils/exportPdf. It deliberately does NOT include the app navbar, the
 * download buttons, or any interactive controls — only the report content a
 * leadership audience should see.
 *
 * Light theme is guaranteed by the `pdf-light-scope` wrapper class (see
 * styles.css), which redefines the theme CSS variables locally. The Effort vs
 * Impact chart is passed LIGHT_MATRIX_PALETTE because html2canvas cannot resolve
 * CSS variables inside a serialized <svg>.
 *
 * `data-pdf-break` markers flag safe page-break points so the paginator never
 * slices through a card.
 */

interface RoadmapStageLike {
  opportunities: unknown[];
  requiredPermissions: { required: boolean; satisfied: boolean; label: string }[];
}

export interface ExecutiveReportPdfDocumentProps {
  confidence: string;
  sourcesLabel: string;
  quickWinsCount: number;
  roadmapStageLabel: string;
  summary: string;
  quickWins: OpportunityCandidate[];
  stages: RoadmapStageLike[];
  blockerCount: number;
  overallReadiness: string;
  opportunities: OpportunityCandidate[];
  orgName?: string | null;
  generatedAt: string;
  runId?: string | null;
}

const DOC_WIDTH_PX = 720;

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="mb-3 text-[15px] font-semibold tracking-tight text-text">{children}</h2>
  );
}

function Bullet() {
  return <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-accent" aria-hidden />;
}

const ExecutiveReportPdfDocument = forwardRef<HTMLDivElement, ExecutiveReportPdfDocumentProps>(
  function ExecutiveReportPdfDocument(props, ref) {
    const {
      confidence,
      sourcesLabel,
      quickWinsCount,
      roadmapStageLabel,
      summary,
      quickWins,
      stages,
      blockerCount,
      overallReadiness,
      opportunities,
      orgName,
      generatedAt,
      runId,
    } = props;

    const stats = [
      { label: 'Overall Confidence', value: confidence },
      { label: 'Sources Analyzed', value: sourcesLabel },
      { label: 'Top Opportunities', value: `${quickWinsCount} Quick Wins` },
      { label: 'Agent Roadmap', value: roadmapStageLabel },
    ];

    const roadmapLines: string[] = [
      `${stages[0]?.opportunities.length ?? 0} opportunities planned for Phase 1.`,
      `${stages[1]?.opportunities.length ?? 0} opportunities planned for Phase 2.`,
      `${stages[2]?.opportunities.length ?? 0} opportunities planned for Phase 3.`,
    ];
    if (blockerCount > 0) {
      roadmapLines.push(
        `${blockerCount} required data permission${blockerCount > 1 ? 's' : ''} still missing — resolve before pilots start.`,
      );
    }

    return (
      <div
        ref={ref}
        className="pdf-light-scope font-sans text-text"
        style={{ width: DOC_WIDTH_PX, padding: 32, boxSizing: 'border-box', background: '#ffffff' }}
      >
        {/* ── Header ─────────────────────────────────────────────── */}
        <header>
          <div className="flex items-start justify-between gap-4">
            {/* Brand wordmark rendered as styled text (not an <img>) so it
                rasterizes reliably in html2canvas without any image/CORS load. */}
            <div className="flex items-center gap-2.5">
              <span
                aria-hidden
                style={{
                  display: 'inline-flex',
                  height: 30,
                  width: 30,
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: 9,
                  background: 'rgb(13 85 215)',
                  color: '#ffffff',
                  fontWeight: 800,
                  fontSize: 13,
                  letterSpacing: '-0.02em',
                }}
              >
                iQ
              </span>
              <span className="text-[19px] font-bold tracking-tight" style={{ color: 'rgb(7 25 58)' }}>
                Agent<span style={{ color: 'rgb(13 85 215)' }}>IQ</span>
              </span>
            </div>
            <div className="text-right text-[11px] leading-relaxed text-muted">
              <div className="font-semibold uppercase tracking-[0.18em] text-accent">Confidential</div>
              {orgName ? <div>Prepared for {orgName}</div> : null}
              <div>Generated {generatedAt}</div>
              {runId ? <div>Run · {runId.slice(0, 8)}</div> : null}
            </div>
          </div>

          <div className="mt-5 text-[11px] font-semibold uppercase tracking-[0.2em] text-accent">
            Executive Report
          </div>
          <h1 className="mt-1 text-[26px] font-semibold leading-tight tracking-tight text-text">
            Board-Ready Discovery Summary
          </h1>
          <p className="mt-1.5 max-w-2xl text-[12.5px] leading-relaxed text-muted">
            Summary of source coverage, confidence, opportunity value, and implementation
            readiness across the Agent Roadmap.
          </p>
          <div
            className="mt-4 h-[3px] w-full rounded-full"
            style={{ background: 'linear-gradient(90deg, rgb(13 85 215) 0%, rgb(13 85 215 / 0.15) 100%)' }}
          />
        </header>

        {/* ── KPI stat row ───────────────────────────────────────── */}
        <section className="mt-6 grid grid-cols-4 gap-3">
          {stats.map((s) => (
            <div
              key={s.label}
              className="rounded-xl border border-border bg-panel px-3.5 py-3"
              style={{ boxShadow: '0 1px 2px rgb(7 25 58 / 0.06), 0 6px 16px rgb(7 25 58 / 0.05)' }}
            >
              <div className="text-[9.5px] font-semibold uppercase tracking-wide text-muted">
                {s.label}
              </div>
              <div className="mt-1.5 text-[18px] font-semibold leading-tight text-text">{s.value}</div>
            </div>
          ))}
        </section>

        <div data-pdf-break aria-hidden style={{ height: 1 }} />

        {/* ── Key Insights ───────────────────────────────────────── */}
        <section className="mt-7">
          <SectionTitle>Key Insights</SectionTitle>
          <p className="text-[12.5px] leading-relaxed text-text">{summary}</p>

          <div className="mt-4 rounded-xl border border-border bg-bg/30 p-4">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-text">
              What leadership should do next
            </div>
            <ul className="mt-2.5 space-y-2">
              {LEADERSHIP_ACTIONS.map((action) => (
                <li key={action} className="flex items-start gap-2.5 text-[12px] leading-relaxed text-text">
                  <Bullet />
                  <span>{action}</span>
                </li>
              ))}
            </ul>
          </div>
        </section>

        <div data-pdf-break aria-hidden style={{ height: 1 }} />

        {/* ── Top Quick Wins ─────────────────────────────────────── */}
        <section className="mt-7">
          <SectionTitle>Top Quick Wins</SectionTitle>
          {quickWins.length === 0 ? (
            <div className="rounded-xl border border-border bg-bg/20 px-3.5 py-3 text-[12px] text-muted">
              No quick wins identified for this discovery run.
            </div>
          ) : (
            <div className="space-y-2.5">
              {quickWins.map((o, i) => (
                <div
                  key={o.id}
                  className="flex items-start gap-3 rounded-xl border border-border bg-bg/20 px-3.5 py-2.5"
                >
                  <div
                    className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-accent text-[11px] font-semibold"
                    style={{ color: '#ffffff' }}
                  >
                    {i + 1}
                  </div>
                  <div className="min-w-0">
                    <div className="text-[12.5px] font-semibold text-text">{o.title}</div>
                    <div className="mt-0.5 text-[11px] text-muted">
                      {o.category} · Impact {o.impact}/10 · Effort {o.effort}/10
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* ── Agent Roadmap Highlights ───────────────────────────── */}
        <section className="mt-7">
          <SectionTitle>Agent Roadmap Highlights</SectionTitle>
          <ul className="space-y-2">
            {roadmapLines.map((line) => (
              <li key={line} className="flex items-start gap-2.5 text-[12px] leading-relaxed text-text">
                <Bullet />
                <span>{line}</span>
              </li>
            ))}
            <li className="flex items-start gap-2.5 text-[12px] leading-relaxed text-text">
              <Bullet />
              <span>
                Overall readiness: <span className="font-semibold">{overallReadiness}</span>.
              </span>
            </li>
          </ul>
        </section>

        <div data-pdf-break aria-hidden style={{ height: 1 }} />

        {/* ── Effort vs Impact ───────────────────────────────────── */}
        <section className="mt-7">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-[15px] font-semibold tracking-tight text-text">Effort vs Impact</h2>
            <span className="text-[10.5px] text-muted">Read-only opportunity snapshot</span>
          </div>
          <div style={{ height: 360 }}>
            <SnapshotMatrix opportunities={opportunities} palette={LIGHT_MATRIX_PALETTE} bare />
          </div>
        </section>

        {/* ── Footer ─────────────────────────────────────────────── */}
        <footer className="mt-8 border-t border-border pt-3">
          <div className="flex items-center justify-between text-[10px] text-muted">
            <span>Confidential — prepared for internal leadership review.</span>
            <span>Generated by AgentIQ · {generatedAt}</span>
          </div>
        </footer>
      </div>
    );
  },
);

export default ExecutiveReportPdfDocument;
