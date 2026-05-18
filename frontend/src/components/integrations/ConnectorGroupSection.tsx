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
  onAddSource: (categoryId: string) => void;
}

export default function ConnectorGroupSection({
  group, selectedId, onSelect, onPrimary, onAddSource,
}: Props) {
  const hasConnectors = group.connectors.length > 0;
  const connectedCount = group.connectors.filter(
    c => c.status === 'connected'
  ).length;

  return (
    <div className="rounded-xl border border-border bg-panel p-5 shadow-sm">

      {/* Group header */}
      <div className="flex items-center justify-between mb-1">
        <div>
          <div className="text-xs font-semibold text-muted uppercase tracking-widest mb-0.5">
            {group.label}
          </div>
          <div className="text-xs text-muted">{group.subLabel}</div>
        </div>
        {connectedCount > 0 && (
          <span className="text-xs text-emerald-500 font-medium">
            {connectedCount} connected
          </span>
        )}
      </div>

      {/* Connector tiles */}
      {hasConnectors ? (
        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
          {group.connectors.map(c => (
            <ConnectorTile
              key={c.id}
              connector={c}
              icon={connectorIcons[c.name] ?? fallbackConnectorIcon}
              selected={selectedId === c.id}
              onSelect={() => onSelect(c.id)}
              onPrimary={() => onPrimary(c.id)}
            />
          ))}
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
          'mt-4 flex items-center gap-1.5 text-xs text-muted',
          'hover:text-text transition-colors cursor-pointer',
          'focus:outline-none focus:ring-2 focus:ring-emerald-500/50 rounded',
        ].join(' ')}
      >
        <PlusCircle size={13} strokeWidth={1.8} className="flex-shrink-0" />
        Add a {group.label.toLowerCase()} source
      </button>
    </div>
  );
}
