/**
 * Cloud-events → Cloud Ops analysis-pack default.
 *
 * Selecting an AWS/Azure Events connector on Stack Builder Step 2 pre-selects the
 * `cloud_ops` analysis pack on Step 4. This is not cosmetic: the discovery runner
 * only polls those connectors when a cloud_ops pack is selected
 * (`if _any_cloud_ops and "aws_events" in _systems` — discovery/runner.py), so with
 * the dropdown left at its old "None" default a connected cloud event source was
 * never read and the run produced no cloud findings at all, silently.
 *
 * Two properties are locked here:
 *   1. The default reaches the LAUNCH (resolvePackIds → pack_ids), not just the
 *      menu — the dropdown and the run always agree because both derive from
 *      resolveAnalysisPackId.
 *   2. It is a starting value, not a lock: once the user uses the dropdown their
 *      choice wins, INCLUDING "None".
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { resolvePackIds } from '../pages/StackBuilderPage';
import DiscoveryPlanPage from '../pages/DiscoveryPlanPage';
import { useSetupState } from '../components/stack_builder';
import {
  CLOUD_EVENT_SYSTEM_IDS,
  cloudOpsDefaultApplies,
  resolveAnalysisPackId,
} from '../data/analysisPacks';
import type { WorkspaceCatalogResponse } from '../types/workspace_catalog';
import type { IndustryListItem } from '../types/stack_builder';

function emptyCatalog(): WorkspaceCatalogResponse {
  return {
    primary_platforms: [],
    operational_systems: [],
    comms_knowledge: [],
    data_engineering: [],
    missing_categories: [],
  };
}

function catalogWithSalesforceProducts(products: string[]): WorkspaceCatalogResponse {
  return {
    ...emptyCatalog(),
    primary_platforms: [
      { system_id: 'salesforce', name: 'Salesforce', status: 'connected', products },
    ],
  } as WorkspaceCatalogResponse;
}

const INDUSTRIES: IndustryListItem[] = [];

function stateWith(overrides: Record<string, unknown> = {}): any {
  return {
    packId: null,
    packIds: [],
    templateId: null,
    industryId: null,
    selectedSystemIds: [],
    selectedSalesforceClouds: [],
    weightings: {},
    focusId: null,
    ...overrides,
  };
}

describe('cloudOpsDefaultApplies — which Step 2 selections imply Cloud Ops', () => {
  // The canonical MSP-B13 connector ids plus the legacy template-suggestion ids,
  // all four of which are selectable on Step 2 (YourSystemsPage SYSTEM_DISPLAY).
  it.each([...CLOUD_EVENT_SYSTEM_IDS])('applies for %s', systemId => {
    expect(cloudOpsDefaultApplies([systemId])).toBe(true);
  });

  it('applies when a cloud connector sits among other systems', () => {
    expect(cloudOpsDefaultApplies(['salesforce', 'servicenow', 'azure_events'])).toBe(true);
  });

  it('does not apply without a cloud event connector', () => {
    expect(cloudOpsDefaultApplies(['salesforce', 'servicenow', 'jira'])).toBe(false);
  });

  it('does not apply to an empty or absent selection', () => {
    expect(cloudOpsDefaultApplies([])).toBe(false);
    expect(cloudOpsDefaultApplies(undefined)).toBe(false);
  });
});

describe('resolveAnalysisPackId — the analysis slot', () => {
  it('defaults to cloud_ops for an untouched slot with a cloud connector', () => {
    expect(resolveAnalysisPackId([], ['aws_events'], false)).toBe('cloud_ops');
  });

  it('is None for an untouched slot with no cloud connector', () => {
    expect(resolveAnalysisPackId([], ['salesforce'], false)).toBe('');
  });

  it('yields an explicitly chosen pack over the default', () => {
    expect(resolveAnalysisPackId(['github_engineering'], ['aws_events'], true))
      .toBe('github_engineering');
  });

  it('respects a deliberate None — the default must not re-apply over it', () => {
    expect(resolveAnalysisPackId([], ['aws_events'], true)).toBe('');
  });

  it('ignores non-analysis packs on the list when finding the slot', () => {
    // service_cloud / ncino are Salesforce packs fixed by the product declaration;
    // they live on packIds but are never the analysis slot, so the default applies.
    expect(resolveAnalysisPackId(['service_cloud', 'ncino'], ['aws_events'], false))
      .toBe('cloud_ops');
  });
});

describe('resolvePackIds — the default reaches the launched pack_ids', () => {
  it('adds cloud_ops when a cloud event connector is selected', () => {
    const ids = resolvePackIds(
      stateWith({ selectedSystemIds: ['aws_events'] }),
      emptyCatalog(),
      INDUSTRIES,
      [],
    );
    expect(ids).toContain('cloud_ops');
  });

  it('does not add cloud_ops without a cloud event connector', () => {
    const ids = resolvePackIds(
      stateWith({ selectedSystemIds: ['salesforce', 'jira'] }),
      catalogWithSalesforceProducts(['salesforce_sc']),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud']);
  });

  it('is additive — declared Salesforce packs are kept and stay first', () => {
    const ids = resolvePackIds(
      stateWith({ selectedSystemIds: ['salesforce', 'azure_events'] }),
      catalogWithSalesforceProducts(['salesforce_sc', 'salesforce_ncino']),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud', 'ncino', 'cloud_ops']);
  });

  it('never displaces the resolved fallback pack', () => {
    // No declaration and no selection would resolve to service_cloud alone; the
    // default appends to that rather than replacing it.
    const ids = resolvePackIds(
      stateWith({ selectedSystemIds: ['aws_events'] }),
      emptyCatalog(),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud', 'cloud_ops']);
  });

  it('does not duplicate a cloud_ops a template already contributed', () => {
    const ids = resolvePackIds(
      stateWith({ packIds: ['cloud_ops'], selectedSystemIds: ['aws_events'] }),
      emptyCatalog(),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['cloud_ops']);
  });

  it('omits cloud_ops once the user has deliberately chosen None', () => {
    const ids = resolvePackIds(
      stateWith({
        selectedSystemIds: ['aws_events'],
        packIds: [],
        analysisPackTouched: true,
      }),
      catalogWithSalesforceProducts(['salesforce_sc']),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud']);
  });

  it('honours a different analysis pack chosen over the default', () => {
    const ids = resolvePackIds(
      stateWith({
        selectedSystemIds: ['aws_events'],
        packIds: ['security_ops'],
        analysisPackTouched: true,
      }),
      emptyCatalog(),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['security_ops']);
  });
});

// ── The Step 4 dropdown itself ───────────────────────────────────────────────

function PlanHarness({ systemIds }: { systemIds: string[] }) {
  const setupState = useSetupState(null);
  const seeded = React.useRef(false);
  if (!seeded.current) {
    seeded.current = true;
    systemIds.forEach(id => setupState.toggleSystem(id));
  }
  return (
    <DiscoveryPlanPage
      setupState={setupState}
      industries={[]}
      templates={[]}
      activePackId="service_cloud"
      onLaunch={() => {}}
    />
  );
}

describe('Step 4 analysis-pack dropdown', () => {
  it('shows Cloud Ops pre-selected when a cloud event connector is selected', () => {
    render(<PlanHarness systemIds={['aws_events']} />);
    const select = screen.getByLabelText('Analysis pack') as HTMLSelectElement;
    expect(select.value).toBe('cloud_ops');
  });

  it('shows None when no cloud event connector is selected', () => {
    render(<PlanHarness systemIds={['servicenow']} />);
    const select = screen.getByLabelText('Analysis pack') as HTMLSelectElement;
    expect(select.value).toBe('');
  });

  it('lets the user turn the default off — None sticks', () => {
    render(<PlanHarness systemIds={['azure_events']} />);
    const select = screen.getByLabelText('Analysis pack') as HTMLSelectElement;
    expect(select.value).toBe('cloud_ops');

    fireEvent.change(select, { target: { value: '' } });
    expect(select.value).toBe('');
  });

  it('lets the user pick a different analysis pack over the default', () => {
    render(<PlanHarness systemIds={['aws_events']} />);
    const select = screen.getByLabelText('Analysis pack') as HTMLSelectElement;

    fireEvent.change(select, { target: { value: 'security_ops' } });
    expect(select.value).toBe('security_ops');
  });
});
