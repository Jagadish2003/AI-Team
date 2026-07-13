/**
 * StackBuilderRegistryDriven.test.tsx — R18-C1 T3 (Addendum A)
 *
 * The Stack Builder selection UI is registry-driven: industries and templates
 * come from the backend registry / template model, choosing Commercial Lending
 * pre-populates an EDITABLE configuration, and an API failure shows a retry
 * state rather than a stale local fallback.
 *
 * Coverage:
 *   AC1  — selecting Commercial Lending pre-populates systems, roles, and focus,
 *          all of which stay editable.
 *   AC5  — the selected template id is carried on the launch payload.
 *   AC7/AC8 — industries and templates render from the API data.
 *   AC9  — choosing an industry applies registry-driven system defaults via the
 *          API path (editable).
 *   AC10 — API failure shows a Retry state; no stale INDUSTRIES / TEMPLATES list
 *          is rendered.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import DiscoveryFocusPage from '../pages/DiscoveryFocusPage';
import { buildStackBuilderLaunchPayload } from '../pages/StackBuilderPage';
import { useSetupState } from '../components/stack_builder';
import type {
  IndustryListItem,
  TemplateListItem,
  SystemDefaultItem,
  SetupState,
} from '../types/stack_builder';

// ── Registry fixtures (shape mirrors the backend Pydantic responses) ──────────

const INDUSTRIES: IndustryListItem[] = [
  { industry_id: 'financial_services', label: 'Financial services', pack_hints: ['ncino'], recommended_systems: ['jira'] },
  { industry_id: 'public_sector', label: 'Public sector', pack_hints: ['service_cloud'], recommended_systems: [] },
];

const LENDING: TemplateListItem = {
  template_id: 'commercial_lending',
  label: 'Commercial lending',
  description: 'Commercial lending starting point.',
  suggested_systems: ['salesforce_ncino', 'jira', 'servicenow', 'confluence'],
  suggested_roles: {
    salesforce_ncino: 'system_of_record',
    jira: 'workflow_system',
    servicenow: 'workflow_system',
    confluence: 'documentation_system',
  },
  focus_defaults: { focus_id: 'approvals_compliance', emphasis: ['approvals', 'compliance_risk'] },
  pack_id: 'ncino',
  detector_emphasis: ['COVENANT_TRACKING_GAP'],
  terminology: { customer: 'borrower' },
  metadata: { industry_id: 'financial_services' },
};

const TEMPLATES: TemplateListItem[] = [LENDING];

// ── Test harness: exposes live setup state to the DOM ─────────────────────────

interface HarnessProps {
  industries?: IndustryListItem[];
  templates?: TemplateListItem[];
  registryLoading?: boolean;
  registryError?: string | null;
  onRetryRegistry?: () => void;
  fetchSystemDefaults?: (industryId: string) => Promise<SystemDefaultItem[]>;
}

function Harness({
  industries = INDUSTRIES,
  templates = TEMPLATES,
  registryLoading = false,
  registryError = null,
  onRetryRegistry = vi.fn(),
  fetchSystemDefaults = vi.fn(async () => []),
}: HarnessProps) {
  const setupState = useSetupState();
  const { state } = setupState;
  return (
    <div>
      <DiscoveryFocusPage
        setupState={setupState}
        industries={industries}
        templates={templates}
        registryLoading={registryLoading}
        registryError={registryError}
        onRetryRegistry={onRetryRegistry}
        fetchSystemDefaults={fetchSystemDefaults}
      />
      <div data-testid="focus">{state.focusId ?? ''}</div>
      <div data-testid="template">{state.templateId ?? ''}</div>
      <div data-testid="systems">{state.selectedSystemIds.join(',')}</div>
      <div data-testid="ncino-role">{state.weightings['salesforce_ncino']?.role ?? ''}</div>
      <div data-testid="ncino-confirmed">
        {String(state.weightings['salesforce_ncino']?.confirmed ?? '')}
      </div>
    </div>
  );
}

// ── AC7 / AC8: render from the registry ───────────────────────────────────────

describe('R18-C1 T3 — industries and templates render from the registry API', () => {
  it('renders an industry pill per registry industry', () => {
    render(<Harness />);
    expect(screen.getByRole('checkbox', { name: 'Financial services' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Public sector' })).toBeInTheDocument();
  });

  it('renders a template pill per registry template', () => {
    render(<Harness />);
    expect(screen.getByRole('checkbox', { name: 'Commercial lending' })).toBeInTheDocument();
  });
});

// ── AC10: API failure shows retry, never a stale list ─────────────────────────

describe('R18-C1 T3 — registry API failure shows a retry state', () => {
  it('shows Retry and no template/industry pills when the registry failed', () => {
    render(
      <Harness
        industries={[]}
        templates={[]}
        registryError="Could not load industries and templates from the registry. Please retry."
      />,
    );

    // No stale hardcoded lists rendered.
    expect(screen.queryByRole('checkbox', { name: 'Commercial lending' })).toBeNull();
    expect(screen.queryByRole('checkbox', { name: 'Financial services' })).toBeNull();

    // Retry offered on both pickers.
    const retries = screen.getAllByRole('button', { name: 'Retry' });
    expect(retries.length).toBeGreaterThanOrEqual(1);
  });

  it('invokes the retry callback when Retry is clicked', () => {
    const onRetryRegistry = vi.fn();
    render(
      <Harness
        industries={[]}
        templates={[]}
        registryError="Registry unavailable"
        onRetryRegistry={onRetryRegistry}
      />,
    );

    fireEvent.click(screen.getAllByRole('button', { name: 'Retry' })[0]);
    expect(onRetryRegistry).toHaveBeenCalledTimes(1);
  });
});

// ── AC1: Commercial Lending pre-populates an editable configuration ───────────

describe('R18-C1 T3 — Commercial Lending pre-populates editable defaults', () => {
  it('pre-populates focus, systems, and roles from the template', () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole('checkbox', { name: 'Commercial lending' }));

    expect(screen.getByTestId('template').textContent).toBe('commercial_lending');
    expect(screen.getByTestId('focus').textContent).toBe('approvals_compliance');

    const systems = screen.getByTestId('systems').textContent ?? '';
    expect(systems).toContain('salesforce_ncino');
    expect(systems).toContain('jira');
    expect(systems).toContain('servicenow');
    expect(systems).toContain('confluence');

    // Role pre-filled from the template's suggested_roles…
    expect(screen.getByTestId('ncino-role').textContent).toBe('system_of_record');
    // …but NOT confirmed, so Step 3 still asks the user (editable).
    expect(screen.getByTestId('ncino-confirmed').textContent).toBe('false');
  });

  it('keeps the pre-filled focus editable — picking another focus wins', () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole('checkbox', { name: 'Commercial lending' }));
    expect(screen.getByTestId('focus').textContent).toBe('approvals_compliance');

    // Focus tiles are radios; choosing a different lens overrides the template.
    // Target the title node precisely (the phrase "Core operations" also appears
    // inside another card's boundary copy, so a role-name regex is ambiguous).
    const coreTitle = screen.getByText('Core operations');
    const coreRadio = coreTitle.closest('[role="radio"]') as HTMLElement;
    fireEvent.click(coreRadio);
    expect(screen.getByTestId('focus').textContent).toBe('core_operations');
    // Template stays selected — only the focus changed.
    expect(screen.getByTestId('template').textContent).toBe('commercial_lending');
  });

  it('toggling the template off removes its systems (editable)', () => {
    render(<Harness />);
    const lendingPill = screen.getByRole('checkbox', { name: 'Commercial lending' });

    fireEvent.click(lendingPill);
    expect(screen.getByTestId('systems').textContent).toContain('salesforce_ncino');

    fireEvent.click(lendingPill);
    expect(screen.getByTestId('template').textContent).toBe('');
    expect(screen.getByTestId('systems').textContent).not.toContain('salesforce_ncino');
  });
});

// ── AC9: choosing an industry applies registry system defaults via the API ────

describe('R18-C1 T3 — industry selection applies registry defaults via the API', () => {
  it('fetches and applies industry system defaults to selected systems (editable)', async () => {
    const fetchSystemDefaults = vi.fn<(id: string) => Promise<SystemDefaultItem[]>>(async () => [
      { system_id: 'salesforce_ncino', role: 'workflow_system', priority: 'optional', workflow_focus: ['approvals'] },
    ]);

    render(<Harness fetchSystemDefaults={fetchSystemDefaults} />);

    // Select the template first so salesforce_ncino is a selected system.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Commercial lending' }));
    expect(screen.getByTestId('ncino-role').textContent).toBe('system_of_record');

    // Choosing an industry pulls its calibrated defaults through the API path
    // and overwrites the unconfirmed weighting.
    fireEvent.click(screen.getByRole('checkbox', { name: 'Financial services' }));

    await waitFor(() =>
      expect(fetchSystemDefaults).toHaveBeenCalledWith('financial_services'),
    );
    await waitFor(() =>
      expect(screen.getByTestId('ncino-role').textContent).toBe('workflow_system'),
    );
  });
});

// ── AC5: the selected template id is carried on the launch payload ────────────

describe('R18-C1 T3 — the launch payload carries the template id (AC5)', () => {
  function baseState(overrides: Partial<SetupState> = {}): SetupState {
    return {
      focusId: 'approvals_compliance',
      industryId: 'financial_services',
      templateId: null,
      templatePreselectedIds: [],
      selectedSystemIds: ['salesforce_ncino'],
      selectedSalesforceClouds: [],
      weightings: {
        salesforce_ncino: {
          systemId: 'salesforce_ncino',
          role: 'system_of_record',
          priority: 'primary',
          workflowFocus: ['approvals'],
          confirmed: true,
        },
      },
      currentStep: 4,
      ...overrides,
    };
  }

  it('sends the selected template id', () => {
    const payload = buildStackBuilderLaunchPayload(
      baseState({ templateId: 'commercial_lending' }),
      'ncino',
      'default',
    );
    expect(payload.template_id).toBe('commercial_lending');
  });

  it('sends null when no template is selected', () => {
    const payload = buildStackBuilderLaunchPayload(baseState(), 'ncino', 'default');
    expect(payload.template_id).toBeNull();
  });
});
