/**
 * 2.0-C4 T4 (AT-845) — grace behaviour, UI half.
 *
 * Parent-story criterion exercised here:
 *
 *   AC3 — the pack runs normally during grace and moves to safe-disabled after it,
 *         with history intact.
 *
 * The UI's whole job in AC3 is to keep two outcomes distinguishable. A pack the
 * organisation disabled and a pack the vendor retired both end up excluded from a
 * run, but the remedies are opposites: re-enable it, versus migrate off it because
 * it can never come back. Labelling both "disabled" would send an operator to a
 * button that cannot help them, which is the failure these tests exist to prevent.
 *
 * The backend halves are pinned in ``backend/tests/unit/test_pack_grace_behaviour.py``
 * and ``backend/tests/contract/test_pack_grace_lifecycle.py``.
 */
import { describe, expect, it } from 'vitest';

import {
  excludedPackLabel,
  excludedPacksDetail,
  packLifecycleLabel,
} from '../pages/RunHealthDashboardPage';
import type { ExcludedPackItem, PackHealthItem } from '../types/runHealth';

const disabledItem: ExcludedPackItem = {
  packId: 'security_ops',
  state: 'disabled',
  reason: 'pack_disabled',
};

const retiredItem: ExcludedPackItem = {
  packId: 'cloud_ops',
  state: 'disabled',
  reason: 'deprecation_grace_expired',
};

describe('excludedPackLabel', () => {
  it('calls a customer disable what it is', () => {
    expect(excludedPackLabel('pack_disabled')).toBe('disabled');
  });

  it('distinguishes a pack retired by an expired grace period', () => {
    expect(excludedPackLabel('deprecation_grace_expired')).toBe(
      'grace period ended',
    );
  });

  it('falls back to neutral wording rather than leaking a raw code', () => {
    expect(excludedPackLabel('some_future_reason')).toBe('disabled');
    expect(excludedPackLabel(undefined)).toBe('disabled');
    expect(excludedPackLabel(null)).toBe('disabled');
  });
});

describe('excludedPacksDetail', () => {
  it('tells a disabled pack the customer can re-enable it', () => {
    const detail = excludedPacksDetail([disabledItem]);
    expect(detail).toContain('security_ops');
    expect(detail).toContain('Re-enable');
  });

  it('tells a retired pack the customer to migrate, and that re-enabling will not help', () => {
    const detail = excludedPacksDetail([retiredItem]);
    expect(detail).toContain('cloud_ops');
    expect(detail).toContain('grace period');
    expect(detail).toContain('Migrate to the replacement pack');
    expect(detail).toContain('re-enabling will not bring it back');
  });

  it('keeps the two remedies separate when both reasons are present', () => {
    const detail = excludedPacksDetail([disabledItem, retiredItem]);
    // Each pack is named in its own clause with its own remedy — never one
    // sentence that describes the retired pack in the disabled pack's terms.
    const reEnableAt = detail.indexOf('Re-enable');
    const migrateAt = detail.indexOf('Migrate to the replacement pack');
    expect(reEnableAt).toBeGreaterThan(-1);
    expect(migrateAt).toBeGreaterThan(-1);
    expect(detail.indexOf('security_ops')).toBeLessThan(migrateAt);
    expect(detail.indexOf('cloud_ops')).toBeGreaterThan(reEnableAt);
  });

  it('is empty when nothing was excluded', () => {
    expect(excludedPacksDetail([])).toBe('');
  });
});

describe('the lifecycle pills are unchanged by grace behaviour', () => {
  const pack = (overrides: Partial<PackHealthItem> = {}): PackHealthItem =>
    ({
      pack_id: 'cloud_ops',
      pack_version: '1.2.0',
      detector_count: 4,
      detectors: ['a', 'b', 'c', 'd'],
      ...overrides,
    }) as PackHealthItem;

  it('reads Active while the pack is in grace', () => {
    // A pack in grace RUNS. Its deprecation notice is a fourth orthogonal fact
    // beside state and version (the AT-843 rule) and must not move the state pill.
    expect(packLifecycleLabel(pack({ deprecated: true } as Partial<PackHealthItem>)).stateLabel).toBe(
      'Active',
    );
  });

  it('reads Disabled once the retirement has been applied', () => {
    expect(packLifecycleLabel(pack({ pack_state: 'disabled' })).stateLabel).toBe(
      'Disabled',
    );
  });

  it('never reads a retirement as an error', () => {
    // Retirement on the announced date is intentional lifecycle, not a fault —
    // the same rule 2.0-C1 T5 established for a disabled pack.
    expect(packLifecycleLabel(pack({ pack_state: 'disabled' })).stateTone).toBe(
      'warn',
    );
  });
});
