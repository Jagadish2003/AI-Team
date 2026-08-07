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
import { resolvePackId, resolvePackIds, resolvePrimaryPackId } from '../pages/StackBuilderPage';
import type { WorkspaceCatalogResponse } from '../types/workspace_catalog';
import type { IndustryListItem, TemplateListItem } from '../types/stack_builder';

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

// The industry pack hints come from the registry response (the `industries`
// argument), never a frontend mirror — financial_services hints to ncino.
const INDUSTRIES: IndustryListItem[] = [
  {
    industry_id: 'financial_services',
    label: 'Financial Services',
    pack_hints: ['ncino'],
    recommended_systems: [],
  },
];

// No template is selected in these cases, so the template registry is empty.
const TEMPLATES: TemplateListItem[] = [];

// After R18-A3 + R18-C1, resolvePackId reads state.packId / state.templateId /
// state.industryId; the two system arrays are included to prove they are ignored.
// Cast keeps the test independent of the full SetupState shape.
function stateWith(overrides: Partial<{
  packId: string | null;
  templateId: string | null;
  industryId: string | null;
  selectedSystemIds: string[];
  selectedSalesforceClouds: string[];
}> = {}): any {
  return {
    packId: null,
    templateId: null,
    industryId: null,
    selectedSystemIds: [],
    selectedSalesforceClouds: [],
    ...overrides,
  };
}

describe('resolvePackId — declaration authoritative', () => {
  it('uses the declared Salesforce product from the catalog', () => {
    expect(resolvePackId(stateWith(), catalogWith(['salesforce_sc']), INDUSTRIES, TEMPLATES)).toBe('service_cloud');
    expect(resolvePackId(stateWith(), catalogWith(['salesforce_ncino']), INDUSTRIES, TEMPLATES)).toBe('ncino');
    expect(resolvePackId(stateWith(), catalogWith(['salesforce_pss']), INDUSTRIES, TEMPLATES)).toBe('strs_benefits');
  });

  it('IGNORES Stack Builder system pre-selection (the nCino footgun)', () => {
    // salesforce_ncino pre-selected as a system AND as a cloud, but nothing
    // declared in the catalog → must NOT resolve to ncino.
    const state = stateWith({
      selectedSystemIds: ['salesforce_ncino', 'jira', 'servicenow'],
      selectedSalesforceClouds: ['salesforce_ncino'],
    });
    expect(resolvePackId(state, catalogWith([]), INDUSTRIES, TEMPLATES)).toBe('service_cloud');
    expect(resolvePackId(state, null, INDUSTRIES, TEMPLATES)).toBe('service_cloud');
  });

  it('falls back to the industry hint only when nothing is declared', () => {
    // financial_services → hints[0] === 'ncino'
    expect(resolvePackId(stateWith({ industryId: 'financial_services' }), catalogWith([]), INDUSTRIES, TEMPLATES)).toBe('ncino');
  });

  it('lets the declaration win over the industry hint', () => {
    const state = stateWith({ industryId: 'financial_services' }); // hint would be ncino
    expect(resolvePackId(state, catalogWith(['salesforce_sc']), INDUSTRIES, TEMPLATES)).toBe('service_cloud');
  });

  it('defaults to service_cloud when nothing is declared and no industry is set', () => {
    expect(resolvePackId(stateWith(), catalogWith([]), INDUSTRIES, TEMPLATES)).toBe('service_cloud');
    expect(resolvePackId(stateWith(), null, INDUSTRIES, TEMPLATES)).toBe('service_cloud');
  });
});

// ── #543: the primary pack must come FROM the resolved list ──────────────────
//
// `resolvePackId` ends in an unconditional `service_cloud` fallback, while
// `resolvePackIds` reaches its fallback only when the selection is EMPTY. Deriving
// the two independently therefore diverged: a user who chose only `cloud_ops` got
// pack_ids `['cloud_ops']` alongside pack_id `'service_cloud'`. The backend UNIONS
// them, so the run activated a Service Cloud pack nobody selected, and the
// Discovery Plan rendered `service_cloud` as active beside a list omitting it.
describe('#543 resolvePrimaryPackId — primary is always drawn from pack_ids', () => {
  function planState(overrides: Record<string, unknown> = {}): any {
    return {
      ...stateWith(),
      packIds: [],
      analysisPackTouched: true,
      ...overrides,
    };
  }

  it('never names a pack that is absent from the resolved list', () => {
    const state = planState({ packIds: ['cloud_ops'] });
    const packIds = resolvePackIds(state, catalogWith([]), [], TEMPLATES);
    const primary = resolvePrimaryPackId(state, catalogWith([]), [], TEMPLATES);

    expect(packIds).toContain(primary);
    // The specific regression: the standalone helper still says service_cloud.
    expect(primary).toBe('cloud_ops');
    expect(packIds).not.toContain('service_cloud');
  });

  it('does not smuggle service_cloud in via an industry hint either', () => {
    const state = planState({ packIds: ['cloud_ops'], industryId: 'financial_services' });
    const packIds = resolvePackIds(state, catalogWith([]), INDUSTRIES, TEMPLATES);
    const primary = resolvePrimaryPackId(state, catalogWith([]), INDUSTRIES, TEMPLATES);

    expect(packIds).toContain(primary);
    expect(packIds).not.toContain('ncino');
  });

  it('still falls back to service_cloud when nothing at all is selected', () => {
    // The fallback is correct HERE — an empty selection has to resolve to
    // something, and resolvePackIds and the primary must agree on what.
    const state = planState();
    const packIds = resolvePackIds(state, catalogWith([]), [], TEMPLATES);
    const primary = resolvePrimaryPackId(state, catalogWith([]), [], TEMPLATES);

    expect(primary).toBe('service_cloud');
    expect(packIds).toEqual(['service_cloud']);
  });

  it('keeps a declared Salesforce pack as the primary', () => {
    const state = planState({ packIds: ['cloud_ops'] });
    const packIds = resolvePackIds(state, catalogWith(['salesforce_sc']), [], TEMPLATES);
    const primary = resolvePrimaryPackId(state, catalogWith(['salesforce_sc']), [], TEMPLATES);

    // Salesforce packs lead the union, so the declaration stays primary.
    expect(primary).toBe('service_cloud');
    expect(packIds).toEqual(['service_cloud', 'cloud_ops']);
  });
});
