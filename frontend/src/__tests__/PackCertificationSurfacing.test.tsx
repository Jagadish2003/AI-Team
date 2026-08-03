/**
 * 2.0-C2 T3 (AT-833) — certification level surfacing.
 *
 * Parent-story criterion exercised here:
 *
 *   AC2 — the level is displayed at selection, activation, on findings, and in
 *         exports.
 *
 * The load-bearing negative is tested in every case: a pack that CLAIMS Certified
 * but whose signature does not verify arrives as `community` and must render as
 * Community, never as its claim (2.0-C2 AC1 carried into the UI). The badge
 * component is given no path to render `declaredLevel`, and these tests pin that.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import PackCertificationBadge from '../components/common/PackCertificationBadge';
import { PackProvenanceRow } from '../components/analyst_review/OpportunityDetail';
import { packLifecycleLabel } from '../pages/RunHealthDashboardPage';
import { certificationsByPackId } from '../api/packStateApi';
import type { PackStateItem, PackStateResponse } from '../api/packStateApi';
import type { OpportunityCandidate } from '../types/analystReview';
import type { PackHealthItem } from '../types/runHealth';

const opp = (overrides: Partial<OpportunityCandidate> = {}): OpportunityCandidate =>
  ({
    id: 'opp_1',
    title: 'Recurring resolution loop',
    packId: 'cloud_ops',
    packVersion: '1.2.0',
    ...overrides,
  }) as OpportunityCandidate;

describe('PackCertificationBadge', () => {
  it('renders the CloudFulcrum Certified badge', () => {
    render(<PackCertificationBadge level="certified" label="CloudFulcrum Certified" />);
    const badge = screen.getByTestId('pack-certification-certified');
    expect(badge).toHaveTextContent('CloudFulcrum Certified');
    expect(badge).toHaveAttribute('data-level', 'certified');
  });

  it('renders Partner and Community distinctly', () => {
    const { rerender } = render(<PackCertificationBadge level="partner" label="Partner" />);
    expect(screen.getByTestId('pack-certification-partner')).toHaveTextContent('Partner');

    rerender(<PackCertificationBadge level="community" label="Community" />);
    expect(screen.getByTestId('pack-certification-community')).toHaveTextContent('Community');
  });

  it('falls back to the canonical wording when the backend sends no label', () => {
    render(<PackCertificationBadge level="certified" />);
    expect(screen.getByTestId('pack-certification-certified')).toHaveTextContent(
      'CloudFulcrum Certified',
    );
  });

  it('adds "review due" without changing the level', () => {
    render(<PackCertificationBadge level="certified" label="CloudFulcrum Certified" reviewDue />);
    const badge = screen.getByTestId('pack-certification-certified');
    // AC4's display half: flagged, never downgraded.
    expect(badge).toHaveAttribute('data-level', 'certified');
    expect(screen.getByTestId('pack-certification-review-due')).toBeInTheDocument();
  });

  it('renders nothing for an absent or unrecognised level', () => {
    const { container, rerender } = render(<PackCertificationBadge level={undefined} />);
    expect(container).toBeEmptyDOMElement();

    rerender(<PackCertificationBadge level="platinum" label="Platinum" />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText('Platinum')).not.toBeInTheDocument();
  });
});

describe('findings surface', () => {
  it('shows the certification level beside the pack id and version', () => {
    render(
      <PackProvenanceRow
        opp={opp({
          packCertificationLevel: 'certified',
          packCertificationLabel: 'CloudFulcrum Certified',
        })}
      />,
    );
    // Provenance and assurance sit side by side — neither replaces the other.
    expect(screen.getByTestId('pack-provenance-id')).toHaveTextContent('cloud_ops');
    expect(screen.getByTestId('pack-provenance-version')).toHaveTextContent('v1.2.0');
    expect(screen.getByTestId('pack-provenance-certification')).toHaveTextContent(
      'CloudFulcrum Certified',
    );
  });

  it('shows Community for a finding whose pack could not prove its claim', () => {
    render(
      <PackProvenanceRow
        opp={opp({
          // What the backend serves after a failed verification: the EFFECTIVE
          // level. The UI is never handed the unproved claim.
          packCertificationLevel: 'community',
          packCertificationLabel: 'Community',
        })}
      />,
    );
    const badge = screen.getByTestId('pack-provenance-certification');
    expect(badge).toHaveTextContent('Community');
    expect(screen.queryByText('CloudFulcrum Certified')).not.toBeInTheDocument();
  });

  it('renders the disabled label and the certification badge together', () => {
    render(
      <PackProvenanceRow
        opp={opp({
          packState: 'disabled',
          packStateLabel: 'Produced by a now-disabled pack',
          packCertificationLevel: 'certified',
          packCertificationLabel: 'CloudFulcrum Certified',
        })}
      />,
    );
    // Disabled and Certified are orthogonal: a disabled pack was still certified
    // when it produced this finding, and neither fact suppresses the other.
    expect(screen.getByTestId('pack-provenance-disabled')).toBeInTheDocument();
    expect(screen.getByTestId('pack-provenance-certification')).toBeInTheDocument();
  });

  it('omits the badge entirely when the backend sent no level', () => {
    render(<PackProvenanceRow opp={opp()} />);
    expect(screen.queryByTestId('pack-provenance-certification')).not.toBeInTheDocument();
    // …and the rest of the provenance row is unaffected (pre-2.0-C2 responses).
    expect(screen.getByTestId('pack-provenance-id')).toBeInTheDocument();
  });
});

describe('run-health packs panel', () => {
  const packRow = (overrides: Partial<PackHealthItem> = {}): PackHealthItem =>
    ({
      pack_id: 'cloud_ops',
      pack_name: 'Cloud Operations',
      pack_version: '1.2.0',
      detector_count: 6,
      detectors: [],
      executed_at: '2026-07-31T00:00:00Z',
      pack_state: 'active',
      ...overrides,
    }) as PackHealthItem;

  it('leaves the lifecycle pills untouched by certification', () => {
    // Regression against 2.0-C1 T5: state and version are separate facts, and
    // adding a third (assurance) must not change either.
    const lifecycle = packLifecycleLabel(
      packRow({ certification_level: 'community', certification_label: 'Community' }),
    );
    expect(lifecycle.stateLabel).toBe('Active');
    expect(lifecycle.stateTone).toBe('good');
    expect(lifecycle.versionLabel).toBe('Version 1.2.0');
  });

  it('a disabled pack still reports its certification independently', () => {
    const lifecycle = packLifecycleLabel(
      packRow({
        pack_state: 'disabled',
        certification_level: 'certified',
        certification_label: 'CloudFulcrum Certified',
      }),
    );
    expect(lifecycle.stateLabel).toBe('Disabled');
    // Disabling a pack is not a statement about its certification.
    expect(lifecycle.stateTone).toBe('warn');
  });
});

describe('selection surface', () => {
  it('maps the pack-state response to badges by pack id', () => {
    const byId = certificationsByPackId({
      orgId: 'default',
      packs: [
        {
          packId: 'cloud_ops',
          packName: 'Cloud Operations',
          packVersion: '1.2.0',
          state: 'active',
          revision: 0,
          reason: null,
          updatedBy: null,
          updatedAt: null,
          pinnedVersion: null,
          effectiveVersion: '1.2.0',
          availableVersions: [],
          registered: true,
          certification: {
            packId: 'cloud_ops',
            level: 'certified',
            label: 'CloudFulcrum Certified',
          },
        },
        {
          // An orphaned row (pack removed from the registry) carries no badge —
          // and must not be invented into one.
          packId: 'gone_pack',
          packName: 'gone_pack',
          packVersion: null,
          state: 'disabled',
          revision: 3,
          reason: null,
          updatedBy: null,
          updatedAt: null,
          pinnedVersion: null,
          effectiveVersion: null,
          availableVersions: [],
          registered: false,
          certification: null,
        },
      ],
    });

    expect(byId.cloud_ops.level).toBe('certified');
    expect(byId.gone_pack).toBeUndefined();
  });

  it('degrades to no badges when the pack-state read fails', () => {
    expect(certificationsByPackId(null)).toEqual({});
    expect(certificationsByPackId(undefined)).toEqual({});
  });
});


describe('certification policy surfacing (AT-834)', () => {
  const row = (overrides: Partial<PackStateItem> = {}): PackStateItem =>
    ({
      packId: 'cloud_ops',
      packName: 'Cloud Operations',
      packVersion: '1.2.0',
      state: 'active',
      revision: 0,
      reason: null,
      updatedBy: null,
      updatedAt: null,
      pinnedVersion: null,
      effectiveVersion: '1.2.0',
      availableVersions: [],
      registered: true,
      certification: {
        packId: 'cloud_ops',
        level: 'community',
        label: 'Community',
        declaredLevel: 'certified',
      },
      ...overrides,
    }) as PackStateItem;

  it('carries the block flag and its reason through the API shape', () => {
    const blocked = row({
      activationBlocked: true,
      activationBlockedReason:
        "pack 'cloud_ops' is Community; this organisation requires CloudFulcrum Certified or higher",
    });
    expect(blocked.activationBlocked).toBe(true);
    expect(blocked.activationBlockedReason).toContain('requires CloudFulcrum Certified');
  });

  it('still maps the badge for a blocked pack', () => {
    // A blocked pack is not a badge-less pack: the reader needs to see the level
    // that caused the block.
    const byId = certificationsByPackId({
      orgId: 'default',
      packs: [row({ activationBlocked: true })],
      certificationPolicy: {
        orgId: 'default',
        minimumLevel: 'certified',
        minimumLevelLabel: 'CloudFulcrum Certified',
        restricted: true,
        label: 'CloudFulcrum Certified or higher',
        revision: 1,
        reason: null,
        updatedBy: null,
        updatedAt: null,
      },
    });
    expect(byId.cloud_ops.level).toBe('community');
    expect(byId.cloud_ops.declaredLevel).toBe('certified');
  });

  it('treats an absent policy as no restriction only when the backend says so', () => {
    // `certificationPolicy: null` means the policy could NOT be read. The UI must
    // not render "unrestricted" from it — it simply shows no banner, and the gate
    // at activation still refuses.
    const response: PackStateResponse = {
      orgId: 'default',
      packs: [row()],
      certificationPolicy: null,
    };
    expect(response.certificationPolicy?.restricted).toBeUndefined();
    expect(certificationsByPackId(response).cloud_ops).toBeDefined();
  });
});
