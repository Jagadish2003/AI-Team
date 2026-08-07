// @vitest-environment jsdom
/**
 * 2.0-B1 T3 — Interrogation UI (drill-down) tests.
 *
 * TraceGraphPanel has no acceptance criteria of its own (it supports 2.0-B1's
 * AC1/AC3 usability) — these tests pin its rendering contract against the
 * trace-graph API shape instead: the hop tree nests correctly by
 * from_hop_id, joins attach to the hop they corroborate, retrieval
 * candidates show used-vs-unused with counts, a deep link renders only when
 * the underlying hop actually carries one, and loading/error/empty states
 * degrade gracefully.
 */
import React from 'react';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DataCacheProvider } from '../lib/dataCache';
import type { TraceGraphResponse, TraceHop } from '../types/traceGraph';
import detailSource from '../components/analyst_review/OpportunityDetail.tsx?raw';

const { fetchTraceGraphMock } = vi.hoisted(() => ({
  fetchTraceGraphMock: vi.fn(),
}));

vi.mock('../api/traceGraphApi', () => ({
  fetchTraceGraph: fetchTraceGraphMock,
  hopDeepLink: (hop: TraceHop) => {
    const value = (hop.detail as Record<string, unknown>)?.source_url ?? (hop.detail as Record<string, unknown>)?.deep_link;
    return typeof value === 'string' && value.trim().length > 0 ? value : null;
  },
}));

import TraceGraphPanel from '../components/analyst_review/TraceGraphPanel';

afterEach(() => {
  cleanup();
  fetchTraceGraphMock.mockReset();
});

function hop(overrides: Partial<TraceHop> & Pick<TraceHop, 'hop_id' | 'hop_type'>): TraceHop {
  return {
    label: overrides.hop_id,
    origin: 'observed',
    connector: null,
    run_id: 'run_1',
    timestamp: null,
    from_hop_id: null,
    detail: {},
    ...overrides,
  };
}

function makeTraceGraph(overrides: Partial<TraceGraphResponse> = {}): TraceGraphResponse {
  return {
    runId: 'run_1',
    oppId: 'opp_1',
    hops: [],
    joins: [],
    complete: true,
    truncated: false,
    retrieval_candidates: [],
    retrieval_candidates_used_count: 0,
    retrieval_candidates_unused_count: 0,
    available: true,
    ...overrides,
  };
}

function renderPanel(runId: string | null = 'run_1', oppId: string | null = 'opp_1') {
  return render(
    <DataCacheProvider>
      <TraceGraphPanel runId={runId} oppId={oppId} />
    </DataCacheProvider>
  );
}

