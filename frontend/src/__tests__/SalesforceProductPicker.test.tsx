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
const mockApiPost = vi.fn();

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
  // apiPost is unused by the picker but imported by the DB scope pickers that
  // ConnectorDetailPanel pulls in, so the module mock must export it.
  apiPost: (...args: unknown[]) => mockApiPost(...args),
}));

const mockPush = vi.fn();
vi.mock('../components/common/Toast', () => ({
  useToast: () => ({ push: mockPush }),
}));

import SalesforceProductPicker from '../components/integrations/SalesforceProductPicker';
import ConnectorDetailPanel from '../components/integrations/ConnectorDetailPanel';
import { Connector } from '../types/connector';

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

// ── Placement inside ConnectorDetailPanel ─────────────────────────────────────
// The product declaration must be the first content section of the Integration
// Hub right panel — above "Access as:" and Connection Health — so it is
// visible without scrolling when Salesforce is selected.

const salesforceConnector: Connector = {
  id: 'salesforce',
  name: 'Salesforce',
  category: 'CRM Platform',
  tier: 'recommended',
  status: 'connected',
  configured: true,
  metrics: [],
  lastSynced: '1 hour ago',
  reads: ['Cases', 'Flows', 'Approvals'],
  signalStrength: 80,
};

describe('SalesforceProductPicker placement in ConnectorDetailPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setupDefaultMocks();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('renders the Salesforce products section above "Access as:" and Connection Health', async () => {
    render(<ConnectorDetailPanel connector={salesforceConnector} onConfigure={vi.fn()} />);

    const products = await screen.findByText('Salesforce products in use');
    const accessAs = screen.getByText('Access as:');
    const health = screen.getByText('Connection Health');

    // DOCUMENT_POSITION_FOLLOWING: the argument comes after the receiver.
    expect(
      products.compareDocumentPosition(accessAs) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      products.compareDocumentPosition(health) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('does not render the products section when Salesforce is not connected', async () => {
    render(
      <ConnectorDetailPanel
        connector={{ ...salesforceConnector, status: 'not_connected', configured: false }}
        onConfigure={vi.fn()}
      />,
    );

    expect(screen.queryByText('Salesforce products in use')).not.toBeInTheDocument();
    expect(screen.getByText('Access as:')).toBeInTheDocument();
  });
});
