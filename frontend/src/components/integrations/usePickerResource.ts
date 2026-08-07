/**
 * usePickerResource — the shared load rule for every Integration Hub scope picker
 * (which Slack/Teams channels, Jira projects, Confluence spaces, SharePoint sites,
 * GitHub repos, and DB schemas AgentIQ may read).
 *
 * ONE rule, in one place: **the loading skeleton belongs to the first load only.**
 *
 * Every picker used to hold its options in component-local state with `loading`
 * initialised to `true` and a mount-time fetch. That has two consequences:
 *
 *   - a REMOUNT re-shows the skeleton and re-fetches, because the local data died
 *     with the previous instance. The Integration Hub re-renders on a background
 *     connector revalidation, so an open picker visibly reloaded on a timer even
 *     though nothing about it had changed;
 *   - a refresh that SHOULD happen (a connect/disconnect invalidates the whole
 *     `connectors` cache prefix, which these keys sit under) had no way to update
 *     the options without blanking them first.
 *
 * Reading through the shared cache fixes both. The data outlives any single mount,
 * so a remount renders instantly from cache; a refetch keeps the current options on
 * screen; and the cache returns the SAME object reference when a refetch finds
 * identical data (see dataCache payloadsEqual), so a periodic refresh that changes
 * nothing costs zero re-renders — no flicker and no reset selection. A refetch that
 * does find a difference re-renders with the new options, which is the point.
 *
 * `firstLoad` is therefore gated on having no data at all, NOT on the resource's
 * `loading` flag: `loading` is also true during a foreground refetch of data we
 * already hold, which is exactly the case that must stay silent.
 */
import { apiGet } from '../../lib/apiClient';
import { useResource } from '../../lib/dataCache';

export interface PickerResource<T> {
  /** The fetched payload, or undefined until the first load resolves. */
  data: T | undefined;
  /** Fetch failure, if the load failed. */
  error: Error | null;
  /**
   * True only while the picker has never held data — the one state that earns the
   * skeleton. Any later refetch leaves this false so the options stay rendered.
   */
  firstLoad: boolean;
}

export function usePickerResource<T>(
  cacheKey: string,
  path: string,
  opts?: { enabled?: boolean },
): PickerResource<T> {
  return usePickerFetch<T>(cacheKey, () => apiGet<T>(path), opts);
}

/**
 * The same rule for a picker whose load is not a single GET — the DB scope
 * pickers read schema discovery and the saved scope together, and both belong
 * under ONE cache key so they are refreshed (and kept) as one unit.
 */
export function usePickerFetch<T>(
  cacheKey: string,
  fetcher: () => Promise<T>,
  opts?: { enabled?: boolean },
): PickerResource<T> & { refetch: () => Promise<void> } {
  const { data, error, refetch } = useResource<T>(cacheKey, fetcher, opts);
  return {
    data,
    error,
    firstLoad: (opts?.enabled ?? true) && data === undefined && error === null,
    refetch,
  };
}
