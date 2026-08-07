/**
 * Integration Hub pickers — the loading skeleton belongs to the FIRST load only.
 *
 * The reported bug: every picker menu visibly re-loaded on a ~30s cadence. The
 * cause was not a poll — each picker fetched on mount with `[]` deps and so could
 * not refresh itself — it was a REMOUNT: the options lived in component-local
 * state with `loading` initialised to `true`, so any remount threw the data away
 * and showed the skeleton again while it re-fetched. The Integration Hub
 * re-renders on the shared cache's background connector revalidation, which is
 * where the 30s cadence came from.
 *
 * These tests pin the fix (see components/integrations/usePickerResource.ts):
 *   - first load shows the skeleton;
 *   - a REMOUNT renders straight from cache — no skeleton, no second request;
 *   - a refresh that finds IDENTICAL data changes nothing on screen;
 *   - a refresh that finds DIFFERENT data updates the options (still no skeleton);
 *   - a refresh landing mid-edit does not discard the user's unsaved selection.
 *
 * Run:
 *   npx vitest run src/__tests__/IntegrationHubPickerSilentRefresh.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import React, { useState } from 'react';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

const mockApiGet = vi.fn();
const mockApiPatch = vi.fn();
const mockApiPost = vi.fn();

vi.mock('../lib/apiClient', () => ({
  ApiError: class ApiError extends Error {
    body: unknown;
    constructor(message: string, body: unknown) {
      super(message);
      this.body = body;
    }
  },
  apiGet: (...args: unknown[]) => mockApiGet(...args),
  apiPatch: (...args: unknown[]) => mockApiPatch(...args),
  apiPost: (...args: unknown[]) => mockApiPost(...args),
}));

const mockPush = vi.fn();
vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mockPush }),
}));

import SlackChannelPicker from '../components/integrations/SlackChannelPicker';
import { DataCacheProvider, useDataCache } from '../lib/dataCache';
import { cacheKeys } from '../lib/cacheKeys';

const TWO_CHANNELS = [
  { id: 'C001', name: 'ops-incidents' },
  { id: 'C002', name: 'deploys' },
];

function channelsResponse(available: { id: string; name: string }[]) {
  return { ok: true, available, selected: [], configured: false };
}

/**
 * Harness holding ONE cache provider across a picker unmount/remount, plus a
 * button that invalidates the picker's key — the same foreground refetch a
 * connect/disconnect triggers (those invalidate the whole `connectors` prefix,
 * which the picker keys sit under).
 */
function Harness() {
  const [mounted, setMounted] = useState(true);
  const cache = useDataCache();
  return (
    <>
      <button type="button" onClick={() => setMounted((m) => !m)}>
        toggle picker
      </button>
      <button
        type="button"
        onClick={() => cache.invalidate(cacheKeys.connectors)}
      >
        invalidate connectors
      </button>
      {mounted ? <SlackChannelPicker /> : null}
    </>
  );
}

function renderHarness() {
  return render(
    <DataCacheProvider>
      <Harness />
    </DataCacheProvider>,
  );
}

const skeleton = () => screen.queryByLabelText('Loading Slack channels');

describe('Integration Hub picker — skeleton on first load only', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockApiGet.mockResolvedValue(channelsResponse(TWO_CHANNELS));
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('shows the skeleton on the first load, then the channels', async () => {
    renderHarness();
    // Before the fetch resolves the picker reserves its space with the skeleton
    // rather than flashing an empty picker.
    expect(skeleton()).toBeInTheDocument();
    expect(await screen.findByText('#ops-incidents')).toBeInTheDocument();
    expect(skeleton()).not.toBeInTheDocument();
  });

  it('renders from cache on a remount — no skeleton and no second request', async () => {
    renderHarness();
    await screen.findByText('#ops-incidents');
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    // Unmount and remount the picker inside the SAME cache provider — the exact
    // shape of the reported bug.
    fireEvent.click(screen.getByText('toggle picker'));
    expect(screen.queryByText('#ops-incidents')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('toggle picker'));

    // The channels are on screen in the same frame: no skeleton, and the cached
    // value is served without re-fetching.
    expect(screen.getByText('#ops-incidents')).toBeInTheDocument();
    expect(skeleton()).not.toBeInTheDocument();
    expect(mockApiGet).toHaveBeenCalledTimes(1);
  });

  it('a refresh that finds identical data leaves the picker untouched', async () => {
    renderHarness();
    await screen.findByText('#ops-incidents');

    fireEvent.click(screen.getByText('invalidate connectors'));
    // The refetch is in flight: the options stay rendered and the skeleton — which
    // the old `loading`-gated version would have shown here — never appears.
    expect(skeleton()).not.toBeInTheDocument();
    expect(screen.getByText('#ops-incidents')).toBeInTheDocument();

    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(2));
    expect(skeleton()).not.toBeInTheDocument();
    expect(screen.getByText('#ops-incidents')).toBeInTheDocument();
    expect(screen.getByText('#deploys')).toBeInTheDocument();
  });

  it('a refresh that finds a DIFFERENCE updates the options', async () => {
    renderHarness();
    await screen.findByText('#deploys');

    // A channel was added and another removed since the first load.
    mockApiGet.mockResolvedValue(
      channelsResponse([
        { id: 'C001', name: 'ops-incidents' },
        { id: 'C003', name: 'release-eng' },
      ]),
    );
    fireEvent.click(screen.getByText('invalidate connectors'));

    expect(await screen.findByText('#release-eng')).toBeInTheDocument();
    expect(screen.queryByText('#deploys')).not.toBeInTheDocument();
    // Updated silently — the skeleton is never shown for a refresh.
    expect(skeleton()).not.toBeInTheDocument();
  });

  it('does not discard an unsaved selection when a refresh lands', async () => {
    renderHarness();
    await screen.findByText('#ops-incidents');

    // The user ticks a channel but has not saved yet.
    fireEvent.click(screen.getByText('#ops-incidents'));
    expect(screen.getByText('1 of 2 channels selected')).toBeInTheDocument();

    fireEvent.click(screen.getByText('invalidate connectors'));
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(2));

    // The server still reports nothing configured; the in-progress edit survives.
    expect(screen.getByText('1 of 2 channels selected')).toBeInTheDocument();
  });

  it('does not fall back to the skeleton after a successful save', async () => {
    renderHarness();
    await screen.findByText('#ops-incidents');
    fireEvent.click(screen.getByText('#ops-incidents'));
    mockApiPatch.mockResolvedValue({
      ok: true,
      available: TWO_CHANNELS,
      selected: ['C001'],
      configured: true,
    });

    fireEvent.click(screen.getByRole('button', { name: /save channel selection/i }));

    await waitFor(() =>
      expect(screen.getByText('1 of 2 channels selected')).toBeInTheDocument(),
    );
    // The save writes its response into the cache instead of invalidating the key,
    // so the picker never blanks back to its skeleton on a successful save.
    expect(skeleton()).not.toBeInTheDocument();
    expect(screen.getByText('#ops-incidents')).toBeInTheDocument();
  });
});
