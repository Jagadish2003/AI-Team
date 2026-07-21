/**
 * JiraProjectPicker — Jira project selection (multi-project).
 *
 * The Jira analogue of SlackChannelPicker (R18-C0 P5): rendered inside
 * ConnectorDetailPanel when connector.id === 'jira' && connector.status ===
 * 'connected'. The customer picks WHICH Jira projects AgentIQ scopes discovery to
 * (JQL project IN (...)), instead of the hardcoded JIRA_PROJECT_KEY default.
 *
 * What it does:
 *   - Lists the selectable projects (GET /api/connectors/jira/projects)
 *   - Multi-select checkboxes — a workspace may scope to several projects
 *   - Saves the selection via PATCH /api/connectors/jira/projects
 *   - Editable later — re-open, change the selection, save again
 *
 * When no selection has been saved yet (configured === false) NO project is
 * pre-selected — the ingestor falls back to the JIRA_PROJECT_KEY default until the
 * customer picks. Saving an empty selection clears it (back to that default).
 * Viewers get a read-only picker (PATCH is analyst+).
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useToast } from '../common/Toast';
import { ApiError, apiGet, apiPatch } from '../../lib/apiClient';
import { useAuthOptional } from '../../context/AuthContext';
import { isViewerRole } from '../../utils/roles';
import PickerSkeleton from './PickerSkeleton';

interface JiraProject {
  key: string;
  name: string;
}

interface JiraProjectsResponse {
  ok: boolean;
  available: JiraProject[];
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
      : 'Failed to save project selection.';
  }
  return 'Network error saving project selection. Please try again.';
}

export default function JiraProjectPicker({ onSaved }: Props) {
  const { push } = useToast();
  const auth = useAuthOptional();
  const isViewer = isViewerRole(auth?.user?.role);

  const [available, setAvailable] = useState<JiraProject[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // Load selectable projects + current selection on mount.
  useEffect(() => {
    setLoading(true);
    apiGet<JiraProjectsResponse>('/api/connectors/jira/projects')
      .then((data) => {
        setAvailable(data?.available ?? []);
        // Only a saved selection pre-selects; unconfigured leaves it blank (the
        // ingestor uses the JIRA_PROJECT_KEY default until the customer chooses).
        setSelected(data?.configured ? new Set(data.selected ?? []) : new Set());
      })
      .catch(() => {
        // Silent failure — an empty picker is a safe default.
      })
      .finally(() => setLoading(false));
  }, []);

  function toggleProject(key: string) {
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
      const data = await apiPatch<JiraProjectsResponse>(
        '/api/connectors/jira/projects',
        { projects: [...selected] },
      );
      setSelected(new Set(data.selected));
      push(
        data.selected.length > 0
          ? `Scoping discovery to ${data.selected.length} Jira project${data.selected.length > 1 ? 's' : ''}.`
          : 'Jira project selection cleared — using the default project.',
      );
      onSaved?.();
    } catch (error) {
      push(getSaveErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }, [selected, push, onSaved]);

  if (loading) {
    return <PickerSkeleton label="Loading Jira projects" />;
  }

  return (
    <div className="mt-4">
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-text">Projects AgentIQ reads</div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          Per workspace
        </div>
      </div>

      <p className="text-xs text-muted mb-3 leading-relaxed">
        Choose which Jira projects are part of discovery. AgentIQ scopes its Jira
        reads to the selected projects — unselected projects are never ingested. You
        can change this later.
      </p>

      {available.length === 0 ? (
        <p className="text-xs text-muted italic">
          No Jira projects available. Confirm the connection has access to at least
          one project, then reopen this panel.
        </p>
      ) : (
        <div
          role="group"
          aria-label="Jira projects"
          className="space-y-1.5 max-h-[15rem] overflow-y-auto pr-1"
        >
          {available.map((project) => {
            const isSelected = selected.has(project.key);
            return (
              <button
                key={project.key}
                type="button"
                role="checkbox"
                aria-checked={isSelected}
                onClick={() => toggleProject(project.key)}
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
                  {project.name}
                  <span className="ml-1.5 text-[10px] font-normal text-muted">
                    {project.key}
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
        {saving ? 'Saving…' : 'Save project selection'}
      </button>

      {available.length > 0 && (
        <p className="mt-2 text-center text-[11px] text-accent">
          {selected.size} of {available.length} project{available.length > 1 ? 's' : ''} selected
        </p>
      )}
    </div>
  );
}
