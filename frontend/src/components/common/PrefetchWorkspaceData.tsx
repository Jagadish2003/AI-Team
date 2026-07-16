import { useEffect } from 'react';
import { useAuthOptional } from '../../context/AuthContext';
import { useRunContext } from '../../context/RunContext';
import { useAnalystReviewContext } from '../../context/AnalystReviewContext';
import { useConnectorContext } from '../../context/ConnectorContext';
import { fetchJwtBearerCredentialStatus, fetchTokenStatus } from '../../services/staticApi';
import { useDataCache } from '../../lib/dataCache';
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
 * Headless: warms this user's whole workspace into the shared cache after login.
 *
 * Every page's data is fetched up front and kept in the cache (which lives at the
 * app root), so navigating anywhere afterwards renders from cache — no refetch,
 * no skeleton, no waiting. Freshness is unaffected: the cache still revalidates
 * in the background on focus / the org change stream, and mutations still
 * invalidate their keys, so warmed data can never go stale silently.
 *
 * prefetch() only fetches a key that has NO data yet, so this is idempotent —
 * re-running it never re-fetches what is already cached, and it never fights the
 * pages, which read the very same keys.
 *
 * Per-opportunity warming is DELIBERATELY staggered. A run can hold many
 * opportunities, and firing a blueprint + enrichment request for each at once
 * would exceed the browser's ~6 connections per origin and starve the page the
 * user is actually looking at — the pathology this prefetch exists to prevent.
 * Spacing them keeps warming strictly in the background.
 */
const PREFETCH_STAGGER_MS = 200;
const API_BASE =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000';

export default function PrefetchWorkspaceData() {
  const auth = useAuthOptional();
  const token = auth?.token ?? null;
  const cache = useDataCache();
  const { runId } = useRunContext();
  const { opportunities } = useAnalystReviewContext();
  const { all: connectors } = useConnectorContext();

  // ── Org-scoped: everything that is not tied to a run ──────────────────────
  // (Connectors and the network profile are already primed by their providers
  // at app mount, so they are not repeated here.)
  useEffect(() => {
    if (!token) return;
    cache.prefetch(cacheKeys.license, fetchLicenseLimits);
    cache.prefetch(cacheKeys.connectorProducts, () =>
      apiGet('/api/connectors/salesforce/products'),
    );
    cache.prefetch(cacheKeys.workspaceCatalog, () =>
      apiGet('/api/integration-hub/workspace-catalog'),
    );
    cache.prefetch(cacheKeys.stackBuilderRegistry, async () => {
      const [industries, templates] = await Promise.all([
        fetchIndustries(API_BASE, token),
        fetchTemplates(API_BASE, token),
      ]);
      return { industries, templates };
    });
    // Run Health's five panels.
    cache.prefetch(cacheKeys.runHealthConnectors, fetchConnectorHealth);
    cache.prefetch(cacheKeys.runHealthRuns, fetchRunHealth);
    cache.prefetch(cacheKeys.runHealthContent, fetchContentHealth);
    cache.prefetch(cacheKeys.runHealthPacks, fetchPackHealth);
    cache.prefetch(cacheKeys.runHealthAttention, fetchAttentionHealth);
  }, [token, cache]);

  // ── Per-connector: the Integration Hub's status cards ─────────────────────
  // Token status for each CONNECTED connector, and the JWT-bearer credential
  // status. Staggered for the same reason as the per-opportunity warming below.
  useEffect(() => {
    if (!token || connectors.length === 0) return;
    const connected = connectors.filter((c) => c.status === 'connected');
    if (connected.length === 0) return;
    let cancelled = false;
    (async () => {
      for (const connector of connected) {
        if (cancelled) return;
        cache.prefetch(cacheKeys.connectorTokenStatus(connector.id), () =>
          fetchTokenStatus(connector.id),
        );
        cache.prefetch(cacheKeys.connectorJwtStatus(connector.id), () =>
          fetchJwtBearerCredentialStatus(connector.id),
        );
        await new Promise((resolve) => setTimeout(resolve, PREFETCH_STAGGER_MS));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, connectors, cache]);

  // ── Run-scoped: the active run's derived artifacts ────────────────────────
  useEffect(() => {
    if (!token || !runId) return;
    cache.prefetch(cacheKeys.runRoadmap(runId), () => fetchRunRoadmap(runId));
    cache.prefetch(cacheKeys.runExecutiveReport(runId), () => fetchRunExecutiveReport(runId));
    cache.prefetch(cacheKeys.runEnrichment(runId), () => fetchRunEnrichment(runId));
    cache.prefetch(cacheKeys.runEvidence(runId), () => fetchEvidence(runId));
  }, [token, runId, cache]);

  // ── Per-opportunity: every opportunity's blueprint + AI analysis ──────────
  // This is what makes Opportunity Review and the Agentforce Blueprint instant
  // for ANY opportunity, not just the one that happens to be selected first.
  useEffect(() => {
    if (!token || !runId || opportunities.length === 0) return;
    let cancelled = false;
    (async () => {
      for (const opp of opportunities) {
        if (cancelled) return;
        cache.prefetch(cacheKeys.runBlueprint(runId, opp.id), () =>
          fetchBlueprint(runId, opp.id),
        );
        cache.prefetch(cacheKeys.runOppEnrichment(runId, opp.id), () =>
          fetchOppEnrichment(runId, opp.id),
        );
        // Yield between opportunities — see the stagger note above.
        await new Promise((resolve) => setTimeout(resolve, PREFETCH_STAGGER_MS));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, runId, opportunities, cache]);

  return null;
}
