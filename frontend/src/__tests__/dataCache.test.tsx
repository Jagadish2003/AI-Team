/**
 * Unit tests for the shared data cache (src/lib/dataCache.tsx).
 *
 * Covers the behaviours the hand-rolled contexts lacked and that the reactive
 * refactor depends on: fetch-once, cross-consumer dedupe, prefix + exact
 * invalidation, optimistic setData, disabled (null key), refetch, and the inert
 * no-op path outside a provider.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor, renderHook } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';

import { DataCacheProvider, useResource, useDataCache } from '../lib/dataCache';

function wrapper({ children }: { children: React.ReactNode }) {
  return <DataCacheProvider>{children}</DataCacheProvider>;
}

describe('dataCache', () => {
  it('fetches once and exposes the resolved data', async () => {
    const fetcher = vi.fn().mockResolvedValue('v1');
    const { result } = renderHook(() => useResource('k1', fetcher), { wrapper });

    await waitFor(() => expect(result.current.data).toBe('v1'));
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('surfaces a fetch error without throwing', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('boom'));
    const { result } = renderHook(() => useResource('k-err', fetcher), { wrapper });

    await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
    expect(result.current.error?.message).toBe('boom');
    expect(result.current.data).toBeUndefined();
  });

  it('dedupes: two consumers of the same key share one in-flight fetch', async () => {
    const fetcher = vi.fn().mockResolvedValue('shared');
    function Harness() {
      const a = useResource<string>('dup', fetcher);
      const b = useResource<string>('dup', fetcher);
      return (
        <div>
          <span data-testid="a">{a.data}</span>
          <span data-testid="b">{b.data}</span>
        </div>
      );
    }
    render(<Harness />, { wrapper });

    await waitFor(() => {
      expect(screen.getByTestId('a').textContent).toBe('shared');
      expect(screen.getByTestId('b').textContent).toBe('shared');
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('invalidate(prefix) refetches every subscribed key under the prefix', async () => {
    const oppFetcher = vi.fn().mockResolvedValueOnce('opp-1').mockResolvedValueOnce('opp-2');
    const roadFetcher = vi.fn().mockResolvedValueOnce('road-1').mockResolvedValueOnce('road-2');
    function Harness() {
      const opp = useResource<string>('runs/r1/opportunities', oppFetcher);
      const road = useResource<string>('runs/r1/roadmap', roadFetcher);
      const other = useResource<string>('runs/r2/opportunities', vi.fn().mockResolvedValue('other'));
      const cache = useDataCache();
      return (
        <div>
          <span data-testid="opp">{opp.data}</span>
          <span data-testid="road">{road.data}</span>
          <span data-testid="other">{other.data}</span>
          <button onClick={() => cache.invalidate('runs/r1')}>inv</button>
        </div>
      );
    }
    render(<Harness />, { wrapper });

    await waitFor(() => expect(screen.getByTestId('opp').textContent).toBe('opp-1'));
    expect(screen.getByTestId('road').textContent).toBe('road-1');

    fireEvent.click(screen.getByRole('button', { name: 'inv' }));

    await waitFor(() => {
      expect(screen.getByTestId('opp').textContent).toBe('opp-2');
      expect(screen.getByTestId('road').textContent).toBe('road-2');
    });
    // The sibling run under a different prefix must NOT have refetched.
    expect(oppFetcher).toHaveBeenCalledTimes(2);
    expect(roadFetcher).toHaveBeenCalledTimes(2);
  });

  it('coalesces a burst of invalidations into a single refetch per key', async () => {
    const fetcher = vi.fn().mockResolvedValue('x');
    function Harness() {
      const r = useResource<string>('connectors', fetcher);
      const cache = useDataCache();
      return (
        <div>
          <span data-testid="v">{r.data}</span>
          <button
            onClick={() => {
              // three synchronous invalidations that all hit `connectors`
              cache.invalidate('connectors');
              cache.invalidate('connectors');
              cache.invalidate('connectors');
            }}
          >
            inv
          </button>
        </div>
      );
    }
    render(<Harness />, { wrapper });
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('x'));
    expect(fetcher).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'inv' }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2)); // not 4
  });

  it('setData optimistically updates all subscribers immediately', async () => {
    const fetcher = vi.fn(
      () => new Promise<string>((r) => setTimeout(() => r('server'), 50)),
    );
    function Harness() {
      const r = useResource('sd', fetcher);
      const cache = useDataCache();
      return (
        <div>
          <span data-testid="v">{r.data ?? 'none'}</span>
          <button onClick={() => cache.setData('sd', 'optimistic')}>set</button>
        </div>
      );
    }
    render(<Harness />, { wrapper });

    fireEvent.click(screen.getByRole('button', { name: 'set' }));
    // Immediate, before the fetch resolves.
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('optimistic'));
  });

  it('does not fetch when the key is null (disabled)', () => {
    const fetcher = vi.fn();
    const { result } = renderHook(() => useResource(null, fetcher), { wrapper });
    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.loading).toBe(false);
    expect(result.current.data).toBeUndefined();
  });

  it('refetch() re-runs the fetcher', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce('first').mockResolvedValueOnce('second');
    const { result } = renderHook(() => useResource('rf', fetcher), { wrapper });
    await waitFor(() => expect(result.current.data).toBe('first'));

    result.current.refetch();
    await waitFor(() => expect(result.current.data).toBe('second'));
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('keeps the cached value and does not re-render when a refetch returns equal data', async () => {
    // Two structurally identical payloads with DIFFERENT object identities —
    // exactly what a poll of an unchanged endpoint produces.
    const payload = () => [
      { id: 'salesforce', status: 'connected', metrics: [{ label: 'Cases', value: '12' }] },
      { id: 'jira', status: 'disconnected', metrics: [] },
    ];
    const fetcher = vi.fn().mockImplementation(async () => payload());

    let renders = 0;
    const seen: unknown[] = [];
    function Harness() {
      const r = useResource<unknown[]>('connectors', fetcher);
      renders += 1;
      if (r.data && seen[seen.length - 1] !== r.data) seen.push(r.data);
      const cache = useDataCache();
      return <button onClick={() => cache.invalidate('connectors')}>inv</button>;
    }
    render(<Harness />, { wrapper });

    await waitFor(() => expect(seen.length).toBe(1));
    const rendersAfterLoad = renders;

    fireEvent.click(screen.getByRole('button', { name: 'inv' }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(renders).toBeGreaterThan(rendersAfterLoad)); // loading flip
    // The refetch happened, but the value handed to consumers is the SAME
    // reference — so memos/effects keyed on it never re-run.
    expect(seen.length).toBe(1);
  });

  it('resolves loading after a refetch that returns identical data', async () => {
    const fetcher = vi.fn().mockImplementation(async () => ({ ok: true }));
    const { result } = renderHook(() => useResource<{ ok: boolean }>('eq', fetcher), { wrapper });
    await waitFor(() => expect(result.current.data).toEqual({ ok: true }));
    const first = result.current.data;

    result.current.refetch();
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
    // Not stuck in loading, and still the original reference.
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBe(first);
    expect(result.current.error).toBeNull();
  });

  it('adopts the new value when the refetched payload actually differs', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({ items: [{ id: 'a' }] })
      .mockResolvedValueOnce({ items: [{ id: 'a' }, { id: 'b' }] });
    const { result } = renderHook(() => useResource<{ items: unknown[] }>('diff', fetcher), {
      wrapper,
    });
    await waitFor(() => expect(result.current.data?.items).toHaveLength(1));

    result.current.refetch();
    await waitFor(() => expect(result.current.data?.items).toHaveLength(2));
  });

  it('treats a payload it cannot compare structurally as changed', async () => {
    // A Map has no own enumerable keys — comparing it structurally would call two
    // different Maps equal. The comparison must stay conservative and emit.
    const fetcher = vi.fn().mockImplementation(async () => new Map([['a', 1]]));
    const { result } = renderHook(() => useResource<Map<string, number>>('map', fetcher), {
      wrapper,
    });
    await waitFor(() => expect(result.current.data).toBeInstanceOf(Map));
    const first = result.current.data;

    result.current.refetch();
    await waitFor(() => expect(result.current.data).not.toBe(first));
  });

  // ── A failed load must not poison the key for the session ────────────────────
  // Regression: the background prefetch warms `runs/{id}/evidence` as soon as a
  // run STARTS, and that endpoint 404s until the run materialises the artifact.
  // The cached failure then blocked every later attempt, so the Agentforce
  // Blueprint's evidence panel stayed stuck until a full page reload.

  it('retries a failed key when a consumer mounts afterwards', async () => {
    const fetcher = vi
      .fn()
      .mockRejectedValueOnce(new Error('404 no evidence for run')) // run still going
      .mockResolvedValue(['ev-1', 'ev-2']); // materialised by the time the page opens

    function Consumer() {
      const r = useResource<string[]>('runs/r1/evidence', fetcher);
      return (
        <span data-testid="v">
          {r.error ? 'error' : r.data ? r.data.join(',') : 'none'}
        </span>
      );
    }
    function Harness() {
      const cache = useDataCache();
      const [open, setOpen] = React.useState(false);
      return (
        <div>
          <button onClick={() => cache.prefetchAsync('runs/r1/evidence', fetcher)}>
            warm
          </button>
          <button onClick={() => setOpen(true)}>open</button>
          {open && <Consumer />}
        </div>
      );
    }
    render(<Harness />, { wrapper });

    // The prefetch fires while the run is still finishing and fails.
    fireEvent.click(screen.getByRole('button', { name: 'warm' }));
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    // Navigating to the page that needs it must try again, not inherit the failure.
    fireEvent.click(screen.getByRole('button', { name: 'open' }));
    await waitFor(() => expect(screen.getByTestId('v').textContent).toBe('ev-1,ev-2'));
  });

  it('retries a failed key that is already on screen on the background tick', async () => {
    vi.useFakeTimers();
    try {
      const fetcher = vi
        .fn()
        .mockRejectedValueOnce(new Error('404 no evidence for run'))
        .mockResolvedValue(['ev-1']);

      function Consumer() {
        const r = useResource<string[]>('runs/r2/evidence', fetcher);
        return (
          <span data-testid="v">
            {r.error ? 'error' : r.data ? r.data.join(',') : 'none'}
          </span>
        );
      }
      render(<Consumer />, { wrapper });

      // First attempt fails while the user is already sitting on the page.
      await vi.advanceTimersByTimeAsync(0);
      expect(screen.getByTestId('v').textContent).toBe('error');

      // The slow revalidation tick retries it — the view recovers on its own,
      // with no navigation and no reload.
      await vi.advanceTimersByTimeAsync(61_000);
      expect(screen.getByTestId('v').textContent).toBe('ev-1');
    } finally {
      vi.useRealTimers();
    }
  });

  it('is inert (no fetch, no throw) outside a DataCacheProvider', () => {
    const fetcher = vi.fn();
    const { result } = renderHook(() => useResource('x', fetcher)); // no wrapper
    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.data).toBeUndefined();

    const { result: api } = renderHook(() => useDataCache());
    expect(() => {
      api.current.invalidate('x');
      api.current.setData('x', 1);
      api.current.clear();
    }).not.toThrow();
    expect(api.current.getData('x')).toBeUndefined();
  });
});
