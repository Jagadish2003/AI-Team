/**
 * SqlServerScopePicker — T2-S11-A Task T9
 *
 * Rendered inside ConnectorDetailPanel when:
 *   - connector.id === 'sql_server' OR connector.id === 'sqlserver'
 *   - connector.status === 'connected'
 *
 * What it does:
 *   - Loads schema discovery on mount: GET /api/db-connectors/sqlserver/schema
 *   - Pre-populates saved scope on mount: GET /api/db-connectors/sqlserver/scope
 *   - Displays a checkbox tree: schema row (select all) + expandable tables
 *   - Saves scope declaration: POST /api/db-connectors/sqlserver/scope
 *   - Shows a save confirmation toast on success
 *   - Shows inline error with retry button on load failure
 *   - All error states are local — parent panel is never broken by this component
 *
 * RBAC stub:
 *   - viewerOnly prop (true = Analyst, false = Editor+)
 *   - When viewerOnly=true, POST button is disabled with tooltip
 *   - Wire actual require_role check when T1-S11 Task 2 lands
 *
 * Props:
 *   onSaved    — called after successful scope save (parent can refetch)
 *   viewerOnly — true blocks saves (Analyst+ per T1-S11); default false
 */

import React, { useCallback, useEffect, useState } from 'react';
import { useToast } from '../common/Toast';
import { ApiError, apiGet, apiPost } from '../../lib/apiClient';
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react';

// ── API types ─────────────────────────────────────────────────────────────────

interface TableMeta {
  schema: string;
  table: string;
}

interface SchemaDiscoveryResult {
  schemas: string[];
  tables: TableMeta[];
  columns?: unknown[];
  estimated_row_counts?: Record<string, number> | null;
}

interface ScopeResponse {
  schemas: string[];
  tables: string[];
}

// ── Props ─────────────────────────────────────────────────────────────────────

interface Props {
  /** Called after a successful scope save so the parent can refetch. */
  onSaved?: () => void;
  /**
   * Stub for T1-S11 Task 2 RBAC.
   * true  → user is Viewer/Analyst, POST is blocked with tooltip.
   * false → user is Editor+, save allowed.
   */
  viewerOnly?: boolean;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractSaveError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown };
    return typeof body?.detail === 'string'
      ? body.detail
      : 'Failed to save scope declaration.';
  }
  return 'Network error saving scope. Please try again.';
}

