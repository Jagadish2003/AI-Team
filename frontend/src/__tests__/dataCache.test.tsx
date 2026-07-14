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
