/**
 * ConnectorGroupSection — ENG-IH-2 Sprint 9
 *
 * Renders one capability group (Group A/B/C) in the Integration Hub
 * redesigned layout. Replaces the flat recommended/standard split with
 * a taxonomy-aware group header + connector tile grid.
 *
 * Each group shows:
 *   - Group label (e.g. "PRIMARY BUSINESS PLATFORMS")
 *   - Sub-label (e.g. "Where your operation's core workflows live")
 *   - Connector tiles for connected and needs_auth systems only
 *   - "Add a [category] source" CTA when the group has connectors to add
 *     CTA navigates to /integration-hub?category={categoryId} (deep-link)
 *
 * Deep-link (?category= param):
 *   When Stack Builder Screen 2 sends the user to Integration Hub
 *   with ?category=comms_knowledge, this component's parent reads the
 *   param and opens the connector picker pre-filtered to that category.
 *   The CTA anchor uses the category ID so it round-trips correctly.
 *
 * Props:
 *   group          — GroupConfig (label, subLabel, categoryId, connectors)
 *   selectedId     — currently selected connector ID
 *   onSelect       — called when a connector tile is clicked
 *   onPrimary      — called when the Connect / Configure button is clicked
 *   onAddSource    — called when "Add a source" CTA is clicked
 */
import React from 'react';
import { Connector } from '../../types/connector';
import ConnectorTile from './ConnectorTile';
import { connectorIcons, fallbackConnectorIcon } from './ConnectorIcons';
import { PlusCircle } from 'lucide-react';
import ConnectedToolsStatus from './ConnectedToolsStatus';

export interface GroupConfig {
  label:       string;
  subLabel:    string;
  categoryId:  string;   // matches ?category= param and catalog API key
  connectors:  Connector[];
  allSystemIds: string[]; // all possible system IDs in this group
}

interface Props {
  group:      GroupConfig;
  selectedId: string | null;
  onSelect:   (id: string) => void;
  onPrimary:  (id: string) => void;
  onReconnect?: (id: string) => void;
  // R18-C0 P4 / AT-566: forwarded to each connected tile's Disconnect action.
  onDisconnect?: (id: string) => void;
  // R18-A3 follow-up: forwarded to each tile's "Set up outbound access" action
  // so the parent can pop the outbound/credential setup modal for that connector.
  onSetupOutbound?: (id: string) => void;
  onAddSource: (categoryId: string) => void;
  // R17-D4 Addendum A / T11: when the org is at its licensed system limit, the
  // Connect action on not-yet-connected tiles is disabled with connectBlockMessage
  // (AC10). Forward-only — the tile itself keeps already-connected systems
  // actionable. Optional so the section renders unchanged where unused.
  connectBlocked?: boolean;
  connectBlockMessage?: string;
  // Id of the connector whose OAuth connect flow is currently in flight, if any.
  // Its tile renders a disabled "Connecting…" action so the flow is one-click.
  connectingId?: string | null;
}

export default function ConnectorGroupSection({
  group, selectedId, onSelect, onPrimary, onReconnect, onDisconnect, onSetupOutbound, onAddSource,
  connectBlocked, connectBlockMessage, connectingId = null,
}: Props) {
  const hasConnectors = group.connectors.length > 0;
  const shouldScrollConnectors = group.connectors.length > 6;
  const connectorGridClass = 'grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3';

  return (
    <div className="rounded-xl border border-border bg-panel p-5 shadow-sm">

      {/* Group header */}
      <div className="mb-1 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="mb-0.5 text-xs font-semibold uppercase tracking-widest text-text">
            {group.label}
          </div>
          <div className="text-xs text-muted">{group.subLabel}</div>
        </div>
        <ConnectedToolsStatus connectors={group.connectors} />
      </div>

      {/* Connector tiles */}
      {hasConnectors ? (
        <div
          className={[
            'mt-4 min-h-0',
            shouldScrollConnectors
              ? 'connector-group-tile-scroll pr-2 focus:outline-none focus:ring-2 focus:ring-accent/40'
              : '',
          ].join(' ')}
          role={shouldScrollConnectors ? 'region' : undefined}
          aria-label={shouldScrollConnectors ? `${group.label} connectors` : undefined}
          tabIndex={shouldScrollConnectors ? 0 : undefined}
        >
          <div className={connectorGridClass}>
            {group.connectors.map(c => (
              <ConnectorTile
                key={c.id}
                connector={c}
                icon={connectorIcons[c.name] ?? fallbackConnectorIcon}
                selected={selectedId === c.id}
                onSelect={() => onSelect(c.id)}
                onPrimary={() => onPrimary(c.id)}
                onReconnect={onReconnect ? () => onReconnect(c.id) : undefined}
                onDisconnect={onDisconnect ? () => onDisconnect(c.id) : undefined}
                onSetupOutbound={onSetupOutbound ? () => onSetupOutbound(c.id) : undefined}
                connectBlocked={connectBlocked}
                connectBlockMessage={connectBlockMessage}
                connecting={connectingId === c.id}
              />
            ))}
          </div>
        </div>
      ) : (
        <div className="mt-3 text-xs text-muted italic">
          No systems connected in this category yet.
        </div>
      )}

      {/* Add a source CTA */}
      <button
        type="button"
        onClick={() => onAddSource(group.categoryId)}
        className={[
          'mt-4 inline-flex items-center gap-1.5 rounded-md border border-accent/20 bg-accent/5 px-2.5 py-1.5 text-xs font-medium text-accent',
          'transition-colors hover:border-accent/45 hover:bg-accent/10 cursor-pointer',
          'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40',
        ].join(' ')}
      >
        <PlusCircle size={13} strokeWidth={1.8} className="flex-shrink-0" />
        Add a {group.label.toLowerCase()} source
      </button>
    </div>
  );
}
