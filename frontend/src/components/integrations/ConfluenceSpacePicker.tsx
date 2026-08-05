/**
 * ConfluenceSpacePicker — Confluence space selection (multi-space).
 *
 * The Confluence analogue of JiraProjectPicker / SlackChannelPicker: rendered
 * inside ConnectorDetailPanel when connector.id === 'confluence' &&
 * connector.status === 'connected'. The customer picks WHICH Confluence spaces
 * AgentIQ scopes discovery to (reach signal + R18-A5 deep content), instead of
 * reading every granted space.
 *
 * When no selection has been saved yet (configured === false) NO space is
 * pre-selected — the ingestor reads every granted space until the customer picks.
 * Saving an empty selection means read nothing. Viewers get a read-only picker.
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

interface ConfluenceSpace {
  key: string;
  name: string;
}

interface ConfluenceSpacesResponse {
  ok: boolean;
  available: ConfluenceSpace[];
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
      : 'Failed to save space selection.';
  }
  return 'Network error saving space selection. Please try again.';
}

export default function ConfluenceSpacePicker({ onSaved }: Props) {
  const { push } = useToast();
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  const cache = useDataCache();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  // True once the user has changed the selection without saving it — a background
  // refresh must never overwrite an edit in progress.
  const dirtyRef = useRef(false);

  // Spaces on the SHARED cache: skeleton on the FIRST load only, so a re-render, a
  // remount, or a background refresh keeps the current list on screen (see
  // usePickerResource).
  const { data, firstLoad } = usePickerResource<ConfluenceSpacesResponse>(
    cacheKeys.connectorSpaces,
    '/api/connectors/confluence/spaces',
  );
  const available: ConfluenceSpace[] = data?.available ?? [];

  useEffect(() => {
    if (dirtyRef.current) return;
    if (!data) return;
    setSelected(data.configured ? new Set(data.selected ?? []) : new Set());
  }, [data]);

  function toggle(key: string) {
    dirtyRef.current = true;
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const data = await apiPatch<ConfluenceSpacesResponse>(
        '/api/connectors/confluence/spaces',
        { spaces: [...selected] },
      );
      setSelected(new Set(data.selected));
      // The saved response is written into the cache rather than invalidating the
      // key: an invalidate refetches in the FOREGROUND, which would blank the
      // picker into its skeleton right after a successful save.
      dirtyRef.current = false;
      cache.setData(cacheKeys.connectorSpaces, data);
      push(
        data.selected.length > 0
          ? `Reading ${data.selected.length} Confluence space${data.selected.length > 1 ? 's' : ''}.`
          : 'No Confluence spaces selected — none will be read.',
      );
      onSaved?.();
    } catch (error) {
      push(getSaveErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }, [selected, push, onSaved, cache]);

  if (firstLoad) {
    return <PickerSkeleton label="Loading Confluence spaces" />;
  }

  return (
    <div className="mt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-text">Spaces AgentIQ reads</div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          Per workspace
        </div>
      </div>

      <p className="text-xs text-muted mb-3 leading-relaxed">
        Choose which Confluence spaces are part of discovery. AgentIQ reads only the
        selected spaces — unselected spaces are never ingested, even if the
        connection is granted them. You can change this later.
      </p>

      {available.length === 0 ? (
        <p className="text-xs text-muted italic">
          No Confluence spaces available. Confirm the connection has access to at
          least one space, then reopen this panel.
        </p>
      ) : (
        <div
          role="group"
          aria-label="Confluence spaces"
          className="space-y-1.5 max-h-[15rem] overflow-y-auto pr-1"
        >
          {available.map((space) => {
            const isSelected = selected.has(space.key);
            return (
              <button
                key={space.key}
                type="button"
                role="checkbox"
                aria-checked={isSelected}
                onClick={() => toggle(space.key)}
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
                  {space.name || space.key}
                  <span className="ml-1.5 text-[10px] font-normal text-muted">
                    {space.key}
                  </span>
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
        {saving ? 'Saving…' : 'Save space selection'}
      </button>

      {available.length > 0 && (
        <p className="mt-2 text-center text-[11px] text-accent">
          {selected.size} of {available.length} space{available.length > 1 ? 's' : ''} selected
        </p>
      )}
    </div>
  );
}
