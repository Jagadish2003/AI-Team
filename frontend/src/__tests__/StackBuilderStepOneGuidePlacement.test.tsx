/**
 * Stack Builder Screen 1 — where the template guide is rendered.
 *
 * The guide used to be rendered ABOVE the whole step, so selecting a template
 * from the "Start from a template" section (two sections down the page) produced
 * guidance off-screen above "Discovery focus" — the user had to scroll back up to
 * read the result of the pill they had just clicked.
 *
 * DiscoveryFocusPage now takes a `guide` slot rendered below the Industry /
 * template grid and above the Continue footer. These tests pin that DOM ORDER,
 * which is the whole point of the change — asserting only that the guide is
 * present would have passed before it too.
 *
 * Run:
 *   npx vitest run src/__tests__/StackBuilderStepOneGuidePlacement.test.tsx
 */
import '@testing-library/jest-dom/vitest';
import React from 'react';
import { render, screen, cleanup } from '@testing-library/react';
import { describe, it, expect, afterEach, vi } from 'vitest';

import DiscoveryFocusPage from '../pages/DiscoveryFocusPage';
import { useSetupState } from '../components/stack_builder';
import type { IndustryListItem, TemplateListItem } from '../types/stack_builder';

const INDUSTRIES: IndustryListItem[] = [
  {
    industry_id: 'financial_services',
    label: 'Financial Services',
    pack_hints: ['service_cloud'],
    recommended_systems: [],
    roadmap_systems: [],
  } as unknown as IndustryListItem,
];

const TEMPLATES: TemplateListItem[] = [
  {
    template_id: 'commercial_lending',
    label: 'Commercial Lending',
    pack_id: 'ncino',
    suggested_systems: ['salesforce'],
    suggested_roles: {},
    focus_defaults: { focus_id: 'approvals_compliance' },
    terminology: {},
    metadata: {},
  } as unknown as TemplateListItem,
];

/** Renders Screen 1 with a recognisable node in the guide slot. */
function Harness({ withGuide = true }: { withGuide?: boolean }) {
  const setupState = useSetupState(null);
  return (
    <DiscoveryFocusPage
      setupState={setupState}
      industries={INDUSTRIES}
      templates={TEMPLATES}
      registryLoading={false}
      registryError={null}
      onRetryRegistry={vi.fn()}
      fetchSystemDefaults={vi.fn().mockResolvedValue([])}
      guide={withGuide ? <div>FIRST RUN GUIDE</div> : undefined}
    />
  );
}

/** Document-order position of an element, for before/after assertions. */
function positionOf(text: string): number {
  const target = screen.getByText(text);
  const all = Array.from(document.querySelectorAll('*'));
  return all.indexOf(target as Element);
}

describe('Stack Builder Screen 1 — template guide placement', () => {
  afterEach(() => cleanup());

  it('renders the guide BELOW the Industry and template sections', () => {
    render(<Harness />);

    const guide = positionOf('FIRST RUN GUIDE');
    expect(guide).toBeGreaterThan(positionOf('Industry'));
    expect(guide).toBeGreaterThan(positionOf('Start from a template'));
  });

  it('renders the guide BELOW the Discovery focus section it used to sit above', () => {
    render(<Harness />);
    expect(positionOf('FIRST RUN GUIDE')).toBeGreaterThan(
      positionOf('Discovery focus'),
    );
  });

  it('keeps the guide ABOVE the Continue footer so it is read before moving on', () => {
    render(<Harness />);
    expect(positionOf('FIRST RUN GUIDE')).toBeLessThan(
      positionOf('Continue to your systems'),
    );
  });

  it('renders the step unchanged when no guide is supplied', () => {
    render(<Harness withGuide={false} />);
    expect(screen.queryByText('FIRST RUN GUIDE')).not.toBeInTheDocument();
    // The step's own sections are untouched by the slot.
    expect(screen.getByText('Discovery focus')).toBeInTheDocument();
    expect(screen.getByText('Industry')).toBeInTheDocument();
    expect(screen.getByText('Start from a template')).toBeInTheDocument();
    expect(screen.getByText('Continue to your systems')).toBeInTheDocument();
  });
});
