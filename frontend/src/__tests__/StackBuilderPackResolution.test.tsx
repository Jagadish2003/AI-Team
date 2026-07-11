/**
 * resolvePackId — the Salesforce product declaration is authoritative.
 *
 * R18-A3 follow-up: the discovery pack for Salesforce is driven ONLY by the
 * workspace product declaration (Integration Hub → "Salesforce products in
 * use"), surfaced here via the workspace catalog. Stack Builder system
 * pre-selection (selectedSystemIds / selectedSalesforceClouds) must NOT
 * influence the pack — a default preselect of `salesforce_ncino` was silently
 * forcing the nCino pack on orgs that declared nothing, so runs picked nCino
 * detectors against orgs with no nCino package (every LLC_BI__* query 400s).
 */
import { describe, expect, it } from 'vitest';
import { resolvePackId } from '../pages/StackBuilderPage';
import type { WorkspaceCatalogResponse } from '../types/workspace_catalog';

function catalogWith(salesforceProducts: string[]): WorkspaceCatalogResponse {
  return {
    primary_platforms: [
      {
        system_id: 'salesforce',
        name: 'Salesforce',
        status: 'connected',
        products: salesforceProducts,
      },
    ],
    operational_systems: [],
    comms_knowledge: [],
    data_engineering: [],
    missing_categories: [],
  };
}

// resolvePackId only reads state.industryId now; the two system arrays are
// included to prove they are ignored. Cast keeps the test independent of the
// full SetupState shape.
function stateWith(overrides: Partial<{
  industryId: string | null;
  selectedSystemIds: string[];
  selectedSalesforceClouds: string[];
}> = {}): any {
  return {
    industryId: null,
    selectedSystemIds: [],
    selectedSalesforceClouds: [],
    ...overrides,
  };
}

describe('resolvePackId — declaration authoritative', () => {
  it('uses the declared Salesforce product from the catalog', () => {
    expect(resolvePackId(stateWith(), catalogWith(['salesforce_sc']))).toBe('service_cloud');
    expect(resolvePackId(stateWith(), catalogWith(['salesforce_ncino']))).toBe('ncino');
    expect(resolvePackId(stateWith(), catalogWith(['salesforce_pss']))).toBe('strs_benefits');
  });

  it('IGNORES Stack Builder system pre-selection (the nCino footgun)', () => {
    // salesforce_ncino pre-selected as a system AND as a cloud, but nothing
    // declared in the catalog → must NOT resolve to ncino.
    const state = stateWith({
      selectedSystemIds: ['salesforce_ncino', 'jira', 'servicenow'],
      selectedSalesforceClouds: ['salesforce_ncino'],
    });
    expect(resolvePackId(state, catalogWith([]))).toBe('service_cloud');
    expect(resolvePackId(state, null)).toBe('service_cloud');
  });

  it('falls back to the industry hint only when nothing is declared', () => {
    // financial_services → hints[0] === 'ncino'
    expect(resolvePackId(stateWith({ industryId: 'financial_services' }), catalogWith([]))).toBe('ncino');
  });

  it('lets the declaration win over the industry hint', () => {
    const state = stateWith({ industryId: 'financial_services' }); // hint would be ncino
    expect(resolvePackId(state, catalogWith(['salesforce_sc']))).toBe('service_cloud');
  });

  it('defaults to service_cloud when nothing is declared and no industry is set', () => {
    expect(resolvePackId(stateWith(), catalogWith([]))).toBe('service_cloud');
    expect(resolvePackId(stateWith(), null)).toBe('service_cloud');
  });
});
