/**
 * R191-P1 (AT-707) — multi-pack run configuration (frontend).
 *
 * A run's packs are the UNION of:
 *   • the SALESFORCE packs, fixed by the Integration Hub product declaration
 *     (declared Salesforce products → packs via CLOUD_PACK_REGISTRY), and
 *   • the ANALYSIS packs, chosen per run in the Discovery Plan multi-select
 *     (held in state.packIds).
 * `resolvePackIds` returns that union (order-preserving, de-duplicated, Salesforce
 * packs first), and the launch payload carries it as `pack_ids`.
 */
import { describe, expect, it } from 'vitest';
import {
  resolvePackIds,
  buildStackBuilderLaunchPayload,
} from '../pages/StackBuilderPage';
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
    templateId: null,
    industryId: null,
    selectedSystemIds: [],
    selectedSalesforceClouds: [],
    weightings: {},
    focusId: null,
    ...overrides,
  };
}

describe('resolvePackIds — Salesforce packs ∪ analysis packs', () => {
  it('maps every declared Salesforce product to its pack (fixed from Integration Hub)', () => {
    const ids = resolvePackIds(
      stateWith(),
      catalogWithSalesforceProducts(['salesforce_sc', 'salesforce_ncino']),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud', 'ncino']);
  });

  it('de-duplicates when several declared products map to the same pack', () => {
    const ids = resolvePackIds(
      stateWith(),
      catalogWithSalesforceProducts(['salesforce_sc', 'salesforce_fsc', 'salesforce_ncino']),
      INDUSTRIES,
      [],
    );
    expect(ids).toEqual(['service_cloud', 'ncino']);
  });

  it('includes the chosen analysis packs (state.packIds)', () => {
    const ids = resolvePackIds(
      stateWith({ packIds: ['cloud_ops', 'github_engineering'] }),
      emptyCatalog(), INDUSTRIES, [],
    );
    expect(ids).toEqual(['cloud_ops', 'github_engineering']);
  });

  it('unions the fixed Salesforce packs with the chosen analysis packs (SF first)', () => {
    const ids = resolvePackIds(
      stateWith({ packIds: ['cloud_ops'] }),
      catalogWithSalesforceProducts(['salesforce_sc', 'salesforce_ncino']),
      INDUSTRIES, [],
    );
    expect(ids).toEqual(['service_cloud', 'ncino', 'cloud_ops']);
  });

  it('de-duplicates across Salesforce and analysis packs', () => {
    const ids = resolvePackIds(
      stateWith({ packIds: ['service_cloud', 'cloud_ops'] }),
      catalogWithSalesforceProducts(['salesforce_sc']),
      INDUSTRIES, [],
    );
    expect(ids).toEqual(['service_cloud', 'cloud_ops']);
  });

  it('falls back to the safe default when nothing is declared or selected', () => {
    const ids = resolvePackIds(stateWith(), emptyCatalog(), INDUSTRIES, []);
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
