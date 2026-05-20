/**
 * SalesforceProductPicker — ENG-IH-3 Sprint 9
 *
 * Rendered inside ConnectorDetailPanel when connector.id === 'salesforce'
 * and connector.status === 'connected'.
 *
 * Replaces the cloud expansion panel that previously lived in
 * Stack Builder Screen 2 (YourSystemsScreen.tsx). This is now a
 * workspace-level declaration — not a per-discovery choice.
 *
 * What it does:
 *   - Shows 6 Salesforce cloud product options as toggle pills
 *   - Saves selection via PATCH /api/connectors/salesforce/products
 *   - On mount: loads current selection via GET /api/connectors/salesforce/products
 *   - Shows a save confirmation toast on success
 *
 * The saved products are stored on the connector record and read by:
 *   - GET /api/integration-hub/workspace-catalog (ENG-IH-1)
 *     → salesforce.products field
 *   - useSetupState (ENG-SB-2)
 *     → pre-populates selectedSalesforceClouds on Stack Builder mount
 *
 * ENG-IH-3 AC3: Stack Builder Screen 2 reads salesforce.products from
 * the workspace catalog — no cloud picker in Screen 2.
 *
 * Accessibility:
 *   role="group" aria-label="Salesforce products"
 *   Each toggle: role="checkbox" aria-checked
 *   Save button: disabled while loading
 *
 * Props:
 *   onSaved — called after successful save (parent can refetch connector)
 */
import React, { useCallback, useEffect, useState } from 'react';
import { useToast } from '../common/Toast';
import { ApiError, apiGet, apiPatch } from '../../lib/apiClient';

// ── Product definitions ───────────────────────────────────────────────────────

interface SalesforceProduct {
  id:    string;
  label: string;
  desc:  string;
}

const SALESFORCE_PRODUCTS: SalesforceProduct[] = [
  { id: 'salesforce_pss',   label: 'Public Sector Solutions / Benefits',
    desc: 'Benefits administration, member services, PSS objects' },
  { id: 'salesforce_sc',    label: 'Service Cloud',
    desc: 'Case management, service requests, SLAs' },
  { id: 'salesforce_ncino', label: 'nCino',
    desc: 'Commercial lending, loan origination, covenants' },
  { id: 'salesforce_fsc',   label: 'Financial Services Cloud',
    desc: 'Wealth management, relationship banking' },
  { id: 'salesforce_rc',    label: 'Revenue Cloud',
    desc: 'CPQ, contracts, revenue operations' },
  { id: 'salesforce_hc',    label: 'Health Cloud',
    desc: 'Patient management, care programmes' },
];

// ── Component ─────────────────────────────────────────────────────────────────

interface Props {
  onSaved?: () => void;
}

interface SalesforceProductsResponse {
  ok: boolean;
  products: string[];
  labels: string[];
}

function getSaveErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const body = error.body as { detail?: unknown };
    return typeof body?.detail === 'string'
      ? body.detail
      : 'Failed to save product declaration.';
  }
  return 'Network error saving product declaration. Please try again.';
}

export default function SalesforceProductPicker({ onSaved }: Props) {
  const { push }  = useToast();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading,  setLoading]  = useState(true);
  const [saving,   setSaving]   = useState(false);

  // Load current declaration on mount
  useEffect(() => {
    setLoading(true);
    apiGet<SalesforceProductsResponse>('/api/connectors/salesforce/products')
      .then(data => {
        if (data?.products) {
          setSelected(new Set(data.products));
        }
      })
      .catch(() => {
        // Silent failure — empty selection is safe default
      })
      .finally(() => setLoading(false));
  }, []);

  function toggleProduct(id: string) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      const data = await apiPatch<SalesforceProductsResponse>(
        '/api/connectors/salesforce/products',
        { products: [...selected] },
      );
      setSelected(new Set(data.products));
      push(
        data.products.length > 0
          ? `Saved ${data.products.length} Salesforce product${data.products.length > 1 ? 's' : ''}.`
          : 'Product declaration cleared.',
      );
      onSaved?.();
      // Reload page to refresh catalog in Stack Builder
      setTimeout(() => window.location.reload(), 500);
    } catch (error) {
      push(getSaveErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }, [selected, push, onSaved]);

  if (loading) {
    return (
      <div className="mt-4 text-xs text-muted animate-pulse">
        Loading product declaration…
      </div>
    );
  }

  return (
    <div className="mt-4">
      {/* Section header */}
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-text">
          Salesforce products in use
        </div>
        <div className="text-[10px] font-medium uppercase tracking-wide text-muted">
          Workspace declaration
        </div>
      </div>

      <p className="text-xs text-muted mb-3 leading-relaxed">
        Select the Salesforce products your workspace uses. This declaration
        is shared across all discovery runs — Stack Builder will pre-select
        these automatically.
      </p>

      {/* Product toggles */}
      <div
        role="group"
        aria-label="Salesforce products"
        className="space-y-1.5"
      >
        {SALESFORCE_PRODUCTS.map(product => {
          const isSelected = selected.has(product.id);
          return (
            <button
              key={product.id}
              type="button"
              role="checkbox"
              aria-checked={isSelected}
              onClick={() => toggleProduct(product.id)}
              className={[
                'w-full flex items-start gap-3 rounded-lg border px-3 py-2.5',
                'text-left transition-[border-color,background-color,box-shadow] cursor-pointer',
                'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/35',
                isSelected
                  ? 'border-accent bg-accent/10 shadow-[0_8px_22px_rgba(13,85,215,0.14)]'
                  : 'border-border bg-panel hover:border-accent/40',
              ].join(' ')}
            >
              {/* Checkbox indicator */}
              <div className={[
                'mt-0.5 h-3.5 w-3.5 flex-shrink-0 rounded border flex items-center justify-center',
                isSelected
                  ? 'border-accent bg-accent'
                  : 'border-border',
              ].join(' ')}>
                {isSelected && (
                  <svg width="8" height="8" viewBox="0 0 8 8" fill="none" aria-hidden>
                    <path d="M1.5 4L3 5.5L6.5 2" stroke="white"
                      strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                )}
              </div>

              <div className="min-w-0 flex-1">
                <div className={`text-xs font-medium ${
                  isSelected ? 'text-accent' : 'text-text'
                }`}>
                  {product.label}
                </div>
                <div className="text-[11px] text-muted leading-relaxed mt-0.5">
                  {product.desc}
                </div>
              </div>
            </button>
          );
        })}
      </div>

      {/* Save button */}
      <button
        type="button"
        onClick={handleSave}
        disabled={saving}
        className={[
          'mt-3 w-full rounded-lg px-4 py-2 text-sm font-medium',
          'border border-accent/20 bg-accent/5 text-accent transition-colors hover:border-accent/45 hover:bg-accent/10',
          'focus:outline-none focus-visible:ring-1 focus-visible:ring-accent/40',
          saving ? 'opacity-60 cursor-not-allowed' : 'cursor-pointer',
        ].join(' ')}
      >
        {saving ? 'Saving…' : 'Save product declaration'}
      </button>

      {selected.size > 0 && (
        <p className="mt-2 text-center text-[11px] text-accent">
          {selected.size} product{selected.size > 1 ? 's' : ''} declared
        </p>
      )}
    </div>
  );
}
