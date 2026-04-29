import type { Connector } from '../types/connector';

export const DISCOVERY_SOURCE_REQUIREMENT_MESSAGE =
  'Connect and configure at least one source to start discovery.';

const UNSYNCED_LABELS = new Set(['', '-', '--', '\u2014', '\u00e2\u20ac\u201d']);

export function hasSuccessfulSync(connector: Connector): boolean {
  const lastSynced = (connector.lastSynced ?? '').trim();
  return Boolean(lastSynced) && !UNSYNCED_LABELS.has(lastSynced);
}

export function isDiscoveryReadyConnector(connector: Connector): boolean {
  return (
    connector.status === 'connected' &&
    connector.configured === true &&
    hasSuccessfulSync(connector)
  );
}
