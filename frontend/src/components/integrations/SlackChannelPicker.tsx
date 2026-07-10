/**
 * SlackChannelPicker — R18-C0 P5
 *
 * Rendered inside ConnectorDetailPanel when connector.id === 'slack' and
 * connector.status === 'connected'. On Slack connect the customer chooses which
 * public channels AgentIQ may read; the Slack ingestor (R16-A2) then reads ONLY
 * the selected channels for that org.
 *
 * What it does:
 *   - Lists the selectable public channels (GET /api/connectors/slack/channels)
 *   - Multi-select checkboxes (a workspace may read many channels)
 *   - Saves the selection via PATCH /api/connectors/slack/channels
 *   - Editable later — re-open, change the selection, save again
 *
 * Consent clarity: the customer can see exactly which channels are part of
 * discovery and exclude noisy/irrelevant ones. When no selection has been saved
 * yet (configured === false) every channel is pre-checked, reflecting the
 * backwards-compatible default (read all) so the customer can narrow it.
 *
 * Viewers get a read-only picker (PATCH is analyst+).
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useToast } from '../common/Toast';
import { ApiError, apiGet, apiPatch } from '../../lib/apiClient';
import { useAuthOptional } from '../../context/AuthContext';
import { isViewerRole } from '../../utils/roles';

interface SlackChannel {
  id: string;
  name: string;
}

interface SlackChannelsResponse {
  ok: boolean;
  available: SlackChannel[];
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

export default function SlackChannelPicker({ onSaved }: Props) {
  const { push } = useToast();
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  const [available, setAvailable] = useState<SlackChannel[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // Load selectable channels + current selection on mount.
  useEffect(() => {
    setLoading(true);
    apiGet<SlackChannelsResponse>('/api/connectors/slack/channels')
      .then((data) => {
        const channels = data?.available ?? [];
        setAvailable(channels);
        // Configured → the saved selection. Not configured yet → pre-check all,
        // which reflects the current "read all accessible channels" default and
        // lets the customer narrow it.
        setSelected(
          data?.configured
            ? new Set(data.selected ?? [])
            : new Set(channels.map((c) => c.id)),
        );
      })
      .catch(() => {
        // Silent failure — an empty picker is a safe default.
      })
      .finally(() => setLoading(false));
  }, []);

  function toggleChannel(id: string) {
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
      const data = await apiPatch<SlackChannelsResponse>(
        '/api/connectors/slack/channels',
        { channels: [...selected] },
      );
      setSelected(new Set(data.selected));
      push(
        data.selected.length > 0
          ? `Reading ${data.selected.length} Slack channel${data.selected.length > 1 ? 's' : ''}.`
          : 'No Slack channels selected — none will be read.',
      );
      onSaved?.();
    } catch (error) {
      push(getSaveErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }, [selected, push, onSaved]);

  if (loading) {
    return (
      <div className="mt-4 text-xs text-muted animate-pulse">
        Loading Slack channels…
      </div>
    );
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
        Choose which Slack channels are part of discovery. AgentIQ reads only the
        selected channels — unselected channels are never ingested, even if the
        connection can see them. You can change this later.
      </p>

      {available.length === 0 ? (
        <p className="text-xs text-muted italic">
          No public channels available. Invite AgentIQ to the channels it should
          read, then reopen this panel.
        </p>
      ) : (
        <div
          role="group"
          aria-label="Slack channels"
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
                  #{channel.name || channel.id}
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
