/**
 * Test helper: render UI wrapped in a DataCacheProvider.
 *
 * Phase 2+ of the reactive-data refactor converts context internals to use the
 * shared cache. The few tests that render a data context directly need a
 * DataCacheProvider ancestor; wrap them with this. (Pure component tests do NOT
 * need it — useResource/useDataCache are inert outside a provider by design.)
 */
import React from 'react';
import { render, type RenderOptions } from '@testing-library/react';
import { DataCacheProvider } from '../lib/dataCache';

export function renderWithCache(ui: React.ReactElement, options?: RenderOptions) {
  return render(<DataCacheProvider>{ui}</DataCacheProvider>, options);
}

export function withCache(ui: React.ReactNode): React.ReactElement {
  return <DataCacheProvider>{ui}</DataCacheProvider>;
}
