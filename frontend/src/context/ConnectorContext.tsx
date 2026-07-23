import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { Connector, ConnectorStatus, ConnectorTier, Metric } from '../types/connector';
import { computeConfidence, Confidence } from '../utils/confidence';
import { getNextBestRecommended } from '../utils/nextBest';
import { isDiscoveryReadyConnector } from '../utils/sourceReadiness';
import { connectConnectorApi, configureSyncApi, disconnectConnectorApi } from '../services/staticApi';
import { authHeaderForToken } from '../lib/apiClient';
import { useAuthOptional } from './AuthContext';
import { useResource, useDataCache } from '../lib/dataCache';
import { cacheKeys } from '../lib/cacheKeys';

type ConnectorContextValue = {
  all: Connector[];                
  connectors: Connector[];        
  recommended: Connector[];
  standard: Connector[];
  selectedConnectorId: string | null;

  loading: boolean;
  error: string | null;
  refetch: () => void;

  recommendedConnectedCount: number;
  confidence: Confidence;
  nextBestRecommendedId: string | null;

  selectConnector: (id: string) => void;
  connectConnector: (id: string) => void;
  configureSync: (id: string) => void;
  // R18-C0 P4 / AT-566: disconnect a connector (clears the org vault credential
  // and returns the tile to its unconnected state). Rejects on failure so the
  // caller can surface an error toast.
  disconnectConnector: (id: string) => Promise<void>;
};

const Ctx = createContext<ConnectorContextValue | null>(null);

type ConnectorPayload = Partial<Connector> & Record<string, unknown>;

function isConnectorStatus(value: unknown): value is ConnectorStatus {
  return (
    value === 'connected' ||
    value === 'not_connected' ||
    value === 'disconnected' ||
    value === 'not_configured' ||
    value === 'coming_soon'
  );
}

function isConnectorTier(value: unknown): value is ConnectorTier {
  return value === 'recommended' || value === 'standard' || value === 'coming_soon';
}

function isMetric(value: unknown): value is Metric {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as Metric).label === 'string' &&
    typeof (value as Metric).value === 'string'
  );
}

function normalizeConnector(raw: ConnectorPayload): Connector | null {
  const id = typeof raw.id === 'string' && raw.id.trim() ? raw.id : null;
  if (!id) return null;

  return {
    ...raw,
    id,
    name: typeof raw.name === 'string' && raw.name.trim() ? raw.name : id,
    category: typeof raw.category === 'string' ? raw.category : 'General',
    tier: isConnectorTier(raw.tier) ? raw.tier : 'standard',
    recommendedRank: typeof raw.recommendedRank === 'number' ? raw.recommendedRank : undefined,
    status: isConnectorStatus(raw.status) ? raw.status : 'not_configured',
    configured: typeof raw.configured === 'boolean' ? raw.configured : false,
    metrics: Array.isArray(raw.metrics) ? raw.metrics.filter(isMetric) : [],
    lastSynced: typeof raw.lastSynced === 'string' ? raw.lastSynced : '—',
    reads: Array.isArray(raw.reads)
      ? raw.reads.filter((read): read is string => typeof read === 'string')
      : [],
    signalStrength: typeof raw.signalStrength === 'number' ? raw.signalStrength : 0,
    products: Array.isArray(raw.products)
      ? raw.products.filter((product): product is string => typeof product === 'string')
      : [],
    // R191-R1 T5 (AT-726): roadmap flags stamped by the backend catalog overlay.
    // A tile without a shipped ingestor comes back roadmap=true with its target
    // release; the tile renders it non-connectable "Coming — <target>".
    roadmap: raw.roadmap === true,
    roadmapTarget:
      typeof raw.roadmapTarget === 'string' ? raw.roadmapTarget : null,
  };
}

