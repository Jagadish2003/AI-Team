/**
 * OracleScopePicker — T2-S12-A
 *
 * Rendered inside ConnectorDetailPanel when the selected connector is Oracle DB
 * and its status is 'connected'.
 *
 * Identical flow to SqlServerScopePicker with these differences:
 *   - Calls /api/db-connectors/oracle_db/schema and /oracle_db/scope
 *   - Oracle schema names are stored in UPPERCASE — displayed verbatim
 *   - Case-sensitivity tooltip informs users names are shown exactly as stored
 *
 * Role enforcement:
 *   The POST save button is disabled with a tooltip when viewerOnly=true.
 *   Wire the actual RBAC check when T1-S11 Task 2 lands.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useToast } from '../common/Toast';
import { ApiError, apiGet, apiPost } from '../../lib/apiClient';

// ── API types ─────────────────────────────────────────────────────────────────

interface TableMeta {
  schema: string;
  table: string;
}

interface SchemaDiscoveryResult {
  schemas: string[];
  tables: TableMeta[];
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
   * true  → user is Viewer, POST is blocked with tooltip.
   * false → user is Analyst+, save allowed.
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

// ── Component ─────────────────────────────────────────────────────────────────

export default function OracleScopePicker({
  onSaved,
  viewerOnly = false,
}: Props) {
  const { push } = useToast();

  // ── Fetch state ────────────────────────────────────────────────────────────
  const [discovery, setDiscovery]   = useState<SchemaDiscoveryResult | null>(null);
  const [loadError, setLoadError]   = useState<string | null>(null);
  const [loading, setLoading]       = useState(true);

  // ── Selection state ────────────────────────────────────────────────────────
  const [selectedSchemas, setSelectedSchemas] = useState<Set<string>>(new Set());
  const [selectedTables, setSelectedTables]   = useState<Set<string>>(new Set());
  const [expanded, setExpanded]               = useState<Set<string>>(new Set());

  // ── Save state ─────────────────────────────────────────────────────────────
  const [saving, setSaving] = useState(false);

  // ── Load schema discovery + previously saved scope ─────────────────────────
  const loadData = useCallback(() => {
    setLoading(true);
    setLoadError(null);

    Promise.all([
      apiGet<SchemaDiscoveryResult>('/api/db-connectors/oracle_db/schema').catch(
        () => null,
      ),
      apiGet<ScopeResponse>('/api/db-connectors/oracle_db/scope').catch(
        () => null,
      ),
    ]).then(([disc, savedScope]) => {
      if (!disc) {
        setLoadError(
          'No schemas discovered. Check your connection and try again.',
        );
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

  // ── Table list helpers ─────────────────────────────────────────────────────
  function tablesForSchema(schema: string): string[] {
    if (!discovery) return [];
    return discovery.tables
      .filter((t) => t.schema === schema)
      .map((t) => `${t.schema}.${t.table}`);
  }

  // ── Toggle schema — selects / deselects all its tables too ────────────────
  function toggleSchema(schema: string) {
    const tables = tablesForSchema(schema);
    setSelectedSchemas((prev) => {
      const next = new Set(prev);
      if (next.has(schema)) {
        next.delete(schema);
        setSelectedTables((pt) => {
          const nt = new Set(pt);
          tables.forEach((t) => nt.delete(t));
          return nt;
        });
      } else {
        next.add(schema);
        setSelectedTables((pt) => {
          const nt = new Set(pt);
          tables.forEach((t) => nt.add(t));
          return nt;
        });
      }
      return next;
    });
  }

  // ── Toggle individual table ────────────────────────────────────────────────
  function toggleTable(schema: string, qualifiedTable: string) {
    setSelectedTables((prev) => {
      const next = new Set(prev);
      if (next.has(qualifiedTable)) {
        next.delete(qualifiedTable);
        setSelectedSchemas((ps) => {
          const ns = new Set(ps);
          ns.delete(schema);
          return ns;
        });
      } else {
        next.add(qualifiedTable);
        const allTables = tablesForSchema(schema);
        if (allTables.every((t) => t === qualifiedTable || prev.has(t))) {
          setSelectedSchemas((ps) => new Set([...ps, schema]));
        }
      }
      return next;
    });
  }

  function toggleExpanded(schema: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(schema) ? next.delete(schema) : next.add(schema);
      return next;
    });
  }

  // ── Save scope declaration ─────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (viewerOnly) return;
    setSaving(true);
    try {
      await apiPost<ScopeResponse>('/api/db-connectors/oracle_db/scope', {
        schemas: [...selectedSchemas],
        tables: [...selectedTables],
      });
      push(
        selectedSchemas.size > 0
          ? `Scope saved — ${selectedSchemas.size} schema(s), ${selectedTables.size} table(s) declared.`
          : 'Scope declaration cleared.',
      );
      onSaved?.();
    } catch (err) {
      push(extractSaveError(err));
    } finally {
      setSaving(false);
    }
  }, [selectedSchemas, selectedTables, viewerOnly, push, onSaved]);

  // ── Loading state ──────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div className="mt-4 text-xs text-muted animate-pulse">
        Loading schema discovery…
      </div>
    );
  }

  // ── Error / empty state — stays local, does not break parent panel ─────────
  if (loadError || !discovery || discovery.schemas.length === 0) {
    return (
      <div className="mt-4">
        <p className="text-xs text-muted mb-2">
          {loadError ?? 'No schemas discovered. Check your connection and try again.'}
        </p>
        <button
          type="button"
          onClick={loadData}
          className="text-xs text-accent underline underline-offset-2 hover:text-accent/80"
        >
          Retry
        </button>
      </div>
    );
  }

  // ── Normal render ──────────────────────────────────────────────────────────
  return (
    <div className="mt-4">
      {/* Section header */}
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-text">Oracle DB scope</div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          Scope declaration
        </div>
      </div>

      <p className="text-xs text-muted mb-1 leading-relaxed">
        Select schemas and tables AgentIQ may query. Selecting a schema without
        expanding allows any table in that schema.
      </p>

      {/* Oracle case-sensitivity tooltip — AC18 */}
      <p
        className="text-[11px] text-muted/70 mb-3 leading-relaxed italic"
        title="Oracle schema names are case-sensitive — shown exactly as stored in the database."
      >
        Oracle schema names are case-sensitive — shown exactly as stored in the
        database.
      </p>

      {/* Schema → table tree */}
      <div role="group" aria-label="Oracle DB scope" className="space-y-1">
        {discovery.schemas.map((schema) => {
          const tables     = tablesForSchema(schema);
          const isSelected = selectedSchemas.has(schema);
          const isExpanded = expanded.has(schema);

          return (
            <div
              key={schema}
              className="rounded-lg border border-border overflow-hidden"
            >
              {/* Schema row */}
              <div className="flex items-center gap-2 px-3 py-2 bg-panel hover:bg-panel2">
                <button
                  type="button"
                  role="checkbox"
                  aria-checked={isSelected}
                  onClick={() => toggleSchema(schema)}
                  className={[
                    'h-3.5 w-3.5 flex-shrink-0 rounded border flex items-center justify-center',
                    isSelected ? 'border-accent bg-accent' : 'border-border',
                  ].join(' ')}
                >
                  {isSelected && (
                    <svg width="8" height="8" viewBox="0 0 8 8" fill="none" aria-hidden>
                      <path
                        d="M1.5 4L3 5.5L6.5 2"
                        stroke="white"
                        strokeWidth="1.2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      />
                    </svg>
                  )}
                </button>

                <span
                  className={`flex-1 text-xs font-medium ${
                    isSelected ? 'text-accent' : 'text-text'
                  }`}
                >
                  {schema}
                </span>

                {tables.length > 0 && (
                  <button
                    type="button"
                    aria-expanded={isExpanded}
                    onClick={() => toggleExpanded(schema)}
                    className="text-muted hover:text-text text-[10px] px-1 shrink-0"
                  >
                    {isExpanded ? '▲' : '▼'}{' '}
                    {tables.length} table{tables.length !== 1 ? 's' : ''}
                  </button>
                )}
              </div>

              {/* Table rows */}
              {isExpanded && tables.length > 0 && (
                <div className="border-t border-border bg-bg/30">
                  {tables.map((qualified) => {
                    const tableName      = qualified.split('.')[1] ?? qualified;
                    const isTableSelected = selectedTables.has(qualified);

                    return (
                      <button
                        key={qualified}
                        type="button"
                        role="checkbox"
                        aria-checked={isTableSelected}
                        onClick={() => toggleTable(schema, qualified)}
                        className="w-full flex items-center gap-2 px-5 py-1.5 text-left hover:bg-panel2"
                      >
                        <span
                          className={[
                            'h-3 w-3 flex-shrink-0 rounded border flex items-center justify-center',
                            isTableSelected
                              ? 'border-accent bg-accent'
                              : 'border-border',
                          ].join(' ')}
                        >
                          {isTableSelected && (
                            <svg
                              width="7"
                              height="7"
                              viewBox="0 0 8 8"
                              fill="none"
                              aria-hidden
                            >
                              <path
                                d="M1.5 4L3 5.5L6.5 2"
                                stroke="white"
                                strokeWidth="1.2"
                                strokeLinecap="round"
                                strokeLinejoin="round"
                              />
                            </svg>
                          )}
                        </span>
                        <span
                          className={`text-xs ${
                            isTableSelected ? 'text-accent' : 'text-muted'
                          }`}
                        >
                          {tableName}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Save button */}
      <button
        type="button"
        onClick={handleSave}
        disabled={saving || viewerOnly}
        title={viewerOnly ? 'Analyst role required' : undefined}
        className={[
          'mt-3 w-full rounded-lg px-4 py-2 text-sm font-medium',
          'border border-accent/20 bg-accent/5 text-accent transition-colors',
          'hover:border-accent/45 hover:bg-accent/10',
          'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40',
          saving || viewerOnly
            ? 'opacity-60 cursor-not-allowed'
            : 'cursor-pointer',
        ].join(' ')}
      >
        {saving ? 'Saving…' : 'Save scope declaration'}
      </button>

      {(selectedSchemas.size > 0 || selectedTables.size > 0) && (
        <p className="mt-2 text-center text-[11px] text-accent">
          {selectedSchemas.size} schema{selectedSchemas.size !== 1 ? 's' : ''}
          {', '}
          {selectedTables.size} table{selectedTables.size !== 1 ? 's' : ''}{' '}
          declared
        </p>
      )}
    </div>
  );
}
