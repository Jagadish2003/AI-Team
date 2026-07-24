/**
 * dataCache — a minimal shared data cache + cross-component invalidation.
 *
 * Why this exists (R18 reactive-data refactor):
 *   The app hand-rolls "fetch into local state + a fetchCount bump to refetch"
 *   in ~9 contexts and many components, so a mutation in one place never
 *   refreshes a view that reads the same resource elsewhere — the user has to
 *   reload the page. This module formalises that pattern with two things the
 *   hand-rolled version lacks: request DEDUPE (one in-flight fetch shared by all
 *   consumers of a key) and cross-boundary INVALIDATION (a mutation invalidates a
 *   resource key and every mounted consumer refetches/re-renders instantly).
 *
 * Deliberate constraints:
 *   - The cache NEVER fetches on its own — the caller passes a fetcher closure.
 *     This preserves the existing contexts' careful use of `authHeaderForToken`
 *     to dodge the first-mount token race and the global 401 interceptor.
 *   - Used OUTSIDE a <DataCacheProvider>, useResource/useDataCache degrade to an
 *     inert no-op (mirrors useNetworkProfileOptional), so isolated component
 *     tests need no provider wrapper.
 *   - Per-user isolation is handled by App.tsx keying the provider subtree on the
 *     JWT (SessionBoundary): a user change remounts DataCacheProvider, so its Map
 *     is discarded. `clear()` is exposed as a belt-and-braces logout path.
 *
 * Keys are hierarchical, "/"-delimited strings (see cacheKeys.ts) so prefix
 * invalidation is meaningful: invalidate('runs/' + runId) refreshes that run's
 * opportunities + roadmap + blueprint together.
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useSyncExternalStore,
} from 'react';

export interface ResourceState<T> {
  data: T | undefined;
  loading: boolean;
  error: Error | null;
  /** Force a refetch of this key (shared with every other consumer). */
  refetch: () => void;
}

export interface DataCacheApi {
  /**
   * Warm a key without subscribing to it: fetches only if the key has no data yet
   * (so it is safe to call repeatedly and never re-fetches what is cached), and
   * returns a promise that resolves when the warm settles — immediately if the key
   * is already cached. The promise lets a background scheduler (see
   * prefetchQueue.ts) CAP how many warms are in flight at once, so login-time
   * warming never starves the foreground page's own fetches. Used to pre-load a
   * workspace after login so later navigation renders from cache.
   */
  prefetchAsync: <T>(key: string, fetcher: () => Promise<T>) => Promise<void>;
  /** Refetch (or drop, if unobserved) every key equal to `prefix` or under `prefix/`. Coalesced. */
  invalidate: (prefix: string) => void;
  /** Refetch (or drop, if unobserved) exactly one key. */
  invalidateExact: (key: string) => void;
  /** Read the currently-cached value for a key without subscribing. */
  getData: <T>(key: string) => T | undefined;
  /** Optimistically write a value for a key and broadcast to subscribers. */
  setData: <T>(key: string, next: T | ((prev: T | undefined) => T)) => void;
  /** Drop the entire cache (logout). */
  clear: () => void;
}

interface Snapshot<T> {
  data: T | undefined;
  loading: boolean;
  error: Error | null;
}

interface Entry {
  data: unknown;
  loading: boolean;
  error: Error | null;
  promise: Promise<void> | null;
  fetcher: (() => Promise<unknown>) | null;
  subscribers: Set<() => void>;
  /** Stable object returned to consumers; replaced (new ref) only on change. */
  snapshot: Snapshot<unknown>;
  /** Epoch ms of the last successful fetch (0 = never). Drives stale-while-revalidate. */
  fetchedAt: number;
}

const EMPTY_SNAPSHOT: Snapshot<unknown> = { data: undefined, loading: false, error: null };

/**
 * Default staleness window for cached resources. Within this window a (re)mount
 * serves the cached value with no refetch; past it, the cached value is served
 * immediately AND refreshed in the background (stale-while-revalidate). Tunable
 * per call via useResource's `staleTime` opt; <= 0 disables revalidation.
 */
