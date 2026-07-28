/**
 * R191-P1 T5 (AT-707) — multi-select run configuration (frontend).
 *
 * Run configuration can now activate MORE THAN ONE pack. `resolvePackIds` returns
 * the full ordered, de-duplicated pack list a run will activate, and the launch
 * payload carries `pack_ids` (the backend reconciles it with the singular
 * `pack_id`). A template declaring several packs activates them all; an explicit
 * Step-4 pack choice overrides; a single-pack template is unchanged.
 *
 * Covers AC5 (frontend half): a template declaring two packs activates both on
 * run creation.
 */
import { describe, expect, it } from 'vitest';
import {
  resolvePackIds,
  buildStackBuilderLaunchPayload,
} from '../pages/StackBuilderPage';
import type { WorkspaceCatalogResponse } from '../types/workspace_catalog';
import type { IndustryListItem, TemplateListItem } from '../types/stack_builder';

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

function template(overrides: Partial<TemplateListItem>): TemplateListItem {
  return {
    template_id: 'combined_ops',
    label: 'Combined operations',
    description: '',
    suggested_systems: [],
    suggested_roles: {},
    focus_defaults: { focus_id: 'core_operations', emphasis: [] },
    pack_id: 'service_cloud',
    detector_emphasis: [],
    terminology: {},
    metadata: {},
    ...overrides,
  };
}

function stateWith(overrides: Record<string, unknown> = {}): any {
  return {
    packId: null,
    templateId: null,
    industryId: null,
    selectedSystemIds: [],
    selectedSalesforceClouds: [],
    weightings: {},
    focusId: null,
    ...overrides,
  };
}

describe('resolvePackIds — multi-select run configuration', () => {
  it('activates every pack a template declares', () => {
    const templates = [
      template({ pack_id: 'service_cloud', packs: ['service_cloud', 'enterprise_ops'] }),
    ];
    const ids = resolvePackIds(
      stateWith({ templateId: 'combined_ops' }), emptyCatalog(), INDUSTRIES, templates,
    );
    expect(ids).toEqual(['service_cloud', 'enterprise_ops']);
  });

  it('de-duplicates a template packs list, order-preserving', () => {
    const templates = [
      template({ packs: ['service_cloud', 'enterprise_ops', 'service_cloud'] }),
    ];
    const ids = resolvePackIds(
      stateWith({ templateId: 'combined_ops' }), emptyCatalog(), INDUSTRIES, templates,
    );
    expect(ids).toEqual(['service_cloud', 'enterprise_ops']);
  });

  it('an explicit Step-4 pack choice overrides the template packs', () => {
    const templates = [
      template({ packs: ['service_cloud', 'enterprise_ops'] }),
    ];
    const ids = resolvePackIds(
      stateWith({ templateId: 'combined_ops', packId: 'ncino' }),
      emptyCatalog(), INDUSTRIES, templates,
    );
    expect(ids).toEqual(['ncino']);
  });

  it('falls back to a single pack for a single-pack template', () => {
    const templates = [template({ pack_id: 'ncino', packs: ['ncino'] })];
    const ids = resolvePackIds(
      stateWith({ templateId: 'combined_ops' }), emptyCatalog(), INDUSTRIES, templates,
    );
    expect(ids).toEqual(['ncino']);
  });

  it('falls back to the safe default when nothing is selected', () => {
    const ids = resolvePackIds(stateWith(), emptyCatalog(), INDUSTRIES, []);
    expect(ids).toEqual(['service_cloud']);
  });

  it('maps every declared Salesforce product to its pack (multi-select declaration)', () => {
    // Service Cloud + nCino declared → both packs activate.
    const ids = resolvePackIds(
      stateWith(),
      catalogWithSalesforceProducts(['salesforce_sc', 'salesforce_ncino']),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud', 'ncino']);
  });

  it('de-duplicates when several declared products map to the same pack', () => {
    // Service Cloud + Financial Services Cloud + nCino → service_cloud once, then ncino.
    const ids = resolvePackIds(
      stateWith(),
      catalogWithSalesforceProducts(['salesforce_sc', 'salesforce_fsc', 'salesforce_ncino']),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud', 'ncino']);
  });

  it('treats a template without a packs field as single-pack (pre-v1.11)', () => {
    const templates = [template({ pack_id: 'service_cloud', packs: undefined })];
    const ids = resolvePackIds(
      stateWith({ templateId: 'combined_ops' }), emptyCatalog(), INDUSTRIES, templates,
    );
    expect(ids).toEqual(['service_cloud']);
  });
});

describe('buildStackBuilderLaunchPayload — carries pack_ids', () => {
  it('sends the full pack_ids list plus the primary pack_id', () => {
    const payload = buildStackBuilderLaunchPayload(
      stateWith({ selectedSystemIds: [] }),
      'service_cloud',
      'org-1',
      ['service_cloud', 'enterprise_ops'],
    );
    expect(payload.pack_id).toBe('service_cloud');
    expect(payload.pack_ids).toEqual(['service_cloud', 'enterprise_ops']);
  });

  it('defaults pack_ids to the singular pack when not provided', () => {
    const payload = buildStackBuilderLaunchPayload(stateWith(), 'ncino', 'org-1');
    expect(payload.pack_ids).toEqual(['ncino']);
  });
});
