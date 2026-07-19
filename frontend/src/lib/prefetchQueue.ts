/**
 * A tiny idle-gated, concurrency-capped scheduler for BACKGROUND prefetch work.
 *
 * Warming the workspace after login must never compete with the page the user is
 * actually looking at. The browser allows only ~6 connections per origin over
 * HTTP/1.1, so firing the whole workspace's requests at once starves the
 * foreground page's fetch behind them (the "huge delay after login" pathology).
 * This queue fixes that with two guarantees:
 *
 *   - CONCURRENCY CAP: at most `MAX_CONCURRENT` prefetches are in flight at once,
 *     leaving the rest of the connection pool free for foreground requests. A page
 *     that mounts and fetches its OWN data does NOT go through this queue — it
 *     fetches immediately at full priority — so the foreground always wins.
 *   - IDLE GATING: the next task starts only when the browser is idle
 *     (`requestIdleCallback`), so a warm never runs while the main thread is busy
 *     rendering/handling the work the user is waiting on.
 *
 * Tasks are thunks returning a promise; `enqueuePrefetch` is fire-and-forget and
 * FIFO, so a caller can enqueue higher-value work first. De-duplication is the
 * cache's job (`prefetchAsync` no-ops a key that is already warm/in-flight), so
 * this stays a dumb scheduler with no knowledge of what it is fetching.
 */

type PrefetchTask = () => Promise<unknown>;

/** Keep well under the ~6 HTTP/1.1 slots so foreground requests are never starved. */
const MAX_CONCURRENT = 2;
/** Give up waiting for idle after this long so a busy tab still warms eventually. */
const IDLE_TIMEOUT_MS = 2_000;

const queue: PrefetchTask[] = [];
let active = 0;

type IdleScheduler = (cb: () => void) => void;

// requestIdleCallback where available; a short timeout everywhere else (Safari,
// jsdom/test env) so behaviour is identical, just without true idle detection.
const scheduleIdle: IdleScheduler = (() => {
  const g = globalThis as unknown as {
    requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
  };
  if (typeof g.requestIdleCallback === 'function') {
    return (cb: () => void) => {
      g.requestIdleCallback!(cb, { timeout: IDLE_TIMEOUT_MS });
    };
  }
  return (cb: () => void) => {
    setTimeout(cb, 1);
  };
})();

function pump(): void {
  // Reserve a slot per scheduled task (active counts scheduled-but-unfinished
  // work), so concurrency is capped even though the task itself starts on idle.
  while (active < MAX_CONCURRENT && queue.length > 0) {
    const task = queue.shift()!;
    active += 1;
    scheduleIdle(() => {
      Promise.resolve()
        .then(task)
        .catch(() => {
          // A prefetch failure is silent — the page will fetch the key on demand.
        })
        .finally(() => {
          active -= 1;
          pump();
        });
    });
  }
}

/** Enqueue a background prefetch. Fire-and-forget, FIFO, capped + idle-gated. */
export function enqueuePrefetch(task: PrefetchTask): void {
  queue.push(task);
  pump();
}

/** Drop everything not yet started (e.g. logout / tests). In-flight tasks finish. */
export function clearPrefetchQueue(): void {
  queue.length = 0;
}

/** Pending (not-yet-started) task count. Exposed for tests/observability. */
export function pendingPrefetchCount(): number {
  return queue.length;
}
