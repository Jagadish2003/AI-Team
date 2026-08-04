/**
 * Discovery Run — the Discovery Log "Refresh" button matches Run Health's.
 *
 * Run Health's button spins its icon, swaps its label to "Refreshing…", and goes
 * disabled + aria-busy while the reads are in flight (RunHealthDashboardPage). The
 * Discovery Run button was a bare button with none of that: a click produced no
 * feedback at all, because the page's `loading` flag is deliberately suppressed for
 * a run already on screen (re-showing it would blank the run into a loading panel).
 *
 * The context therefore exposes a separate, tracked `refresh`/`refreshing` pair.
 * These tests pin both halves: the button's busy behaviour, and the rule that a
 * SILENT revalidation (focus / interval / the terminal-status transition, which all
 * call `refetch`) never spins it.
 *
 * Run:
 *   npx vitest run src/__tests__/DiscoveryRunRefreshButton.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import React from 'react';
import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Context mocks ─────────────────────────────────────────────────────────────

const discoveryRunValue = {
  run: { id: 'run_1', status: 'complete', startedAt: '2026-01-01T00:00:00Z' },
  events: [{ id: 'e1', stage: 'CONNECT', message: 'Connected sources: salesforce' }],
  loading: false,
  error: null as string | null,
  started: true,
  computing: false,
  currentStep: 'complete',
  failedSteps: [] as string[],
  startRun: vi.fn(),
  restartRun: vi.fn(),
  refetch: vi.fn(),
  refresh: vi.fn(),
  refreshing: false,
};

vi.mock('../context/DiscoveryRunContext', () => ({
  useDiscoveryRunContext: () => discoveryRunValue,
}));
vi.mock('../context/RunContext', () => ({
  useRunContext: () => ({ runId: 'run_1', setRunId: vi.fn(), clearRunId: vi.fn() }),
}));
vi.mock('../context/ConnectorContext', () => ({
  // `all` is what TopNav reads; `connectors` is what the page reads.
  useConnectorContext: () => ({ all: [], connectors: [], refetch: vi.fn() }),
}));
vi.mock('../context/SourceIntakeContext', () => ({
  useSourceIntakeContext: () => ({ uploadedFiles: [] }),
}));
vi.mock('../lib/apiClient', () => ({
  ApiError: class ApiError extends Error {},
  apiGet: vi.fn().mockResolvedValue({ ok: true, products: [], labels: [] }),
}));

import DiscoveryRunPage from '../pages/DiscoveryRunPage';

function renderPage() {
  return render(
    <MemoryRouterStub>
      <DiscoveryRunPage />
    </MemoryRouterStub>,
  );
}

// The page uses useNavigate/useLocation; a real router is the least-mocked way to
// satisfy them.
import { MemoryRouter } from 'react-router-dom';
function MemoryRouterStub({ children }: { children: React.ReactNode }) {
  return <MemoryRouter initialEntries={['/discovery-run?runId=run_1']}>{children}</MemoryRouter>;
}

const refreshButton = () => screen.getByTestId('refresh-run');

// jsdom implements no scrollTo on elements; the Discovery Log's auto-scroll effect
// calls it. A no-op keeps the effect from throwing during these renders.
if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {
    /* jsdom stub */
  } as unknown as typeof Element.prototype.scrollTo;
}

