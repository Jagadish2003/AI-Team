/**
 * PR review fix — certification policy readability.
 *
 * The defect: the activation GATE fails closed (an unreadable policy returns 503)
 * while the display annotation fails SOFT. Both are individually correct, but they
 * disagreed invisibly. On an unreadable policy the response carried
 * `certificationPolicy: null` and no `activationBlocked` on any row — which is
 * byte-for-byte what an UNRESTRICTED org looks like. A surface filtering on
 * `activationBlocked` therefore found nothing, rendered every pack as activatable,
 * and the launch then returned 503 with nothing on screen explaining why.
 *
 * The fix is not to make the display block packs — that would over-block an org
 * whose policy may well permit them. It is to make "unknown" expressible, so a
 * consumer can no longer mistake it for "permitted". These tests pin exactly that
 * distinguishability, including the negative: the unrestricted and unreadable
 * responses must NOT read the same.
 */
import { describe, expect, it } from 'vitest';

import {
  activationEligibility,
  isCertificationPolicyIndeterminate,
} from '../api/packStateApi';
import type {
  PackCertificationPolicy,
  PackStateItem,
  PackStateResponse,
} from '../api/packStateApi';

const pack = (overrides: Partial<PackStateItem> = {}): PackStateItem =>
  ({
    packId: 'cloud_ops',
    packName: 'Cloud Operations',
    packVersion: '1.2.0',
    state: 'active',
    revision: 1,
    reason: null,
    updatedBy: null,
    updatedAt: null,
    pinnedVersion: null,
    effectiveVersion: '1.2.0',
    availableVersions: [],
    registered: true,
    certification: null,
    ...overrides,
  }) as PackStateItem;

const unrestrictedPolicy: PackCertificationPolicy = {
  orgId: 'acme',
  minimumLevel: 'community',
  minimumLevelLabel: 'Community',
  restricted: false,
  label: 'Community',
  revision: 1,
  reason: null,
  updatedBy: null,
  updatedAt: null,
};

/** Policy read, imposes no restriction — every pack genuinely clears the floor. */
const unrestricted: PackStateResponse = {
  orgId: 'acme',
  packs: [pack({ activationPolicyStatus: 'available', activationBlocked: false })],
  certificationPolicy: unrestrictedPolicy,
  certificationPolicyStatus: 'available',
};

/** Policy store down — eligibility is unknown and activation will be refused. */
const unreadable: PackStateResponse = {
  orgId: 'acme',
  packs: [pack({ activationPolicyStatus: 'unavailable' })],
  certificationPolicy: null,
  certificationPolicyStatus: 'unavailable',
};

describe('isCertificationPolicyIndeterminate', () => {
  it('reports an unreadable policy', () => {
    expect(isCertificationPolicyIndeterminate(unreadable)).toBe(true);
  });

  it('does not report an unrestricted policy as unreadable', () => {
    expect(isCertificationPolicyIndeterminate(unrestricted)).toBe(false);
  });

  it('the two cases are distinguishable even though both have a null policy', () => {
    // The whole point of the fix. Before it, `certificationPolicy` was the only
    // signal and it was null for both, so these two states were identical.
    const unrestrictedWithNullPolicy: PackStateResponse = {
      ...unrestricted,
      certificationPolicy: null,
    };
    expect(isCertificationPolicyIndeterminate(unrestrictedWithNullPolicy)).toBe(false);
    expect(isCertificationPolicyIndeterminate(unreadable)).toBe(true);
  });

  it('treats a pre-bump response with no status field as determinate', () => {
    // Additive field: a response served before the contract bump omits it. Reading
    // absence as "unreadable" would show a scary advisory on every older backend.
    expect(isCertificationPolicyIndeterminate({ orgId: 'acme', packs: [] })).toBe(false);
    expect(isCertificationPolicyIndeterminate(null)).toBe(false);
    expect(isCertificationPolicyIndeterminate(undefined)).toBe(false);
  });
});

describe('activationEligibility', () => {
  it('reports a blocked pack', () => {
    expect(
      activationEligibility(
        pack({ activationPolicyStatus: 'available', activationBlocked: true }),
      ),
    ).toBe('blocked');
  });

  it('reports a permitted pack', () => {
    expect(
      activationEligibility(
        pack({ activationPolicyStatus: 'available', activationBlocked: false }),
      ),
    ).toBe('permitted');
  });

  it('reports UNKNOWN — not permitted — when the policy could not be read', () => {
    // The exact misreading the finding named: `if (pack.activationBlocked)` is
    // false here, so a raw-field consumer calls this activatable. It is not known
    // to be.
    const row = pack({ activationPolicyStatus: 'unavailable' });
    expect(row.activationBlocked).toBeUndefined();
    expect(activationEligibility(row)).toBe('unknown');
    expect(activationEligibility(row)).not.toBe('permitted');
  });

  it('reports unknown for a missing row or an absent annotation', () => {
    expect(activationEligibility(undefined)).toBe('unknown');
    expect(activationEligibility(null)).toBe('unknown');
    // No annotation at all (pre-bump backend): still not a permission claim.
    expect(activationEligibility(pack())).toBe('unknown');
  });
});
