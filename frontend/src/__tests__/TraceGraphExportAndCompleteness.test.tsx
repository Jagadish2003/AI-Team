// @vitest-environment jsdom
/**
 * 2.0-B1 UI completion — the three gaps found when the Source Trace panel was
 * walked against the story text.
 *
 * Each test here corresponds to something the story asked for that the panel did
 * not do:
 *
 *   AC1 — "every hop carries origin, connector, RUN ID, and timestamp". The API
 *         always returned run_id; the panel rendered the other three. It matters
 *         once a chain spans runs, because otherwise a reviewer cannot tell which
 *         run observed which hop.
 *
 *   AC3 — "the trace shows retrieval candidates both used AND not used". An empty
 *         candidate list rendered nothing at all, leaving "retrieval never ran"
 *         indistinguishable from "retrieval ran and proposed nothing" — the same
 *         ambiguity the incomplete-chain notice exists to remove, one component
 *         down.
 *
 *   AC4/AC6 — "any finding exports as a signed, auditable bundle". Fully built in
 *         the backend, reachable only by curl. The export is a DISCLOSURE, so the
 *         button is analyst+ and it must not pretend an incomplete chain is a
 *         complete one.
 *
 * Run: npx vitest run src/__tests__/TraceGraphExportAndCompleteness.test.tsx
 */
import React from 'react';
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DataCacheProvider } from '../lib/dataCache';
import type { TraceGraphResponse, TraceHop } from '../types/traceGraph';

const { fetchTraceGraphMock, reportMock, bundleMock, roleMock } = vi.hoisted(() => ({
  fetchTraceGraphMock: vi.fn(),
  reportMock: vi.fn(),
  bundleMock: vi.fn(),
  roleMock: { current: 'analyst' as string },
}));

vi.mock('../api/traceGraphApi', () => ({
  fetchTraceGraph: fetchTraceGraphMock,
}));

vi.mock('../api/evidenceExportApi', () => ({
  downloadEvidenceReportForFinding: reportMock,
  downloadFindingEvidenceBundle: bundleMock,
}));

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { role: roleMock.current } }),
}));

// isViewerRole falls back to an env signal when there is no authenticated role;
// the auth mock above always supplies one, so the real helper is used unmocked.
import { ApiError } from '../lib/apiClient';
import TraceGraphPanel from '../components/analyst_review/TraceGraphPanel';

afterEach(() => {
  cleanup();
  fetchTraceGraphMock.mockReset();
  reportMock.mockReset();
  bundleMock.mockReset();
  roleMock.current = 'analyst';
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
    hops: [hop({ hop_id: 'finding:1', hop_type: 'finding', label: 'A finding' })],
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

function renderPanel() {
  return render(
    <DataCacheProvider>
      <TraceGraphPanel runId="run_1" oppId="opp_1" />
    </DataCacheProvider>,
  );
}

// ---------------------------------------------------------------------------
// AC1 — the run id
// ---------------------------------------------------------------------------

describe('AC1 — every hop carries its run id', () => {
  it('renders the run id alongside origin, connector and timestamp', async () => {
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        hops: [
          hop({
            hop_id: 'finding:1',
            hop_type: 'finding',
            label: 'A finding',
            connector: 'servicenow',
            timestamp: '07 Aug 2026, 07:35',
            run_id: 'run_42',
          }),
        ],
      }),
    );
    renderPanel();
    const runLabel = await screen.findByTestId('trace-hop-run-finding:1');
    expect(runLabel).toHaveTextContent('run_42');
    // The other three AC1 fields are still there — this must be additive.
    expect(screen.getByText('servicenow')).toBeInTheDocument();
    expect(screen.getByText('07 Aug 2026, 07:35')).toBeInTheDocument();
    expect(screen.getByTestId('trace-origin-observed')).toBeInTheDocument();
  });

  it('shows each hop its OWN run id when a chain spans runs', async () => {
    // The case the field exists for: an entity first seen in an earlier run.
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        hops: [
          hop({ hop_id: 'finding:1', hop_type: 'finding', run_id: 'run_42' }),
          hop({
            hop_id: 'evidence:1',
            hop_type: 'evidence',
            from_hop_id: 'finding:1',
            run_id: 'run_7',
          }),
        ],
      }),
    );
    renderPanel();
    expect(await screen.findByTestId('trace-hop-run-finding:1')).toHaveTextContent('run_42');
    expect(screen.getByTestId('trace-hop-run-evidence:1')).toHaveTextContent('run_7');
  });
});

// ---------------------------------------------------------------------------
// AC3 — retrieval candidates, including when there are none
// ---------------------------------------------------------------------------

