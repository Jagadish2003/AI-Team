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
}

const EMPTY_SNAPSHOT: Snapshot<unknown> = { data: undefined, loading: false, error: null };

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

  /** Register the latest fetcher for a key and kick off the first fetch if idle. */
  prime(key: string, fetcher: () => Promise<unknown>): void {
    const e = this.ensure(key);
    e.fetcher = fetcher;
    if (e.data === undefined && e.error === null && !e.promise && !e.loading) {
      this.run(key);
    }
  }

  run(key: string): void {
    const e = this.map.get(key);
    if (!e || !e.fetcher) return;
    const fetcher = e.fetcher;
    e.loading = true;
    e.error = null;
    this.emit(e);
    const p = fetcher().then(
      (data) => {
        if (e.promise !== p) return; // superseded by a newer run
        e.data = data;
        e.error = null;
        e.loading = false;
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
  return (
    <DataCacheContext.Provider value={storeRef.current}>{children}</DataCacheContext.Provider>
  );
}

const NOOP = () => {};
const INERT_API: DataCacheApi = {
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
  opts?: { enabled?: boolean },
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
    store!.prime(key!, stableFetcherRef.current!);
  }, [enabled, key, store]);

  const refetch = useCallback(() => {
    if (enabled) store!.run(key!);
  }, [enabled, key, store]);

  return { data: snap.data, loading: snap.loading, error: snap.error, refetch };
}
