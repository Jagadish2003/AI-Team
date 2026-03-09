import React, { createContext, useCallback, useContext, useMemo, useState } from 'react';
import mock from '../data/mockConnectors.json';
import { Connector } from '../types/connector';
import { computeConfidence, Confidence } from '../utils/confidence';
import { getNextBestRecommended } from '../utils/nextBest';

type MockShape = { recommended: Connector[]; connectors: Connector[] };
type ConnectorContextValue = {
  connectors: Connector[];
  recommended: Connector[];
  standard: Connector[];
  selectedConnectorId: string | null;

  recommendedConnectedCount: number;
  confidence: Confidence;
  nextBestRecommendedId: string | null;

  selectConnector: (id: string) => void;
  connectConnector: (id: string) => void;
  configureSync: (id: string) => void;
};

const Ctx = createContext<ConnectorContextValue | null>(null);

function mergeAll(data: MockShape): Connector[] { return [...data.recommended, ...data.connectors]; }

export function ConnectorProvider({ children }: { children: React.ReactNode }) {
  const data = mock as unknown as MockShape;
  const [all, setAll] = useState<Connector[]>(() => mergeAll(data));
  const [selectedConnectorId, setSelectedConnectorId] = useState<string | null>(data.recommended[0]?.id ?? null);

  const recommended = useMemo(() => all.filter(c => c.tier === 'recommended').sort((a,b)=>(a.recommendedRank??999)-(b.recommendedRank??999)), [all]);
  const standard = useMemo(() => all.filter(c => c.tier !== 'recommended'), [all]);

  const recommendedConnectedCount = useMemo(() => recommended.filter(c => c.status === 'connected').length, [recommended]);
  const confidence = useMemo(() => computeConfidence(recommendedConnectedCount), [recommendedConnectedCount]);
  const nextBestRecommendedId = useMemo(() => getNextBestRecommended(recommended), [recommended]);

  const selectConnector = useCallback((id: string) => setSelectedConnectorId(id), []);
  const connectConnector = useCallback((id: string) => {
    setAll(prev => prev.map(c => {
      if (c.id !== id) return c;
      if (c.status === 'coming_soon') return c;
      return { ...c, status: 'connected', lastSynced: 'just now' };
    }));
  }, []);
  const configureSync = useCallback((id: string) => setAll(prev => prev.map(c => (c.id === id ? { ...c, lastSynced: 'just now' } : c))), []);

  const value: ConnectorContextValue = { connectors: all, recommended, standard, selectedConnectorId, recommendedConnectedCount, confidence, nextBestRecommendedId, selectConnector, connectConnector, configureSync };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useConnectorContext() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error('useConnectorContext must be used inside ConnectorProvider');
  return ctx;
}
