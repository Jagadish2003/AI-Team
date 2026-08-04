/**
 * connectorEnablement.ts — the Integration Hub's connectability gate and the
 * status pill derived from it, in ONE place.
 *
 * This lived privately inside ConnectorTile.tsx. It moved here because the status
 * pill is rendered on three surfaces (the tile, its detail panel, and the hero
 * card) and all three must answer "is this connectable, and what state is it in?"
 * identically — a tile reading "Not configured" beside a panel reading
 * "Disconnected" for the same connector is a contradiction the user sees.
 */
import { Connector, ConnectorStatus } from '../../types/connector';

// Connectors whose Connect button is ENABLED on the Integration Hub. This is a
// UI gate only — the OAuth backends for the other connectors (Slack AT-420,
// Teams AT-434, Confluence/SharePoint AT-462, GitHub) remain fully wired
// (CONNECTOR_AUTH_CONFIGS + the generic auth-url → callback flow), so re-enabling
// one later is just adding its id back to this list.
//
// Product decision (July 2026): the three systems of record plus GitHub are
// connectable from the hub. R18-A4 (Slack & Teams Deep Content) adds the two chat
// platforms and R18-A5 (Confluence & SharePoint Deep Content) adds the two
// knowledge platforms — their OAuth backends (AT-420 Slack / AT-434 Teams /
// AT-462 Confluence+SharePoint) and the reach + depth ingestion paths are fully
// wired, and connecting them is what surfaces the deep-content consent copy and
// starts conversation/page ingestion. Every other tile renders its action button
// disabled with the "Connecting new sources is currently unavailable" tooltip.
export const ENABLED_CONNECTOR_IDS = [
  'salesforce', 'servicenow', 'jira', 'github', 'slack', 'teams',
  'confluence', 'sharepoint',
];

/**
 * Is this connector's connect action enabled?
 *
 * MSP-B13 (AT-748): a multi-scope cloud connector (AWS/Azure Events) onboards in
 * the detail panel (credentials + scope pinning + per-scope health) rather than
 * via the tile's OAuth flow, so it bypasses the OAuth-only enablement list.
 */
export function isConnectorEnabled(
  connector: Pick<Connector, 'id' | 'multiScope'>,
): boolean {
  return Boolean(connector.multiScope) || ENABLED_CONNECTOR_IDS.includes(connector.id);
}

/**
 * The status pill to show, derived from the SAME gate as the connect action
 * rather than from the raw catalog status.
 *
 * A connector whose action is disabled cannot be in a connection state at all,
 * so "Disconnected" would imply a connection the user could restore — it reads
 * as "Not configured". An enabled connector is honestly Connected or
 * Disconnected. `coming_soon` is not produced here: that pill is the withdrawn
 * roadmap labelling, which ConnectorTile renders separately when its release
 * flag is on.
 */
export function connectorBadgeStatus(
  connector: Pick<Connector, 'id' | 'multiScope' | 'status'>,
): ConnectorStatus {
  if (!isConnectorEnabled(connector)) return 'not_configured';
  return connector.status === 'connected' ? 'connected' : 'disconnected';
}