const DEFAULT_STALE_MS = 30_000;

/**
 * Cross-user freshness (an org is used by several people at once).
 *
 * A resource changed by ANOTHER user must show up here without a manual reload.
 * Both knobs below drive BACKGROUND revalidation of currently-observed keys —
 * the cached value stays on screen and is silently replaced when the fresh one
 * lands, so there is no spinner and no flicker.
 *
 * - FOCUS: returning to the tab/window revalidates immediately (debounced by a
 *   small minimum age so rapid focus/blur does not hammer the API).
 * - INTERVAL: while the tab is visible, observed keys are refreshed on a slow
 *   tick so a change made elsewhere appears without the user doing anything.
 *
 * A push feed could later make this instant; these make it correct today.
 */
const FOCUS_REVALIDATE_MIN_AGE_MS = 5_000;
const BACKGROUND_REVALIDATE_INTERVAL_MS = 30_000;

/**
 * The cache store. One instance per DataCacheProvider (i.e. per user session,
 * because the provider is remounted on auth change).
 */
class DataCacheStore {
  private map = new Map<string, Entry>();
  private pendingPrefixes = new Set<string>();
  private flushScheduled = false;

  private ensure(key: string): Entry {
    let e = this.map.get(key);
    if (!e) {
      e = {
        data: undefined,
        loading: false,
        error: null,
        promise: null,
        fetcher: null,
        subscribers: new Set(),
        snapshot: EMPTY_SNAPSHOT,
        fetchedAt: 0,
      };
      this.map.set(key, e);
    }
    return e;
  }

  private emit(e: Entry): void {
    e.snapshot = { data: e.data, loading: e.loading, error: e.error };
    e.subscribers.forEach((cb) => cb());
  }

  subscribe(key: string, cb: () => void): () => void {
    const e = this.ensure(key);
    e.subscribers.add(cb);
    return () => {
      e.subscribers.delete(cb);
    };
  }

  /** Read-only snapshot for useSyncExternalStore — never mutates the map. */
  peek(key: string): Snapshot<unknown> {
    return this.map.get(key)?.snapshot ?? EMPTY_SNAPSHOT;
  }

  /**
   * Register the latest fetcher for a key and either kick off the first fetch
   * (no data yet) or, if the cached data is older than `staleTime`, refresh it in
   * the background (stale-while-revalidate). `staleTime <= 0` disables the
   * revalidation (fetch-once). An errored entry is left for an explicit refetch.
   */
  prime(key: string, fetcher: () => Promise<unknown>, staleTime = DEFAULT_STALE_MS): void {
    const e = this.ensure(key);
    e.fetcher = fetcher;
    if (e.data === undefined && e.error === null && !e.promise && !e.loading) {
      this.run(key); // first load for this key
      return;
    }
    if (
      e.data !== undefined &&
      !e.promise &&
      staleTime > 0 &&
      Date.now() - e.fetchedAt > staleTime
    ) {
      this.run(key, true); // background revalidate — keep showing the stale value
    }
  }

  run(key: string, background = false): void {
    const e = this.map.get(key);
    if (!e || !e.fetcher) return;
    const fetcher = e.fetcher;
    // A background (stale-while-revalidate) refresh keeps the current value and
    // does NOT flip loading, so consumers never flash a spinner while fresh data
    // is fetched. A foreground run (first load / invalidate) shows loading.
    if (!(background && e.data !== undefined)) {
      e.loading = true;
      e.error = null;
      this.emit(e);
    }
    const p = fetcher().then(
      (data) => {
        if (e.promise !== p) return; // superseded by a newer run
        e.data = data;
        e.error = null;
        e.loading = false;
        e.fetchedAt = Date.now();
        e.promise = null;
        this.emit(e);
      },
      (err) => {
        if (e.promise !== p) return;
        e.error = err instanceof Error ? err : new Error(String(err));
        e.loading = false;
        e.promise = null;
        this.emit(e);
      },
    );
    e.promise = p;
  }

