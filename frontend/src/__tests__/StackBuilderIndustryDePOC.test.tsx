/**
 * StackBuilderIndustryDePOC.test.tsx — R18-C0 P10 (Addendum A)
 *
 * Locks the genericisation of the Stack Builder industry pack-hint defaults.
 *
 * The frontend's INDUSTRY_PACK_HINTS map is a local mirror of the backend
 * industry registry's pack_hints (see backend/discovery/packs/
 * industry_registry.py — the source of truth, covered by
 * backend/tests/contract/test_stack_builder_api.py). Public sector's default
 * pack hint used to point at the customer-specific `strs_benefits` pack;
 * it must now default to the generic `service_cloud` pack. Financial
 * services must never default toward a public-sector pack either.
 *
 * This does not remove the real salesforce_pss -> strs_benefits system
 * wiring in CLOUD_PACK_REGISTRY — that is legitimate, industry-agnostic
 * technical routing for a system the user explicitly selected, not a
 * generic industry default.
 */

import { describe, it, expect } from 'vitest';
import { INDUSTRY_PACK_HINTS } from '../pages/StackBuilderPage';

describe('R18-C0 P10 — Stack Builder industry pack-hint de-POC', () => {
  it('defaults Public sector to the generic service_cloud pack only', () => {
    expect(INDUSTRY_PACK_HINTS.public_sector).toEqual(['service_cloud']);
  });

  it('never suggests a customer-specific pack for any industry default', () => {
    for (const [industryId, hints] of Object.entries(INDUSTRY_PACK_HINTS)) {
      for (const hint of hints) {
        expect(hint.toLowerCase()).not.toContain('strs');
      }
      expect(industryId).toBeTruthy();
    }
  });

  it('keeps Financial services suggesting financial-services systems only', () => {
    expect(INDUSTRY_PACK_HINTS.financial_services).toEqual(['ncino', 'service_cloud']);
  });
});
