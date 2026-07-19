import { useEffect } from 'react';
import { useAuthOptional } from '../../context/AuthContext';
import { useRunContext } from '../../context/RunContext';
import { useAnalystReviewContext } from '../../context/AnalystReviewContext';
import { useConnectorContext } from '../../context/ConnectorContext';
import { fetchJwtBearerCredentialStatus, fetchTokenStatus } from '../../services/staticApi';
import { useDataCache } from '../../lib/dataCache';
import { enqueuePrefetch } from '../../lib/prefetchQueue';
import { cacheKeys } from '../../lib/cacheKeys';
import { apiGet } from '../../lib/apiClient';
import { fetchLicenseLimits } from '../../api/licenseApi';
import {
  fetchAttentionHealth,
  fetchConnectorHealth,
  fetchContentHealth,
  fetchPackHealth,
  fetchRunHealth,
} from '../../api/runHealthApi';
import { fetchRunExecutiveReport, fetchRunRoadmap } from '../../api/runScopedS9S10Api';
import { fetchOppEnrichment, fetchRunEnrichment } from '../../api/enrichmentApi';
import { fetchEvidence } from '../../api/runApi';
import { fetchBlueprint } from '../../api/blueprintApi';
import { fetchIndustries, fetchTemplates } from '../../api/stackBuilderApi';

/**
 * Headless: warms this user's whole workspace into the shared cache after login,
 * WITHOUT starving the page the user is actually looking at.
 *
 * Every page's data is fetched up front and kept in the cache (which lives at the
 * app root), so navigating anywhere afterwards renders from cache — no refetch,
 * no skeleton, no waiting.
 *
 * The load-bearing rule (why this used to make login SLOW): the browser allows
 * only ~6 connections per origin over HTTP/1.1. Firing the whole workspace's
 * requests at once — this component previously fired ~14 synchronously plus more
 * on a timer — saturates that pool, so the request for the page you just
 * navigated to queues behind the entire warm. The fix: every warm here goes
 * through `enqueuePrefetch`, an idle-gated queue capped at 2 in-flight
 * (prefetchQueue.ts). A page that mounts fetches its OWN keys directly through
 * `useResource` (NOT this queue) at full priority, and `prefetchAsync` dedupes,
 * so the foreground page always wins the connection pool and the warm fills in
 * quietly behind it. Freshness is unaffected — the cache still revalidates on
 * focus / interval, and mutations still invalidate their keys.
 *
 * Warming is idempotent: `prefetchAsync` only fetches a key with no data yet, so
 * re-running an effect never re-fetches what is already cached and it never
 * fights the pages, which read the very same keys.
 */
const API_BASE =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000';

export default function PrefetchWorkspaceData() {
  const auth = useAuthOptional();
  const token = auth?.token ?? null;
  const cache = useDataCache();
  const { runId } = useRunContext();
  const { opportunities } = useAnalystReviewContext();
  const { all: connectors } = useConnectorContext();

  // Every warm is enqueued (idle-gated, concurrency-capped) rather than fired
  // directly, so it can never contend with the foreground page's own fetches.
  const warm = (key: string, fetcher: () => Promise<unknown>) =>
    enqueuePrefetch(() => cache.prefetchAsync(key, fetcher));

  // ── Org-scoped: everything that is not tied to a run ──────────────────────
  // (Connectors and the network profile are already primed by their providers
  // at app mount, so they are not repeated here.)
  useEffect(() => {
    if (!token) return;
    warm(cacheKeys.license, fetchLicenseLimits);
    warm(cacheKeys.connectorProducts, () => apiGet('/api/connectors/salesforce/products'));
    warm(cacheKeys.workspaceCatalog, () =>
      apiGet('/api/integration-hub/workspace-catalog'),
    );
    warm(cacheKeys.stackBuilderRegistry, async () => {
      const [industries, templates] = await Promise.all([
        fetchIndustries(API_BASE, token),
        fetchTemplates(API_BASE, token),
      ]);
      return { industries, templates };
    });
    // Run Health's five panels.
    warm(cacheKeys.runHealthConnectors, fetchConnectorHealth);
    warm(cacheKeys.runHealthRuns, fetchRunHealth);
    warm(cacheKeys.runHealthContent, fetchContentHealth);
    warm(cacheKeys.runHealthPacks, fetchPackHealth);
    warm(cacheKeys.runHealthAttention, fetchAttentionHealth);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // ── Per-connector: the Integration Hub's status cards ─────────────────────
  // Token + JWT-bearer status for each CONNECTED connector. The queue paces these
  // (cap 2, idle-gated), so no manual stagger is needed any more.
  useEffect(() => {
    if (!token || connectors.length === 0) return;
    for (const connector of connectors) {
      if (connector.status !== 'connected') continue;
      warm(cacheKeys.connectorTokenStatus(connector.id), () => fetchTokenStatus(connector.id));
      warm(cacheKeys.connectorJwtStatus(connector.id), () =>
        fetchJwtBearerCredentialStatus(connector.id),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, connectors]);

  // ── Run-scoped: the active run's derived artifacts ────────────────────────
  useEffect(() => {
    if (!token || !runId) return;
    warm(cacheKeys.runRoadmap(runId), () => fetchRunRoadmap(runId));
    warm(cacheKeys.runExecutiveReport(runId), () => fetchRunExecutiveReport(runId));
    warm(cacheKeys.runEnrichment(runId), () => fetchRunEnrichment(runId));
    warm(cacheKeys.runEvidence(runId), () => fetchEvidence(runId));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, runId]);

  // ── Per-opportunity: every opportunity's blueprint + AI analysis ──────────
  // This is what makes Opportunity Review and the Agentforce Blueprint instant
  // for ANY opportunity, not just the one selected first. The queue keeps the
  // fan-out (2 requests × N opportunities) strictly in the background.
  useEffect(() => {
    if (!token || !runId || opportunities.length === 0) return;
    for (const opp of opportunities) {
      warm(cacheKeys.runBlueprint(runId, opp.id), () => fetchBlueprint(runId, opp.id));
      warm(cacheKeys.runOppEnrichment(runId, opp.id), () => fetchOppEnrichment(runId, opp.id));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, runId, opportunities]);

  return null;
}
