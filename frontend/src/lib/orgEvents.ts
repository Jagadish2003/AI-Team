/**
 * A tiny in-app signal: "something in this org changed — refresh what you show".
 *
 * An AgentIQ org is used by several people at once, so a change made by ANOTHER
 * user must reach every mounted view. Two things listen to this signal:
 *
 *   - DataCacheProvider  → background-revalidates every observed cache key.
 *   - useRevalidateOnFocus → refetches the hand-rolled contexts (AnalystReview,
 *     Normalization, DiscoveryRun) that own their own useState + fetch.
 *
 * Between them that covers EVERY page, so a publisher does not need to know
 * which pages are mounted or which resource changed — it just says "changed".
 *
 * The publisher is the server-sent-events stream (see orgEventStream.ts): the
 * backend emits an event whenever any user in the org mutates something. This
 * module is deliberately dependency-free so both the cache and the hook can
 * import it without any cycle.
 *
 * Listeners must never throw into the emitter — a broken listener must not stop
 * the others from refreshing, so each is called defensively.
 */
type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe to org-changed. Returns an unsubscribe function. */
export function subscribeOrgChanged(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Announce that something in the org changed; every listener refreshes. */
export function emitOrgChanged(): void {
  listeners.forEach((listener) => {
    try {
      listener();
    } catch {
      // A failing listener must not prevent the others from refreshing.
    }
  });
}
