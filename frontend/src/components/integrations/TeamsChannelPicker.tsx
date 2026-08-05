/**
 * TeamsChannelPicker — Microsoft Teams channel selection.
 *
 * The Teams analogue of SlackChannelPicker (R18-C0 P5): rendered inside
 * ConnectorDetailPanel when connector.id === 'teams' && connector.status ===
 * 'connected'. The customer chooses which granted Teams channels AgentIQ reads;
 * the Teams ingestor (R17-A1 reach + R18-A4 depth) then reads ONLY the selected
 * channels for that org.
 *
 * What it does:
 *   - Lists the selectable channels (GET /api/connectors/teams/channels) — the
 *     granted standard channels across the connected teams
 *   - Multi-select checkboxes (a workspace may read many channels)
 *   - Saves the selection via PATCH /api/connectors/teams/channels
 *   - Editable later — re-open, change the selection, save again
 *   - Carries the R18-A4 depth-phase consent notice inline
 *
 * When no selection has been saved yet (configured === false) NO channel is
 * pre-checked. Until a selection is saved the ingestor still reads every granted
 * channel (the backwards-compatible default); the picker just doesn't pre-check
 * them, so the customer explicitly opts channels in. Viewers get a read-only
 * picker (PATCH is analyst+).
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useToast } from '../common/Toast';
import { ApiError, apiPatch } from '../../lib/apiClient';
import { useAuthOptional } from '../../context/AuthContext';
import { isViewerRole } from '../../utils/roles';
import { useDataCache } from '../../lib/dataCache';
import { cacheKeys } from '../../lib/cacheKeys';
import PickerSkeleton from './PickerSkeleton';
import { usePickerResource } from './usePickerResource';
import ConversationContentConsentNotice from './ConversationContentConsentNotice';

interface TeamsChannel {
  id: string;
  name: string;
  team?: string;
}

interface TeamsChannelsResponse {
  ok: boolean;
  available: TeamsChannel[];
  selected: string[];
  configured: boolean;
}

interface Props {
  onSaved?: () => void;
}

function getSaveErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: unknown };
    return typeof body?.detail === 'string'
      ? body.detail
      : 'Failed to save channel selection.';
  }
  return 'Network error saving channel selection. Please try again.';
}

export default function TeamsChannelPicker({ onSaved }: Props) {
  const { push } = useToast();
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  const cache = useDataCache();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  // True once the user has changed the selection without saving it — a background
  // refresh must never overwrite an edit in progress.
  const dirtyRef = useRef(false);

  // Channels on the SHARED cache: skeleton on the FIRST load only, so a re-render,
  // a remount, or a background refresh keeps the current list on screen (see
  // usePickerResource).
  const { data, firstLoad } = usePickerResource<TeamsChannelsResponse>(
    cacheKeys.connectorTeamsChannels,
    '/api/connectors/teams/channels',
  );
  const available: TeamsChannel[] = data?.available ?? [];

  // Configured → the saved selection. Not configured yet → pre-select NONE
  // (consistent with the Jira/Confluence/SharePoint/GitHub pickers). Until a
  // selection is saved the ingestor still reads every granted channel (the
  // backwards-compatible default); the picker just doesn't pre-check them.
  useEffect(() => {
    if (dirtyRef.current) return;
    if (!data) return;
    setSelected(data.configured ? new Set(data.selected ?? []) : new Set());
  }, [data]);

  function toggleChannel(id: string) {
    dirtyRef.current = true;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const data = await apiPatch<TeamsChannelsResponse>(
        '/api/connectors/teams/channels',
        { channels: [...selected] },
      );
      setSelected(new Set(data.selected));
      // The saved response is written into the cache rather than invalidating the
      // key: an invalidate refetches in the FOREGROUND, which would blank the
      // picker into its skeleton right after a successful save.
      dirtyRef.current = false;
      cache.setData(cacheKeys.connectorTeamsChannels, data);
      push(
        data.selected.length > 0
          ? `Reading ${data.selected.length} Teams channel${data.selected.length > 1 ? 's' : ''}.`
          : 'No Teams channels selected — none will be read.',
      );
      onSaved?.();
    } catch (error) {
      push(getSaveErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }, [selected, push, onSaved, cache]);

  if (firstLoad) {
    return <PickerSkeleton label="Loading Teams channels" />;
  }

  return (
    <div className="mt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-text">Channels AgentIQ reads</div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          Per workspace
        </div>
      </div>

      <p className="text-xs text-muted mb-3 leading-relaxed">
        Choose which Teams channels are part of discovery. AgentIQ reads only the
        selected channels — unselected channels are never ingested, even if the
        connection is granted them. You can change this later.
      </p>

      <ConversationContentConsentNotice scopeLabel="selected channels" />

      <div className="mt-3" />

      {available.length === 0 ? (
        <p className="text-xs text-muted italic">
          No Teams channels available. Confirm your Microsoft 365 admin has granted
          AgentIQ access to at least one standard channel, then reopen this panel.
        </p>
      ) : (
        <div
          role="group"
          aria-label="Teams channels"
          className="space-y-1.5 max-h-[15rem] overflow-y-auto pr-1"
        >
          {available.map((channel) => {
            const isSelected = selected.has(channel.id);
            return (
              <button
                key={channel.id}
                type="button"
                role="checkbox"
                aria-checked={isSelected}
                onClick={() => toggleChannel(channel.id)}
                disabled={isViewer}
                className={[
                  'w-full flex items-center gap-3 rounded-lg border px-3 py-2.5',
                  'text-left transition-[border-color,background-color,box-shadow]',
                  'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/35',
                  isViewer ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
                  isSelected
                    ? 'border-accent bg-accent/10'
                    : 'border-border bg-panel hover:border-accent/40',
                ].join(' ')}
              >
                <div
                  className={[
                    'h-3.5 w-3.5 flex-shrink-0 rounded-sm border flex items-center justify-center',
                    isSelected ? 'border-accent bg-accent/20' : 'border-border',
                  ].join(' ')}
                >
                  {isSelected && (
                    <div className="h-1.5 w-1.5 rounded-[1px] bg-accent" aria-hidden />
                  )}
                </div>
                <span
                  className={`min-w-0 flex-1 truncate text-xs font-medium ${
                    isSelected ? 'text-accent' : 'text-text'
                  }`}
                >
                  {channel.name || channel.id}
                  {channel.team ? (
                    <span className="ml-1.5 text-[10px] font-normal text-muted">
                      {channel.team}
                    </span>
                  ) : null}
                </span>
              </button>
            );
          })}
        </div>
      )}

      <button
        type="button"
        onClick={handleSave}
        disabled={saving || isViewer || available.length === 0}
        className={[
          'mt-3 w-full rounded-lg px-4 py-2 text-sm font-medium',
          'border border-accent/20 bg-accent/5 text-accent transition-colors hover:border-accent/45 hover:bg-accent/10',
          'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40',
          saving || isViewer || available.length === 0 ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer',
        ].join(' ')}
      >
        {saving ? 'Saving…' : 'Save channel selection'}
      </button>

      {available.length > 0 && (
        <p className="mt-2 text-center text-[11px] text-accent">
          {selected.size} of {available.length} channel{available.length > 1 ? 's' : ''} selected
        </p>
      )}
    </div>
  );
}