  /**
   * Prime a key and return a promise that resolves once its fetch settles (or at
   * once if it already holds data). `staleTime = 0` means "warm only" — an already
   * cached key never refetches — so this is safe to call repeatedly. Never rejects:
   * a warm failure is swallowed (the observing page surfaces its own error later).
   */
  async primeAsync(key: string, fetcher: () => Promise<unknown>): Promise<void> {
    this.prime(key, fetcher, 0);
    const e = this.map.get(key);
    if (e?.promise) {
      try {
        await e.promise;
      } catch {
        /* a warm failure is not the warmer's to surface */
      }
    }
  }

  getData<T>(key: string): T | undefined {
    return this.map.get(key)?.data as T | undefined;
  }

  setData<T>(key: string, next: T | ((prev: T | undefined) => T)): void {
    const e = this.ensure(key);
    const value =
      typeof next === 'function'
        ? (next as (prev: T | undefined) => T)(e.data as T | undefined)
        : next;
    e.data = value;
    e.error = null;
    this.emit(e);
  }

  invalidateExact(key: string): void {
    const e = this.map.get(key);
    if (!e) return;
    if (e.subscribers.size > 0) this.run(key);
    else this.map.delete(key);
  }

  invalidate(prefix: string): void {
    // Coalesce a synchronous burst (e.g. a mutation invalidating 3 keys) into a
    // single flush so each affected key refetches at most once.
    this.pendingPrefixes.add(prefix);
    if (!this.flushScheduled) {
      this.flushScheduled = true;
      queueMicrotask(() => this.flush());
    }
  }

  private matches(key: string, prefix: string): boolean {
    return key === prefix || key.startsWith(prefix + '/');
  }

  private flush(): void {
    const prefixes = [...this.pendingPrefixes];
    this.pendingPrefixes.clear();
    this.flushScheduled = false;
    for (const [key, e] of [...this.map.entries()]) {
      if (!prefixes.some((p) => this.matches(key, p))) continue;
      if (e.subscribers.size > 0) this.run(key);
      else this.map.delete(key); // unobserved → drop so its next mount refetches
    }
  }

  /**
   * Background-revalidate every OBSERVED key whose data is older than maxAgeMs.
   *
   * This is how another user's change reaches this client: the mounted consumers
   * of a shared org resource silently refetch and re-render with the new value.
   * Unobserved keys are skipped (nothing is showing them; they revalidate on
   * their next mount via prime()), as are first-loads and in-flight fetches.
   */
  revalidateObserved(maxAgeMs = 0): void {
    const now = Date.now();
    for (const [key, e] of this.map.entries()) {
      if (e.subscribers.size === 0) continue; // nobody is displaying it
      if (e.data === undefined || e.promise) continue; // first load / in-flight
      if (now - e.fetchedAt <= maxAgeMs) continue; // still fresh enough
      this.run(key, true); // background → keeps the current value, no spinner
    }
  }

  clear(): void {
    this.map.clear();
  }
}

const DataCacheContext = createContext<DataCacheStore | null>(null);

export function DataCacheProvider({ children }: { children: React.ReactNode }) {
  // One store for the life of this provider instance. The provider is remounted
  // per user (App.tsx SessionBoundary), so the store is naturally per-session.
  const storeRef = useRef<DataCacheStore>();
  if (!storeRef.current) storeRef.current = new DataCacheStore();
  const store = storeRef.current;

  // Keep this client in step with the rest of the org (several people use one
  // workspace at a time): background-revalidate what is on screen when the tab
  // regains focus, and on a slow tick while it stays visible. Both are silent —
  // the current value stays rendered until the fresh one arrives.
  useEffect(() => {
    const isVisible = () => typeof document === 'undefined' || !document.hidden;

    const onFocus = () => {
      if (isVisible()) store.revalidateObserved(FOCUS_REVALIDATE_MIN_AGE_MS);
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onFocus);

    // Paused while hidden — a background tab must not poll.
    const timer = setInterval(() => {
      if (isVisible()) store.revalidateObserved(BACKGROUND_REVALIDATE_INTERVAL_MS);
    }, BACKGROUND_REVALIDATE_INTERVAL_MS);

    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onFocus);
      clearInterval(timer);
    };
  }, [store]);

  return <DataCacheContext.Provider value={store}>{children}</DataCacheContext.Provider>;
}

