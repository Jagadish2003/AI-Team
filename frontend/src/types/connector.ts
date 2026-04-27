export type ConnectorTier = 'recommended' | 'standard' | 'coming_soon';
export type ConnectorStatus = 'connected' | 'not_connected' | 'coming_soon';

export interface ConnectRequest { status: 'connected' | 'not_connected'; }
export type ConnectResponse = Connector;

export interface Metric { label: string; value: string; }

export interface Connector {
  id: string;
  name: string;
  category: string;
  tier: ConnectorTier;
  recommendedRank?: number;
  status: ConnectorStatus;
  configured: boolean;
  metrics: Metric[];
  lastSynced: string;
  reads: string[];
  signalStrength: number;
}