describe('TraceGraphPanel', () => {
  it('renders nothing when runId or oppId is missing', () => {
    const { container } = renderPanel(null, 'opp_1');
    expect(container).toBeEmptyDOMElement();
  });

  it('shows a loading state before the fetch resolves', async () => {
    let resolveFn: (value: TraceGraphResponse) => void = () => {};
    fetchTraceGraphMock.mockReturnValue(
      new Promise<TraceGraphResponse>((resolve) => {
        resolveFn = resolve;
      })
    );
    renderPanel();
    expect(screen.getByTestId('trace-graph-loading')).toBeInTheDocument();
    resolveFn(makeTraceGraph());
    await waitFor(() => expect(screen.queryByTestId('trace-graph-loading')).not.toBeInTheDocument());
  });

  it('shows an error state when the fetch fails', async () => {
    fetchTraceGraphMock.mockRejectedValue(new Error('boom'));
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('trace-graph-error')).toBeInTheDocument());
  });

  it('shows an empty state when the trace is not available', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph({ available: false, hops: [] }));
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('trace-graph-empty')).toBeInTheDocument());
  });

  it('renders the hop chain nested by from_hop_id, with origin pills', async () => {
    const finding = hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'Repetitive approval routing' });
    const evidence = hop({
      hop_id: 'evidence:ev_1', hop_type: 'evidence', label: 'High volume detected',
      from_hop_id: 'finding:opp_1', connector: 'salesforce', origin: 'observed',
    });
    const sourceRecord = hop({
      hop_id: 'source_record:salesforce:ev_1:0', hop_type: 'source_record', label: 'ev_1',
      from_hop_id: 'evidence:ev_1', connector: 'salesforce', origin: 'inferred',
    });
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({ hops: [finding, evidence, sourceRecord] })
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId('trace-hop-finding:opp_1')).toBeInTheDocument());
    expect(screen.getByTestId('trace-hop-evidence:ev_1')).toBeInTheDocument();
    expect(screen.getByTestId('trace-hop-source_record:salesforce:ev_1:0')).toBeInTheDocument();

    // The finding hop (whose only child is observed) shows Observed; the
    // source-record leaf marked inferred shows Inferred.
    const findingNode = screen.getByTestId('trace-hop-finding:opp_1');
    expect(findingNode.querySelector('[data-testid="trace-origin-observed"]')).toBeInTheDocument();
    const leafNode = screen.getByTestId('trace-hop-source_record:salesforce:ev_1:0');
    expect(leafNode.querySelector('[data-testid="trace-origin-inferred"]')).toBeInTheDocument();
  });

  it('collapses and expands a hop with children via its toggle', async () => {
    const finding = hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'x' });
    const evidence = hop({
      hop_id: 'evidence:ev_1', hop_type: 'evidence', label: 'y', from_hop_id: 'finding:opp_1',
    });
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph({ hops: [finding, evidence] }));
    renderPanel();

    await waitFor(() => expect(screen.getByTestId('trace-hop-evidence:ev_1')).toBeInTheDocument());
    const toggle = screen.getByTestId('trace-hop-toggle-finding:opp_1');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('trace-hop-evidence:ev_1')).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('trace-hop-evidence:ev_1')).toBeInTheDocument();
  });

  it('shows the join type and window on the hop it corroborates', async () => {
    const finding = hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'x' });
    const eventHop = hop({
      hop_id: 'source_record:event_signature:sig-1:0', hop_type: 'source_record',
      label: 'event_signature:sig-1', from_hop_id: 'finding:opp_1', connector: 'events',
    });
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        hops: [finding, eventHop],
        joins: [{
          join_type: 'event_incident', window_seconds: 7200, delta_seconds: 300,
          within_window: true, a_at: null, b_at: null,
          hop_id: 'source_record:event_signature:sig-1:0',
        }],
      })
    );
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId('trace-join-source_record:event_signature:sig-1:0-0')).toBeInTheDocument()
    );
    expect(screen.getByText(/event_incident/)).toBeInTheDocument();
    expect(screen.getByText(/7200s window/)).toBeInTheDocument();
    expect(screen.getByText(/300s apart/)).toBeInTheDocument();
  });

  it('renders a deep link only when the hop actually carries a source_url', async () => {
    const finding = hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'x' });
    const withLink = hop({
      hop_id: 'source_record:incident:INC1:0', hop_type: 'source_record', label: 'INC1',
      from_hop_id: 'finding:opp_1', connector: 'servicenow',
      detail: { source_url: 'https://instance.service-now.com/incident.do?sys_id=abc' },
    });
    const withoutLink = hop({
      hop_id: 'source_record:incident:INC2:0', hop_type: 'source_record', label: 'INC2',
      from_hop_id: 'finding:opp_1', connector: 'servicenow', detail: {},
    });
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph({ hops: [finding, withLink, withoutLink] }));
    renderPanel();

    await waitFor(() =>
      expect(screen.getByTestId('trace-hop-link-source_record:incident:INC1:0')).toBeInTheDocument()
    );
    expect(screen.getByTestId('trace-hop-link-source_record:incident:INC1:0')).toHaveAttribute(
      'href', 'https://instance.service-now.com/incident.do?sys_id=abc'
    );
    expect(screen.queryByTestId('trace-hop-link-source_record:incident:INC2:0')).not.toBeInTheDocument();
  });

  it('shows used vs. unused retrieval candidates with counts, expandable', async () => {
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        hops: [hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'x' })],
        retrieval_candidates: [
          {
            chunk_id: 'c1', used: true, decision: 'included', reason: 'included@position_1',
            confidence: 0.9, origin: 'observed', source_system: 'confluence',
            source_artifact: 'page-1', content_snippet: 'relevant text', is_stale: false,
          },
          {
            chunk_id: 'c2', used: false, decision: 'excluded', reason: 'below_confidence_floor',
            confidence: 0.02, origin: 'observed', source_system: 'git',
            source_artifact: 'README.md', content_snippet: 'unrelated text', is_stale: false,
          },
        ],
        retrieval_candidates_used_count: 1,
        retrieval_candidates_unused_count: 1,
      })
    );
    renderPanel();

    await waitFor(() => expect(screen.getByTestId('trace-retrieval-toggle')).toBeInTheDocument());
    expect(screen.getByText('Retrieval candidates (1 used, 1 not used)')).toBeInTheDocument();
    expect(screen.queryByTestId('trace-retrieval-list')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('trace-retrieval-toggle'));
    expect(screen.getByTestId('trace-retrieval-c1')).toBeInTheDocument();
    expect(screen.getByTestId('trace-retrieval-c2')).toBeInTheDocument();
    expect(screen.getByTestId('trace-retrieval-c1')).toHaveTextContent('Used');
    expect(screen.getByTestId('trace-retrieval-c2')).toHaveTextContent('Not used');
  });

  it('omits the retrieval-candidates section entirely when there are none', async () => {
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({ hops: [hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'x' })] })
    );
    renderPanel();
    await waitFor(() => expect(screen.getByTestId('trace-hop-finding:opp_1')).toBeInTheDocument());
    expect(screen.queryByTestId('trace-retrieval-toggle')).not.toBeInTheDocument();
  });

  it('shows a truncation notice when the trace was capped', async () => {
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        hops: [hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'x' })],
        truncated: true,
      })
    );
    renderPanel();
    await waitFor(() => expect(screen.getByText(/This chain is large/)).toBeInTheDocument());
  });

  // 2.0-B1 AC1: a chain that stops above the source records is SHOWN — it is the
  // one a reviewer most needs to interrogate — but it has to say so, or a short
  // chain is indistinguishable from a complete one.
  it('still renders an incomplete chain, and says why it stops', async () => {
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        hops: [
          hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'Automate repetitive flows' }),
          hop({
            hop_id: 'evidence:ev_1',
            hop_type: 'evidence',
            label: 'Multiple low-complexity flows on a high-volume object',
            from_hop_id: 'finding:opp_1',
          }),
        ],
        complete: false,
        incompleteReason: 'no_source_record',
        available: true,
      })
    );
    renderPanel();

    // The chain is not hidden behind the empty state.
    await waitFor(() =>
      expect(screen.getByText('Automate repetitive flows')).toBeInTheDocument()
    );
    expect(screen.queryByTestId('trace-graph-empty')).not.toBeInTheDocument();
    // And the shortfall is stated rather than left to be inferred from hop count.
    expect(screen.getByTestId('trace-graph-incomplete')).toHaveTextContent(
      /stops at the evidence layer/
    );
  });

  it('shows no incompleteness note once the chain reaches a source record', async () => {
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        hops: [
          hop({ hop_id: 'finding:opp_1', hop_type: 'finding', label: 'f' }),
          hop({ hop_id: 'source_record:sf:rec:0', hop_type: 'source_record', label: 'rec',
                from_hop_id: 'finding:opp_1' }),
        ],
        complete: true,
        incompleteReason: null,
      })
    );
    renderPanel();
    await waitFor(() => expect(screen.getByText('rec')).toBeInTheDocument());
    expect(screen.queryByTestId('trace-graph-incomplete')).not.toBeInTheDocument();
  });
  // 2.0-B1 story item 3 (Interrogation UI) — the WIRING, not the panel.
  //
  // Kept in this file deliberately rather than a new one: the panel tests above
  // could not catch what actually shipped — OpportunityDetail rendered
  // <TraceGraphPanel /> while never importing it, so React received `undefined`
  // as an element type and every test that rendered the Opportunity Review detail
  // crashed (40 of them). Testing the panel in isolation says nothing about
  // whether the page meant to host it can mount.
  describe('OpportunityDetail wiring', () => {
    // Read via Vite's `?raw` rather than node:fs — the frontend tsconfig types
    // only `vite/client` (no @types/node), so node:* imports fail `tsc -b` and
    // break the build gate. `?raw` is declared by vite/client and works in both
    // vitest and the build.

    it('imports the source-trace panel it renders', () => {
      // Both halves: rendering without importing is the defect, and importing
      // without rendering would silently drop the interrogation surface.
      expect(detailSource).toMatch(/<TraceGraphPanel\s/);
      expect(detailSource).toMatch(
        /import\s+TraceGraphPanel\s+from\s+["']\.\/TraceGraphPanel["']/
      );
    });

    it('passes the run and opportunity the panel needs to fetch a trace', () => {
      // A panel mounted without both ids renders nothing, which looks identical to
      // "this finding has no trace".
      const usage = detailSource.match(/<TraceGraphPanel[^/>]*/)?.[0] ?? '';
      expect(usage).toMatch(/runId=/);
      expect(usage).toMatch(/oppId=/);
    });
  });
});
