/**
 * Cross-page reactivity — declaring a Salesforce product refreshes the workspace
 * catalog (which drives Stack Builder's pack resolution) with no manual reload.
 *
 * This locks the flagship bug fix: SalesforceProductPicker.save invalidates
 * cacheKeys.workspaceCatalog, so any consumer reading the catalog through the
 * shared cache (StackBuilderPage in the app) refetches instantly. Here a probe
 * component stands in for that consumer.
 */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';

import { DataCacheProvider, useResource } from '../lib/dataCache';
import { cacheKeys } from '../lib/cacheKeys';
import SalesforceProductPicker from '../components/integrations/SalesforceProductPicker';

const apiGet = vi.fn();
const apiPatch = vi.fn();

vi.mock('../lib/apiClient', () => ({
  apiGet: (...args: unknown[]) => apiGet(...args),
  apiPatch: (...args: unknown[]) => apiPatch(...args),
  ApiError: class ApiError extends Error {},
}));

vi.mock('../context/AuthContext', () => ({
  useAuthOptional: () => ({ user: { role: 'owner' } }),
}));

vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: vi.fn() }),
}));

function CatalogProbe({ fetcher }: { fetcher: () => Promise<{ count: number }> }) {
  const { data } = useResource(cacheKeys.workspaceCatalog, fetcher);
  return <div data-testid="catalog-count">{data ? String(data.count) : 'none'}</div>;
}

describe('product declaration → workspace catalog reactivity', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPatch.mockReset();
  });

  it('refetches the workspace catalog when a product declaration is saved', async () => {
    apiGet.mockResolvedValue({ ok: true, products: [], labels: [] }); // picker's initial load
    apiPatch.mockResolvedValue({ ok: true, products: ['salesforce_sc'], labels: ['Service Cloud'] });

    const catalogFetcher = vi
      .fn()
      .mockResolvedValueOnce({ count: 1 })
      .mockResolvedValueOnce({ count: 2 }) as () => Promise<{ count: number }>;

    render(
      <DataCacheProvider>
        <CatalogProbe fetcher={catalogFetcher} />
        <SalesforceProductPicker />
      </DataCacheProvider>,
    );

    // Catalog loaded once.
    await waitFor(() => expect(screen.getByTestId('catalog-count').textContent).toBe('1'));

    // Declare Service Cloud and save. The product declaration is a multi-select
    // (checkbox) picker (R191-P1) — one product is enough for this reactivity check.
    const scCheckbox = await screen.findByRole('checkbox', { name: /Service Cloud/i });
    fireEvent.click(scCheckbox);
    fireEvent.click(screen.getByRole('button', { name: /save product declaration/i }));

    // The save invalidated the catalog key → the probe refetched with no reload.
    await waitFor(() => expect(screen.getByTestId('catalog-count').textContent).toBe('2'));
    expect(apiPatch).toHaveBeenCalledWith('/api/connectors/salesforce/products', {
      products: ['salesforce_sc'],
    });
    expect(catalogFetcher).toHaveBeenCalledTimes(2);
  });
});