function extractLoadError(err: unknown): string {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown };
    return typeof body?.detail === 'string'
      ? body.detail
      : 'Failed to load schema discovery.';
  }
  return 'Network error loading schema. Please try again.';
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function SqlServerScopePicker({
  onSaved,
  viewerOnly = false,
}: Props) {
  const { push } = useToast();

  // ── Fetch state ────────────────────────────────────────────────────────────
  const [discovery, setDiscovery] = useState<SchemaDiscoveryResult | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // ── Selection state ────────────────────────────────────────────────────────
  const [selectedSchemas, setSelectedSchemas] = useState<Set<string>>(new Set());
  const [selectedTables, setSelectedTables] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // ── Save state ─────────────────────────────────────────────────────────────
  const [saving, setSaving] = useState(false);

  // ── Load schema discovery + previously saved scope ─────────────────────────
  const loadData = useCallback(() => {
    setLoading(true);
    setLoadError(null);

    Promise.all([
      apiGet<SchemaDiscoveryResult>('/api/db-connectors/sqlserver/schema').catch(
        (err) => {
          setLoadError(extractLoadError(err));
          return null;
        }
      ),
      apiGet<ScopeResponse>('/api/db-connectors/sqlserver/scope').catch(
        () => null
      ),
    ]).then(([disc, savedScope]) => {
      if (!disc) {
        setLoading(false);
        return;
      }

      setDiscovery(disc);

      if (savedScope) {
        setSelectedSchemas(new Set(savedScope.schemas ?? []));
        setSelectedTables(new Set(savedScope.tables ?? []));
      }

      setLoading(false);
    });
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  // ── Toggle schema selection ────────────────────────────────────────────────
  const handleToggleSchema = useCallback(
    (schema: string) => {
      const newSchemas = new Set(selectedSchemas);
      const tablesInSchema = (discovery?.tables ?? [])
        .filter((t) => t.schema === schema)
        .map((t) => t.table);

      if (newSchemas.has(schema)) {
        // Deselect schema: remove it and all its tables
        newSchemas.delete(schema);
        const newTables = new Set(selectedTables);
        tablesInSchema.forEach((t) => newTables.delete(t));
        setSelectedTables(newTables);
      } else {
        // Select schema: add it and all its tables
        newSchemas.add(schema);
        const newTables = new Set(selectedTables);
        tablesInSchema.forEach((t) => newTables.add(t));
        setSelectedTables(newTables);
      }

      setSelectedSchemas(newSchemas);
    },
    [discovery, selectedSchemas, selectedTables]
  );

  // ── Toggle individual table selection ──────────────────────────────────────
  const handleToggleTable = useCallback(
    (schema: string, table: string) => {
      const newTables = new Set(selectedTables);
      const tablesInSchema = (discovery?.tables ?? [])
        .filter((t) => t.schema === schema)
        .map((t) => t.table);

      if (newTables.has(table)) {
        newTables.delete(table);
      } else {
        newTables.add(table);
      }

      // Update schema checkbox: checked only if ALL tables are selected
      const allTablesSelected = tablesInSchema.every((t) =>
        newTables.has(t)
      );

      const newSchemas = new Set(selectedSchemas);
      if (allTablesSelected) {
        newSchemas.add(schema);
      } else {
        newSchemas.delete(schema);
      }

      setSelectedSchemas(newSchemas);
      setSelectedTables(newTables);
    },
    [discovery, selectedTables]
  );

  // ── Toggle schema expansion ────────────────────────────────────────────────
  const handleToggleExpanded = useCallback((schema: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(schema)) {
        next.delete(schema);
      } else {
        next.add(schema);
      }
      return next;
    });
  }, []);

  // ── Save scope declaration ─────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await apiPost<{ ok: boolean; scope: ScopeResponse }>(
        '/api/db-connectors/sqlserver/scope',
        {
          schemas: Array.from(selectedSchemas),
          tables: Array.from(selectedTables),
        }
      );
      push({
        type: 'success',
        message: 'Scope declaration saved successfully.',
      });
      if (onSaved) onSaved();
    } catch (err) {
      push({
        type: 'error',
        message: extractSaveError(err),
      });
    } finally {
      setSaving(false);
    }
  }, [selectedSchemas, selectedTables, push, onSaved]);

  // ── Render ─────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8">
        <Loader2 size={20} className="animate-spin text-muted" />
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="rounded-md border border-red-200 bg-red-50 p-4">
        <p className="text-sm text-red-700">{loadError}</p>
        <button
          onClick={loadData}
          className="mt-3 inline-block rounded bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-700"
        >
          Retry
        </button>
      </div>
    );
  }

  if (!discovery || discovery.schemas.length === 0) {
    return (
      <div className="rounded-md border border-border bg-bg/20 p-4">
        <p className="text-sm text-muted">
          No schemas discovered. Check your connection and try again.
        </p>
      </div>
    );
  }

  // Group tables by schema
  const tablesBySchema = new Map<string, string[]>();
  (discovery.tables ?? []).forEach((t) => {
    const tables = tablesBySchema.get(t.schema) ?? [];
    tables.push(t.table);
    tablesBySchema.set(t.schema, tables);
  });

  return (
    <div className="space-y-4">
      {/* Title */}
      <div className="text-sm font-medium text-text">Scope Declaration</div>

      {/* Schema tree */}
      <div className="rounded-md border border-border bg-bg/50 p-3 space-y-2">
        {discovery.schemas.map((schema) => {
          const tablesInSchema = tablesBySchema.get(schema) ?? [];
          const isExpanded = expanded.has(schema);
          const isSchemaSelected = selectedSchemas.has(schema);
          const allTablesSelected = tablesInSchema.every((t) =>
            selectedTables.has(t)
          );
          const someTablesSelected =
            tablesInSchema.some((t) => selectedTables.has(t)) &&
            !allTablesSelected;

          return (
            <div key={schema}>
              {/* Schema row */}
              <div className="flex items-center gap-2 py-1">
                {tablesInSchema.length > 0 && (
                  <button
                    onClick={() => handleToggleExpanded(schema)}
                    className="p-0 text-muted hover:text-text"
                    aria-label={isExpanded ? 'Collapse' : 'Expand'}
                  >
                    {isExpanded ? (
                      <ChevronDown size={16} />
                    ) : (
                      <ChevronRight size={16} />
                    )}
                  </button>
                )}

                {tablesInSchema.length === 0 && (
                  <div className="w-4" />
                )}

                <input
                  type="checkbox"
                  checked={allTablesSelected}
                  ref={(el) => {
                    if (el) {
                      el.indeterminate = someTablesSelected;
                    }
                  }}
                  onChange={() => handleToggleSchema(schema)}
                  className="h-4 w-4 rounded border-border text-blue-600 cursor-pointer"
                  aria-label={`Select schema ${schema}`}
                />

                <label className="flex-1 cursor-pointer text-sm font-medium text-text">
                  {schema}
                </label>

                <span className="text-xs text-muted">
                  {tablesInSchema.length} table{tablesInSchema.length !== 1 ? 's' : ''}
                </span>
              </div>

              {/* Table rows (expanded) */}
              {isExpanded && tablesInSchema.length > 0 && (
                <div className="ml-6 space-y-1 pl-2 border-l border-border">
                  {tablesInSchema.map((table) => (
                    <div key={table} className="flex items-center gap-2 py-1">
                      <input
                        type="checkbox"
                        checked={selectedTables.has(table)}
                        onChange={() => handleToggleTable(schema, table)}
                        className="h-4 w-4 rounded border-border text-blue-600 cursor-pointer"
                        aria-label={`Select table ${schema}.${table}`}
                      />
                      <label className="flex-1 cursor-pointer text-sm text-text">
                        {table}
                      </label>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Save button */}
      <button
        onClick={handleSave}
        disabled={saving || viewerOnly}
        title={
          viewerOnly
            ? 'Analyst+ role required to save scope declaration (T1-S11 Task 2)'
            : ''
        }
        className={`w-full rounded px-3 py-2 text-sm font-medium transition-colors ${
          saving || viewerOnly
            ? 'cursor-not-allowed bg-muted/30 text-muted'
            : 'cursor-pointer bg-blue-600 text-white hover:bg-blue-700'
        }`}
      >
        {saving ? (
          <span className="flex items-center justify-center gap-2">
            <Loader2 size={16} className="animate-spin" />
            Saving...
          </span>
        ) : (
          'Save Scope Declaration'
        )}
      </button>
    </div>
  );
}