export function ConnectorProvider({ children }: { children: React.ReactNode }) {
  const [selectedConnectorId, setSelectedConnectorId] = useState<string | null>(null);
  // Errors from mutations (connect/configure) are surfaced separately from the
  // resource's own fetch error, so a failed connect still shows a message.
  const [mutationError, setMutationError] = useState<string | null>(null);

  // Use the in-session token directly from AuthContext to avoid the race where
  // this fires before AuthProvider syncs the module-level _authToken. The
  // fetcher is a closure so the shared cache signs each (re)fetch with the
  // current token; the token itself changes only across a SessionBoundary
  // remount (App.tsx), which starts a fresh cache anyway.
  const auth = useAuthOptional();
  const token = auth?.token ?? null;
  const cache = useDataCache();

  const fetchConnectors = useCallback(async (): Promise<Connector[]> => {
    const res = await fetch(
      `${(import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000'}/api/connectors`,
      { headers: authHeaderForToken(token) },
    );
    if (!res.ok) throw new Error(`Failed to load connectors (${res.status})`);
    const data = (await res.json()) as unknown;
    if (!Array.isArray(data)) throw new Error('Invalid connectors response');
    return data
      .map((connector) => normalizeConnector(connector as ConnectorPayload))
      .filter((connector): connector is Connector => Boolean(connector));
  }, [token]);

  // Shared, deduped, invalidatable resource. A mutation anywhere that calls
  // cache.invalidate(cacheKeys.connectors) — connect/configure/disconnect here,
  // or a credential/product save elsewhere — refetches this instantly.
  const resource = useResource<Connector[]>(cacheKeys.connectors, fetchConnectors);
  const all = useMemo(() => resource.data ?? [], [resource.data]);
  // Loading is true ONLY before the first load resolves — NOT during background
  // refetches. A mutation elsewhere invalidates 'connectors' and the resource
  // refetches while keeping its current data; if `loading` flipped true on every
  // refetch, IntegrationHubPage (which renders the hub only when !loading) would
  // unmount and remount the whole hub on each invalidate — a visible "page
  // refresh" that also remounts open modals. Keeping data-present refetches
  // non-blocking makes them the quiet background updates a cache should be.
  const loading = resource.data === undefined && resource.error === null;
  const error = resource.error?.message ?? mutationError;
  const refetch = useCallback(() => cache.invalidate(cacheKeys.connectors), [cache]);

  // Pick a sensible default selection once connectors load (rank 1 first).
  useEffect(() => {
    if (all.length === 0) return;
    setSelectedConnectorId((prev) => {
      if (prev) return prev;
      const topRecommended = [...all]
        .filter((d) => d.tier === 'recommended')
        .sort((a, b) => (a.recommendedRank ?? 999) - (b.recommendedRank ?? 999));
      return topRecommended.length > 0 ? topRecommended[0].id : (all[0]?.id ?? null);
    });
  }, [all]);

  const recommended = useMemo(
    () =>
      all
        .filter((c) => c.tier === 'recommended')
        .sort((a, b) => (a.recommendedRank ?? 999) - (b.recommendedRank ?? 999)),
    [all]
  );

  const standard = useMemo(
    () => all.filter((c) => c.tier !== 'recommended'),
    [all]
  );

  const recommendedConnectedCount = useMemo(
    () => recommended.filter(isDiscoveryReadyConnector).length,
    [recommended]
  );

  const confidence = useMemo(
    () => computeConfidence(recommendedConnectedCount),
    [recommendedConnectedCount]
  );

  const nextBestRecommendedId = useMemo(
    () => getNextBestRecommended(recommended),[recommended]
  );

  const selectConnector = useCallback((id: string) => {
    setSelectedConnectorId(id);
  },[]);

  // Connecting/configuring/disconnecting a connector can change its tile state,
  // the network-profile auth-capability gating, AND the licence systems-used
  // count ("one connected entity = one system"), so all three keys are
  // invalidated → tiles, the detail panel, gating, and the usage strip all
  // refresh live (no reload). The licence key is what keeps the strip correct
  // now that it reads from the cache rather than re-fetching per connector change.
  const invalidateConnectorState = useCallback(() => {
    cache.invalidate(cacheKeys.connectors);
    cache.invalidate(cacheKeys.networkProfile);
    cache.invalidate(cacheKeys.license);
  }, [cache]);

  const connectConnector = useCallback(async (id: string) => {
    setMutationError(null);
    try {
      await connectConnectorApi(id);
      invalidateConnectorState();
    } catch (e: any) {
      setMutationError(e?.message ?? 'Failed to connect');
    }
  }, [invalidateConnectorState]);

  const configureSync = useCallback(async (id: string) => {
    setMutationError(null);
    try {
      await configureSyncApi(id);
      invalidateConnectorState();
    } catch (e: any) {
      setMutationError(e?.message ?? 'Failed to configure sync');
    }
  }, [invalidateConnectorState]);

  const disconnectConnector = useCallback(async (id: string) => {
    // Let the caller handle success/error toasts, but always invalidate so the
    // tile reflects the cleared credential. Re-throw so the confirm dialog can
    // keep the user informed if the disconnect failed.
    try {
      await disconnectConnectorApi(id);
    } finally {
      invalidateConnectorState();
    }
  }, [invalidateConnectorState]);

  const value: ConnectorContextValue = useMemo(() => ({
    all,                    
    connectors: all,        
    recommended,
    standard,
    selectedConnectorId,
    loading,
    error,
    refetch,
    recommendedConnectedCount,
    confidence,
    nextBestRecommendedId,
    selectConnector,
    connectConnector,
    configureSync,
    disconnectConnector
  }),[
    all, recommended, standard, selectedConnectorId,
    loading, error, recommendedConnectedCount, confidence, nextBestRecommendedId,
    selectConnector, connectConnector, configureSync, disconnectConnector, refetch
  ]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useConnectorContext() {
  const ctx = useContext(Ctx);
  if (!ctx)
    throw new Error('useConnectorContext must be used inside ConnectorProvider');
  return ctx;
}