describe('Discovery Run refresh button', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    discoveryRunValue.refreshing = false;
  });
  afterEach(() => cleanup());

  it('reads "Refresh" and is enabled when idle', () => {
    renderPage();
    const button = refreshButton();
    expect(button).toHaveTextContent('Refresh');
    expect(button).not.toHaveTextContent('Refreshing');
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute('aria-busy', 'false');
  });

  it('calls the tracked refresh (not the silent refetch) when clicked', () => {
    renderPage();
    fireEvent.click(refreshButton());
    expect(discoveryRunValue.refresh).toHaveBeenCalledTimes(1);
    // refetch is the SILENT path — the button must not use it, or the click would
    // produce no feedback (the original bug).
    expect(discoveryRunValue.refetch).not.toHaveBeenCalled();
  });

  it('shows the busy state while a refresh is in flight, like Run Health', () => {
    discoveryRunValue.refreshing = true;
    renderPage();

    const button = refreshButton();
    expect(button).toHaveTextContent('Refreshing…');
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');
    // The icon spins — the same animate-spin treatment Run Health uses.
    expect(button.querySelector('.animate-spin')).not.toBeNull();
  });

  it('does not spin when the run is merely computing (that is not a refresh)', () => {
    discoveryRunValue.refreshing = false;
    renderPage();
    expect(refreshButton()).toBeEnabled();
    expect(refreshButton().querySelector('.animate-spin')).toBeNull();
  });
});

// ── The context's own contract ────────────────────────────────────────────────
//
// Tested through a real provider so the busy flag is driven by the actual fetch
// lifecycle rather than a hand-set value.

const fetchRun = vi.fn();
const fetchRunEvents = vi.fn();
const fetchRunStatus = vi.fn();

vi.mock('../api/runApi', () => ({
  fetchRun: (...a: unknown[]) => fetchRun(...a),
  fetchRunEvents: (...a: unknown[]) => fetchRunEvents(...a),
  fetchRunStatus: (...a: unknown[]) => fetchRunStatus(...a),
  startRun: vi.fn(),
  replayRun: vi.fn(),
}));

describe('DiscoveryRunContext refresh tracking', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchRun.mockResolvedValue({ id: 'run_1', status: 'complete' });
    fetchRunEvents.mockResolvedValue([]);
    fetchRunStatus.mockResolvedValue({ status: 'complete', current_step: 'complete', failed_steps: [] });
  });
  afterEach(() => cleanup());

  it('flips refreshing while the refresh round runs, and clears it afterwards', async () => {
    // Imported lazily so the module-level context mocks above do not apply here.
    const { DiscoveryRunProvider, useDiscoveryRunContext } = await vi.importActual<
      typeof import('../context/DiscoveryRunContext')
    >('../context/DiscoveryRunContext');

    let ctx: ReturnType<typeof useDiscoveryRunContext> | null = null;
    function Probe() {
      ctx = useDiscoveryRunContext();
      return <div>{ctx.refreshing ? 'busy' : 'idle'}</div>;
    }

    render(
      <MemoryRouterStub>
        <DiscoveryRunProvider>
          <Probe />
        </DiscoveryRunProvider>
      </MemoryRouterStub>,
    );

    await waitFor(() => expect(fetchRun).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByText('idle')).toBeInTheDocument());

    const callsBefore = fetchRun.mock.calls.length;
    act(() => ctx!.refresh());

    // The busy state appears immediately and clears once the round settles.
    await waitFor(() => expect(screen.getByText('busy')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText('idle')).toBeInTheDocument());
    expect(fetchRun.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it('a silent refetch never reports the busy state', async () => {
    const { DiscoveryRunProvider, useDiscoveryRunContext } = await vi.importActual<
      typeof import('../context/DiscoveryRunContext')
    >('../context/DiscoveryRunContext');

    const seen: boolean[] = [];
    let ctx: ReturnType<typeof useDiscoveryRunContext> | null = null;
    function Probe() {
      ctx = useDiscoveryRunContext();
      seen.push(ctx.refreshing);
      return null;
    }

    render(
      <MemoryRouterStub>
        <DiscoveryRunProvider>
          <Probe />
        </DiscoveryRunProvider>
      </MemoryRouterStub>,
    );

    await waitFor(() => expect(fetchRun).toHaveBeenCalled());
    const callsBefore = fetchRun.mock.calls.length;
    act(() => ctx!.refetch());

    await waitFor(() => expect(fetchRun.mock.calls.length).toBeGreaterThan(callsBefore));
    // The data was re-read, but nothing ever reported busy — a background
    // revalidation must not spin the Refresh button.
    expect(seen.every((busy) => busy === false)).toBe(true);
  });
});
