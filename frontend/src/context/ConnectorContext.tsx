import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { Connector } from '../types/connector';
import { computeConfidence, Confidence } from '../utils/confidence';
import { getNextBestRecommended } from '../utils/nextBest';
import { isDiscoveryReadyConnector } from '../utils/sourceReadiness';
import { connectConnectorApi, configureSyncApi, fetchConnectors } from '../services/staticApi';

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
};

const Ctx = createContext<ConnectorContextValue | null>(null);

export function ConnectorProvider({ children }: { children: React.ReactNode }) {
  const [all, setAll] = useState<Connector[]>([]);
  const[selectedConnectorId, setSelectedConnectorId] = useState<string | null>(null);

  const[loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [fetchCount, setFetchCount] = useState<number>(0);

  const refetch = useCallback(() => setFetchCount((c) => c + 1),[]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchConnectors()
      .then((data) => {
        if (!alive) return;
        setAll(data);
        
        // FIX: Sort the data by recommendedRank before picking the default
        // This ensures rank 1 (ServiceNow) is always selected first
        setSelectedConnectorId((prev) => {
          if (prev) return prev;
          
          const topRecommended = [...data]
            .filter((d) => d.tier === 'recommended')
            .sort((a, b) => (a.recommendedRank ?? 999) - (b.recommendedRank ?? 999));
            
          return topRecommended.length > 0 ? topRecommended[0].id : (data[0]?.id ?? null);
        });

        setError(null);
      })
      .catch((e: any) => {
        if (!alive) return;
        setError(e?.message ?? 'Failed to load connectors');
      })
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [fetchCount]);

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
    configureSync
  }),[
    all, recommended, standard, selectedConnectorId,
    loading, error, recommendedConnectedCount, confidence, nextBestRecommendedId,
    selectConnector, connectConnector, configureSync, refetch
  ]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useConnectorContext() {
  const ctx = useContext(Ctx);
  if (!ctx)
    throw new Error('useConnectorContext must be used inside ConnectorProvider');
  return ctx;
}
