/**
 * StackBuilderTemplateDePOC.test.tsx — R18-C0 P9
 *
 * Locks the "Public retirement" front-door template tile removal.
 *
 * The template tile was a POC-specific shortcut (Public retirement / STRS)
 * presented as a first-class Stack Builder template. This task removes the
 * visible tile only: Public sector remains a selectable industry, and the
 * same systems the old template preselected (salesforce_pss, jira,
 * servicenow, confluence) remain reachable by hand via industry + system
 * selection. Nothing functional was deleted — only the named front-door
 * shortcut.
 */

import { describe, it, expect } from 'vitest';
import { TEMPLATES, INDUSTRIES } from '../pages/DiscoveryFocusPage';

describe('R18-C0 P9 — Stack Builder template de-POC', () => {
  it('no longer lists a Public retirement (or similarly POC-named) template tile', () => {
    const ids = TEMPLATES.map(t => t.id);
    const labels = TEMPLATES.map(t => t.label.toLowerCase());

    expect(ids).not.toContain('public_retirement');
    expect(labels.some(label => label.includes('public retirement'))).toBe(false);
    expect(labels.some(label => label.includes('strs'))).toBe(false);
  });

  it('keeps exactly the generic, reusable templates', () => {
    expect(TEMPLATES.map(t => t.id).sort()).toEqual(
      ['commercial_lending', 'revenue_operations', 'service_operations'].sort(),
    );
  });

  it('keeps Public sector selectable as a generic industry', () => {
    expect(INDUSTRIES.some(ind => ind.id === 'public_sector')).toBe(true);
  });
});
