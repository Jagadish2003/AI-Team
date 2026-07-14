export type ConnectorTier = 'recommended' | 'standard' | 'coming_soon';
export type ConnectorStatus =
  | 'connected'
  | 'not_connected'
  | 'disconnected'
  | 'not_configured'
  | 'coming_soon';

export interface ConnectRequest { status: 'connected' | 'not_connected' | 'disconnected' | 'not_configured'; }
export type ConnectResponse = Connector;

// R18-A3 follow-up: a one-shot request to auto-open a connector's outbound /
// credential setup modal from the Integration Hub tile (the "Set up outbound
// access" / "Enter credentials" button). `nonce` bumps on every click so the
// same connector can be re-triggered; the setup managers open their modal only
// when `connectorId` matches and the nonce is new.
export interface OutboundSetupRequest {
  connectorId: string;
  nonce: number;
}

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
  // Declared Salesforce cloud products for this org (e.g. 'salesforce_ncino',
  // 'salesforce_sc'). Drives the connector's actual read-scope display.
  products?: string[];
}
