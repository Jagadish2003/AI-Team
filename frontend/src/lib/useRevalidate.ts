import { useEffect, useRef } from 'react';

/**
 * Cross-user freshness for data that is NOT on the shared cache.
 *
 * An AgentIQ org is used by several people at once, so a change made by another
 * user (an approved opportunity, an edited mapping, a new run) must show up here
 * without a manual reload. Resources on the shared cache get this from
 * DataCacheProvider's background revalidation; the hand-rolled contexts
 * (AnalystReview, Normalization, …) still own their own `useState` + fetch, so
 * this hook gives them the same behaviour through the `refetch()` they already
 * expose — no refactor of their internals required.
 *
 * Revalidation is driven by:
 *   - tab focus / visibility → refetch immediately (debounced by a small minimum
 *     age so rapid focus/blur cannot hammer the API), and
 *   - a slow tick while the tab is visible → a change made elsewhere appears
 *     without the user doing anything.
 *
 * A background tab never refetches. `refetch` is read through a ref, so a caller
 * may pass an inline closure without resubscribing every render.
 */
const FOCUS_MIN_AGE_MS = 5_000;
const DEFAULT_INTERVAL_MS = 30_000;

export function useRevalidateOnFocus(
  refetch: () => void,
  opts?: { enabled?: boolean; intervalMs?: number },
): void {
  const enabled = opts?.enabled ?? true;
  const intervalMs = opts?.intervalMs ?? DEFAULT_INTERVAL_MS;

  const refetchRef = useRef(refetch);
  refetchRef.current = refetch;
  const lastRunRef = useRef(0);

  useEffect(() => {
    if (!enabled) return;

    const isVisible = () => typeof document === 'undefined' || !document.hidden;
    const maybeRefetch = (minAgeMs: number) => {
      if (!isVisible()) return;
      const now = Date.now();
      if (now - lastRunRef.current <= minAgeMs) return;
      lastRunRef.current = now;
      refetchRef.current();
    };

    const onFocus = () => maybeRefetch(FOCUS_MIN_AGE_MS);
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);

    // Paused while hidden — a background tab must not poll.
    const timer = setInterval(() => maybeRefetch(intervalMs), intervalMs);

    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
      clearInterval(timer);
    };
  }, [enabled, intervalMs]);
}