describe('AC3 — an empty candidate list says so rather than vanishing', () => {
  it('states that no candidates were proposed', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph({ retrieval_candidates: [] }));
    renderPanel();
    const empty = await screen.findByTestId('trace-retrieval-empty');
    expect(empty).toHaveTextContent(/no retrieval candidates were proposed/i);
    // And it does not pretend a list exists.
    expect(screen.queryByTestId('trace-retrieval-toggle')).not.toBeInTheDocument();
  });

  it('still renders the used/unused toggle when candidates exist', async () => {
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({
        retrieval_candidates: [
          {
            chunk_id: 'c1',
            used: true,
            decision: 'included',
            reason: null,
            confidence: 0.9,
            origin: 'observed',
            source_system: 'confluence',
            source_artifact: 'page-1',
            content_snippet: 'runbook step',
            is_stale: false,
          },
        ],
        retrieval_candidates_used_count: 1,
        retrieval_candidates_unused_count: 0,
      }),
    );
    renderPanel();
    expect(await screen.findByTestId('trace-retrieval-toggle')).toHaveTextContent(
      '1 used, 0 not used',
    );
    expect(screen.queryByTestId('trace-retrieval-empty')).not.toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// AC4 / AC6 — the signed export
// ---------------------------------------------------------------------------

describe('AC4/AC6 — the two evidence downloads', () => {
  it('the readable PDF is offered first — it is what almost every click wants', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    renderPanel();
    const report = await screen.findByTestId('trace-export-report');
    const bundle = screen.getByTestId('trace-export-bundle');
    expect(report).toHaveTextContent(/Evidence report \(PDF\)/i);
    expect(bundle).toHaveTextContent(/Signed bundle/i);
    // Document order: readable first.
    expect(report.compareDocumentPosition(bundle) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('downloads the readable report for this run and finding', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    reportMock.mockResolvedValue(undefined);
    renderPanel();
    fireEvent.click(await screen.findByTestId('trace-export-report'));
    await waitFor(() => expect(reportMock).toHaveBeenCalledWith('run_1', 'opp_1'));
    expect(bundleMock).not.toHaveBeenCalled();
  });

  it('downloads the signed bundle separately', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    bundleMock.mockResolvedValue(undefined);
    renderPanel();
    fireEvent.click(await screen.findByTestId('trace-export-bundle'));
    await waitFor(() => expect(bundleMock).toHaveBeenCalledWith('run_1', 'opp_1'));
    expect(reportMock).not.toHaveBeenCalled();
  });

  it('warns that the bundle must not be edited — that is what makes it verifiable', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    renderPanel();
    const bundle = await screen.findByTestId('trace-export-bundle');
    expect(bundle.getAttribute('title')).toMatch(/any altered byte fails verification/i);
  });

  it('hides both from a viewer — issuing evidence is a disclosure', async () => {
    roleMock.current = 'viewer';
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    renderPanel();
    // The trace itself still renders; only the exports are withheld.
    expect(await screen.findByTestId('trace-graph-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('trace-export-report')).not.toBeInTheDocument();
    expect(screen.queryByTestId('trace-export-bundle')).not.toBeInTheDocument();
  });

  it('stays available on an INCOMPLETE chain, and says the chain is partial', async () => {
    // An auditor is entitled to the evidence for a partial finding. What must not
    // happen is an artifact leaving without that fact attached.
    fetchTraceGraphMock.mockResolvedValue(
      makeTraceGraph({ complete: false, incompleteReason: 'no_source_record' }),
    );
    renderPanel();
    const report = await screen.findByTestId('trace-export-report');
    expect(report.getAttribute('title')).toMatch(/stops short of its source records/i);
  });

  it('names the reason when an export is refused', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    reportMock.mockRejectedValue(
      new ApiError('GET failed', 400, { detail: 'License carries no report_key.' }),
    );
    renderPanel();
    fireEvent.click(await screen.findByTestId('trace-export-report'));
    // The server's specific reason survives to the user — "export failed" alone
    // would send them nowhere.
    expect(await screen.findByTestId('trace-export-error')).toHaveTextContent(
      'License carries no report_key.',
    );
  });

  it('reports an analyst-only refusal in role terms', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    bundleMock.mockRejectedValue(new ApiError('GET failed', 403, {}));
    renderPanel();
    fireEvent.click(await screen.findByTestId('trace-export-bundle'));
    expect(await screen.findByTestId('trace-export-error')).toHaveTextContent(
      /analyst-only/i,
    );
  });

  it('recovers after a failed export instead of staying stuck', async () => {
    fetchTraceGraphMock.mockResolvedValue(makeTraceGraph());
    reportMock.mockRejectedValueOnce(new ApiError('GET failed', 500, {}));
    renderPanel();
    const button = await screen.findByTestId('trace-export-report');
    fireEvent.click(button);
    await screen.findByTestId('trace-export-error');
    expect(button).not.toBeDisabled();

    reportMock.mockResolvedValueOnce(undefined);
    fireEvent.click(button);
    await waitFor(() =>
      expect(screen.queryByTestId('trace-export-error')).not.toBeInTheDocument(),
    );
  });
});
