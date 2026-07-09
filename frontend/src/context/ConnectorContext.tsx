import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { Connector, ConnectorStatus, ConnectorTier, Metric } from '../types/connector';
import { computeConfidence, Confidence } from '../utils/confidence';
import { getNextBestRecommended } from '../utils/nextBest';
import { isDiscoveryReadyConnector } from '../utils/sourceReadiness';
import { connectConnectorApi, configureSyncApi, disconnectConnectorApi } from '../services/staticApi';
import { authHeaderForToken } from '../lib/apiClient';
import { useAuthOptional } from './AuthContext';

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
  };
}

export function ConnectorProvider({ children }: { children: React.ReactNode }) {
  const [all, setAll] = useState<Connector[]>([]);
  const[selectedConnectorId, setSelectedConnectorId] = useState<string | null>(null);

  const[loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState<number>(0);

  // Use the in-session token directly from AuthContext to avoid the race where
  // ConnectorProvider's effect fires before AuthProvider's effect syncs the
  // module-level _authToken. This mirrors the same pattern used in RunContext.
  const auth = useAuthOptional();
  const token = auth?.token ?? null;

  const refetch = useCallback(() => setFetchCount((c) => c + 1),[]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    // Pass the in-session token explicitly so the request is signed with the
    // right JWT even on the first mount (before setAuthToken effect has run).
    fetch(`${(import.meta.env.VITE_API_BASE_URL as string | undefined) ?? 'http://localhost:8000'}/api/connectors`, {
      headers: authHeaderForToken(token),
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`Failed to load connectors (${res.status})`);
        return res.json() as Promise<unknown>;
      })
      .then((data) => {
        if (!alive) return;
        if (!Array.isArray(data)) {
          throw new Error('Invalid connectors response');
        }

        const normalized = data
          .map((connector) => normalizeConnector(connector as ConnectorPayload))
          .filter((connector): connector is Connector => Boolean(connector));

        setAll(normalized);
        
        // FIX: Sort the data by recommendedRank before picking the default
        // This ensures rank 1 (ServiceNow) is always selected first
        setSelectedConnectorId((prev) => {
          if (prev) return prev;
          
          const topRecommended = [...normalized]
            .filter((d) => d.tier === 'recommended')
            .sort((a, b) => (a.recommendedRank ?? 999) - (b.recommendedRank ?? 999));
             
          return topRecommended.length > 0 ? topRecommended[0].id : (normalized[0]?.id ?? null);
        });

        setError(null);
      })
      .catch((e: any) => {
        if (!alive) return;
        setError(e?.message ?? 'Failed to load connectors');
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [fetchCount, token]);

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

  const connectConnector = useCallback(async (id: string) => {
    try {
      await connectConnectorApi(id);
      refetch();
    } catch (e: any) {
      setError(e?.message ?? 'Failed to connect');
    }
  },[refetch]);

  const configureSync = useCallback(async (id: string) => {
    try {
      await configureSyncApi(id);
      refetch();
    } catch (e: any) {
      setError(e?.message ?? 'Failed to configure sync');
    }
  }, [refetch]);

  const disconnectConnector = useCallback(async (id: string) => {
    // Let the caller handle success/error toasts, but always refetch so the tile
    // reflects the cleared credential. Re-throw so the confirm dialog can keep the
    // user informed if the disconnect failed.
    try {
      await disconnectConnectorApi(id);
    } finally {
      refetch();
    }
  }, [refetch]);

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
