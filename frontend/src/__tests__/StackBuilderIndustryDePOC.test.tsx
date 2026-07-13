/** R18-C0 P10 / R18-C1 AC7-AC10 - registry-owned pack-hint routing. */
import { describe, expect, it } from 'vitest';

import { resolvePackId } from '../pages/StackBuilderPage';
import type {
  IndustryListItem,
  SetupState,
  TemplateListItem,
} from '../types/stack_builder';

const industries: IndustryListItem[] = [
  {
    industry_id: 'public_sector',
    label: 'Public sector',
    pack_hints: ['service_cloud'],
    recommended_systems: [],
  },
  {
    industry_id: 'insurance_fixture',
    label: 'Insurance fixture',
    pack_hints: ['insurance_pack_from_registry'],
    recommended_systems: [],
  },
];

const baseState: SetupState = {
  focusId: 'core_operations',
  industryId: null,
  templateId: null,
  packId: null,
  templatePreselectedIds: [],
  selectedSystemIds: [],
  selectedSalesforceClouds: [],
  weightings: {},
  currentStep: 1,
};

describe('Stack Builder registry-owned pack hints', () => {
  it('uses the backend response for the Public sector default', () => {
    const state = { ...baseState, industryId: 'public_sector' };
    expect(resolvePackId(state, null, industries, [])).toBe('service_cloud');
  });

  it('supports an arbitrary industry pack by configuration only', () => {
    const state = { ...baseState, industryId: 'insurance_fixture' };
    expect(resolvePackId(state, null, industries, [])).toBe(
      'insurance_pack_from_registry',
    );
  });

  it('ignores Stack Builder system pre-selection — declaration/industry drive the pack', () => {
    // Reconciled with R18-A3 (f2026ee): a selected system must NOT force a pack
    // (the silent nCino footgun). With nothing declared in the catalog, the
    // industry hint wins and the salesforce_pss pre-selection is ignored.
    const state = {
      ...baseState,
      industryId: 'public_sector',
      selectedSystemIds: ['salesforce_pss'],
    };
    expect(resolvePackId(state, null, industries, [])).toBe('service_cloud');
  });

  it('lets the user override a template pack before launch', () => {
    const template = {
      template_id: 'commercial_lending',
      pack_id: 'ncino',
    } as TemplateListItem;
    const state = {
      ...baseState,
      templateId: 'commercial_lending',
      packId: 'service_cloud',
    };
    expect(resolvePackId(state, null, industries, [template])).toBe('service_cloud');
  });
});
