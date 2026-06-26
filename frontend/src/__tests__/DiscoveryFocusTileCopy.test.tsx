/**
 * DiscoveryFocusTileCopy.test.tsx — R16-C2 T5
 *
 * Locks the Discovery Focus tile-boundary copy (Section 3 / AC6).
 *
 * After R16-C2 T1–T4 each focus genuinely re-ranks discovery toward its
 * detector affinities (emphasis, not exclusion), so the tile copy can — and
 * must — honestly describe a distinct product boundary. These tests guard the
 * AC6 design-review intent without coupling to exact wording: every tile must
 * state what it emphasises, the two most-confused tiles must draw an explicit
 * boundary against each other, and enterprise-wide must read as the unbiased
 * full view rather than another specialised lens.
 */

import { describe, it, expect } from 'vitest';
import { FOCUS_CARDS } from '../pages/DiscoveryFocusPage';
import type { FocusId } from '../types/stack_builder';

const byId = (id: FocusId) => {
  const card = FOCUS_CARDS.find(c => c.id === id);
  if (!card) throw new Error(`No focus card for ${id}`);
  return card;
};

describe('R16-C2 T5 — Discovery Focus tile boundary copy', () => {
  it('defines exactly the seven canonical focus tiles', () => {
    expect(FOCUS_CARDS.map(c => c.id).sort()).toEqual(
      [
        'approvals_compliance',
        'back_office_productivity',
        'core_operations',
        'cross_system_handoffs',
        'engineering_change',
        'enterprise_wide',
        'member_customer_service',
      ].sort(),
    );
  });

  it('gives every tile non-trivial, scannable copy', () => {
    for (const card of FOCUS_CARDS) {
      expect(card.subtext.length).toBeGreaterThan(40);
      // Selection cards, not docs — keep copy from sprawling.
      expect(card.subtext.length).toBeLessThan(320);
    }
  });

  it('gives every tile a unique subtext (no copy overlap)', () => {
    const subtexts = FOCUS_CARDS.map(c => c.subtext);
    expect(new Set(subtexts).size).toBe(subtexts.length);
  });

  it('states what each focus emphasises or how to use it', () => {
    for (const card of FOCUS_CARDS) {
      expect(/emphasis|use (this )?when|no lens/i.test(card.subtext)).toBe(true);
    }
  });

  it('draws the approvals↔handoffs boundary on both tiles (the gate vs the gap)', () => {
    // A stuck approval gate is Approvals/compliance; work stuck between teams
    // is Cross-system handoffs. Each tile must point at the other.
    expect(byId('approvals_compliance').subtext.toLowerCase()).toContain('gate');
    expect(byId('approvals_compliance').subtext.toLowerCase()).toContain('cross-system handoffs');

    expect(byId('cross_system_handoffs').subtext.toLowerCase()).toContain('between');
    expect(byId('cross_system_handoffs').subtext.toLowerCase()).toContain('approvals / compliance');
  });

  it('distinguishes member service from back-office on both tiles', () => {
    expect(byId('member_customer_service').subtext.toLowerCase()).toContain('back-office productivity');
    expect(byId('back_office_productivity').subtext.toLowerCase()).toContain('member / customer service');
  });

  it('positions enterprise-wide as the unbiased full view, not a specialised focus', () => {
    const text = byId('enterprise_wide').subtext.toLowerCase();
    expect(text).toContain('unbiased');
    expect(/no lens|all (connected )?system|every connected system/.test(text)).toBe(true);
    expect(text).toContain('not a specialised focus');
  });

  it('keeps enterprise-wide as the full-width tile', () => {
    expect(byId('enterprise_wide').wide).toBe(true);
  });
});
