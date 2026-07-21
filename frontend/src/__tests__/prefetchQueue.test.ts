/**
 * prefetchQueue — the idle-gated, concurrency-capped background scheduler that
 * keeps workspace warming from starving the foreground page after login.
 *
 * The load-bearing guarantee is the concurrency cap: no matter how many warms are
 * enqueued at once (the ~14-request login burst that used to saturate the ~6
 * HTTP/1.1 connection slots), at most 2 are ever in flight — so the page the user
 * navigated to always has connection slots free.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  enqueuePrefetch,
  clearPrefetchQueue,
  pendingPrefetchCount,
} from '../lib/prefetchQueue';

describe('prefetchQueue', () => {
  beforeEach(() => {
    clearPrefetchQueue();
  });

  it('never runs more than 2 tasks concurrently, and runs them all', async () => {
    let active = 0;
    let maxActive = 0;
    let completed = 0;

    const makeTask = () => () =>
      new Promise<void>((resolve) => {
        active += 1;
        maxActive = Math.max(maxActive, active);
        setTimeout(() => {
          active -= 1;
          completed += 1;
          resolve();
        }, 10);
      });

    for (let i = 0; i < 12; i += 1) enqueuePrefetch(makeTask());

    await vi.waitFor(() => expect(completed).toBe(12), { timeout: 4000 });
    expect(maxActive).toBeLessThanOrEqual(2);
    expect(pendingPrefetchCount()).toBe(0);
  });

  it('a failing task does not stall the queue', async () => {
    let completed = 0;
    const ok = () => () =>
      new Promise<void>((resolve) => {
        setTimeout(() => {
          completed += 1;
          resolve();
        }, 5);
      });
    const boom = () => () => Promise.reject(new Error('nope'));

    enqueuePrefetch(boom());
    enqueuePrefetch(ok());
    enqueuePrefetch(boom());
    enqueuePrefetch(ok());

    // Both ok tasks still run even though two tasks rejected.
    await vi.waitFor(() => expect(completed).toBe(2), { timeout: 4000 });
    expect(pendingPrefetchCount()).toBe(0);
  });

  it('clearPrefetchQueue drops not-yet-started work', async () => {
    // Fill both concurrency slots with slow tasks, then enqueue more and clear.
    const slow = () => () => new Promise<void>((resolve) => setTimeout(resolve, 50));
    enqueuePrefetch(slow());
    enqueuePrefetch(slow());
    enqueuePrefetch(slow());
    enqueuePrefetch(slow());
    expect(pendingPrefetchCount()).toBeGreaterThan(0);

    clearPrefetchQueue();
    expect(pendingPrefetchCount()).toBe(0);
  });
});