const NOOP = () => {};
const INERT_API: DataCacheApi = {
  prefetchAsync: () => Promise.resolve(),
  invalidate: NOOP,
  invalidateExact: NOOP,
  getData: () => undefined,
  setData: NOOP,
  clear: NOOP,
};

/**
 * Imperative cache handle for mutations: invalidate keys / read / optimistic
 * write / clear. Inert (no-op) outside a DataCacheProvider.
 */
export function useDataCache(): DataCacheApi {
  const store = useContext(DataCacheContext);
  return useMemo<DataCacheApi>(() => {
    if (!store) return INERT_API;
    return {
      // primeAsync() fetches only when the key holds no data, so a repeated warm
      // is a no-op rather than a re-fetch, and resolves once the fetch settles so
      // the prefetch queue can bound concurrency.
      prefetchAsync: (key, fetcher) =>
        store.primeAsync(key, fetcher as () => Promise<unknown>),
      invalidate: (prefix) => store.invalidate(prefix),
      invalidateExact: (key) => store.invalidateExact(key),
      getData: (key) => store.getData(key),
      setData: (key, next) => store.setData(key, next),
      clear: () => store.clear(),
    };
  }, [store]);
}

/**
 * Subscribe to a cached resource. Fetches once per key (deduped across all
 * consumers), re-renders on change, and refetches on invalidate.
 *
 * @param key      cache key, or null to disable (e.g. a run-scoped resource with
 *                 no runId yet). Also disabled via opts.enabled === false.
 * @param fetcher  closure returning the resource; may capture the auth token.
 */
export function useResource<T>(
  key: string | null,
  fetcher: () => Promise<T>,
  opts?: { enabled?: boolean; staleTime?: number },
): ResourceState<T> {
  const store = useContext(DataCacheContext);
  const enabled = (opts?.enabled ?? true) && key !== null && store !== null;

  // Keep the latest fetcher without using it as an effect dep (it is a new
  // closure each render). A stable wrapper defers to the latest closure so
  // invalidate-driven refetches always use current props/token.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const stableFetcherRef = useRef<() => Promise<unknown>>();
  if (!stableFetcherRef.current) {
    stableFetcherRef.current = () => fetcherRef.current();
  }

  const subscribe = useCallback(
    (cb: () => void) => {
      if (!enabled) return NOOP;
      return store!.subscribe(key!, cb);
    },
    [enabled, key, store],
  );

  const getSnapshot = useCallback(() => {
    if (!enabled) return EMPTY_SNAPSHOT;
    return store!.peek(key!);
  }, [enabled, key, store]);

  const snap = useSyncExternalStore(subscribe, getSnapshot, getSnapshot) as Snapshot<T>;

  useEffect(() => {
    if (!enabled) return;
    store!.prime(key!, stableFetcherRef.current!, opts?.staleTime);
  }, [enabled, key, store, opts?.staleTime]);

  const refetch = useCallback(() => {
    if (enabled) store!.run(key!);
  }, [enabled, key, store]);

  // An enabled key with no data and no error yet IS loading — either prime()
  // has not run (the effect above fires after the first render, so the store has
  // no entry and peek() returns the empty snapshot) or its fetch is in flight.
  // Reporting false there is indistinguishable from "loaded and empty", so a
  // consumer renders its empty/absent state for a tick before the skeleton: the
  // licence strip vanished (reserving no space, then shoving the page down) and
  // the product picker flashed an unselected form. A DISABLED key is never
  // loading, and a cached key reports false immediately — so prefetched data
  // still renders instantly with no skeleton.
  const loading =
    enabled && snap.data === undefined && snap.error === null ? true : snap.loading;

  return { data: snap.data, loading, error: snap.error, refetch };
}
