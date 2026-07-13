/**
 * StackBuilderTemplateDePOC.test.tsx — R18-C0 P9, updated for R18-C1 T3
 *
 * Original intent (R18-C0 P9): lock the removal of the POC-specific
 * "Public retirement" / STRS front-door template tile, while keeping Public
 * sector selectable as a generic industry and the three generic templates.
 *
 * R18-C1 T3 (Addendum A) makes the Stack Builder selection UI registry-driven:
 * industries and templates come from the backend registry / template model, and
 * the hardcoded INDUSTRIES / TEMPLATES arrays that used to live in
 * DiscoveryFocusPage are GONE (AC10). The de-POC guarantee itself is now
 * enforced by the backend registry + its contract test
 * (backend/tests/contract/test_stack_builder_templates.py).
 *
 * This test therefore now locks the frontend half of that guarantee:
 *   1. DiscoveryFocusPage no longer exports hardcoded INDUSTRIES / TEMPLATES
 *      arrays (there is no local source of truth to drift from the backend).
 *   2. The picker renders exactly what the registry returns — a STRS / public
 *      retirement template only appears if the backend serves one, and Public
 *      sector renders when the registry lists it.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import * as focusPageModule from '../pages/DiscoveryFocusPage';
import DiscoveryFocusPage from '../pages/DiscoveryFocusPage';
import { useSetupState } from '../components/stack_builder';
import type { IndustryListItem, TemplateListItem } from '../types/stack_builder';

const INDUSTRIES: IndustryListItem[] = [
  { industry_id: 'financial_services', label: 'Financial services', pack_hints: ['ncino'], recommended_systems: [] },
  { industry_id: 'public_sector', label: 'Public sector', pack_hints: ['service_cloud'], recommended_systems: [] },
];

const TEMPLATES: TemplateListItem[] = [
  {
    template_id: 'commercial_lending',
    label: 'Commercial lending',
    description: 'Lending starting point',
    suggested_systems: ['salesforce_ncino'],
    suggested_roles: { salesforce_ncino: 'system_of_record' },
    focus_defaults: { focus_id: 'approvals_compliance', emphasis: [] },
    pack_id: 'ncino',
    detector_emphasis: [],
    terminology: {},
    metadata: {},
  },
  {
    template_id: 'service_operations',
    label: 'Service operations',
    description: 'Service starting point',
    suggested_systems: ['salesforce_sc'],
    suggested_roles: {},
    focus_defaults: { focus_id: 'member_customer_service', emphasis: [] },
    pack_id: 'service_cloud',
    detector_emphasis: [],
    terminology: {},
    metadata: {},
  },
];

function Harness() {
  const setupState = useSetupState();
  return (
    <DiscoveryFocusPage
      setupState={setupState}
      industries={INDUSTRIES}
      templates={TEMPLATES}
      registryLoading={false}
      registryError={null}
      onRetryRegistry={vi.fn()}
      fetchSystemDefaults={vi.fn(async () => [])}
    />
  );
}

describe('R18-C1 T3 — Stack Builder is registry-driven (de-POC preserved)', () => {
  it('no longer exports hardcoded INDUSTRIES / TEMPLATES arrays from the page', () => {
    const mod = focusPageModule as Record<string, unknown>;
    expect(mod.INDUSTRIES).toBeUndefined();
    expect(mod.TEMPLATES).toBeUndefined();
    // Focus tiles are NOT registry-driven, so they stay exported.
    expect(Array.isArray(mod.FOCUS_CARDS)).toBe(true);
  });

  it('renders only the templates the registry returns — no STRS / public retirement tile', () => {
    render(<Harness />);

    expect(screen.getByRole('checkbox', { name: 'Commercial lending' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Service operations' })).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /public retirement/i })).toBeNull();
    expect(screen.queryByRole('checkbox', { name: /strs/i })).toBeNull();
  });

  it('keeps Public sector selectable as a generic industry when the registry lists it', () => {
    render(<Harness />);
    expect(screen.getByRole('checkbox', { name: 'Public sector' })).toBeInTheDocument();
  });
});
