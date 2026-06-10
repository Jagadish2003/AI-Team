/**
 * SalesforceProductPicker — regression tests
 *
 * Primary guard:
 *   Saving the product declaration must NOT trigger a full page reload or
 *   navigation. The auth token lives in React state only (AuthContext Section 3),
 *   so a reload wipes the session and bounces the user to /login — which looked
 *   like an unexpected logout after clicking "Save product declaration".
 *   See SalesforceProductPicker.handleSave.
 *
 * Run:
 *   npx vitest run src/__tests__/SalesforceProductPicker.test.tsx
 */

import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// ── Mock API client before importing the component ────────────────────────────

const mockApiGet = vi.fn();
const mockApiPatch = vi.fn();

vi.mock('../lib/apiClient', () => ({
  ApiError: class ApiError extends Error {
    body: unknown;
    constructor(message: string, body: unknown) {
      super(message);
      this.body = body;
    }
  },
  apiGet: (...args: unknown[]) => mockApiGet(...args),
  apiPatch: (...args: unknown[]) => mockApiPatch(...args),
}));

const mockPush = vi.fn();
vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mockPush }),
}));

import SalesforceProductPicker from '../components/integrations/SalesforceProductPicker';

// ── Helpers ───────────────────────────────────────────────────────────────────

function setupDefaultMocks() {
  // No prior declaration → empty selection.
  mockApiGet.mockResolvedValue({ ok: true, products: [], labels: [] });
  mockApiPatch.mockResolvedValue({
    ok: true,
    products: ['salesforce_ncino'],
    labels: ['nCino'],
  });
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('SalesforceProductPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('does not reload or navigate the page after a successful save', async () => {
    // Spy on the navigation surfaces handleSave could touch. jsdom does not
    // implement reload/assign, so we replace the whole location object.
    const reload = vi.fn();
    const assign = vi.fn();
    const replace = vi.fn();
    const originalLocation = window.location;
    Object.defineProperty(window, 'location', {
      configurable: true,
      writable: true,
      value: { ...originalLocation, reload, assign, replace },
    });

    try {
      render(<SalesforceProductPicker />);

      // Wait for the on-mount load to settle (loading skeleton clears).
      const ncino = await screen.findByText('nCino');
      fireEvent.click(ncino);

      fireEvent.click(screen.getByText('Save product declaration'));

      // Save completes: PATCH fires and a confirmation toast is pushed.
      await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
      await waitFor(() => expect(mockPush).toHaveBeenCalled());

      // The session-killing calls must never happen.
      expect(reload).not.toHaveBeenCalled();
      expect(assign).not.toHaveBeenCalled();
      expect(replace).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, 'location', {
        configurable: true,
        writable: true,
        value: originalLocation,
      });
    }
  });

  it('sends the selected product in the PATCH and reflects the saved selection', async () => {
    render(<SalesforceProductPicker />);

    const ncino = await screen.findByText('nCino');
    fireEvent.click(ncino);
    fireEvent.click(screen.getByText('Save product declaration'));

    await waitFor(() => expect(mockApiPatch).toHaveBeenCalledTimes(1));
    expect(mockApiPatch).toHaveBeenCalledWith(
      '/api/connectors/salesforce/products',
      { products: ['salesforce_ncino'] },
    );
  });
});
