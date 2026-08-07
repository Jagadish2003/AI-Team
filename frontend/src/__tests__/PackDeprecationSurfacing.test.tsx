/**
 * 2.0-C4 T2 (AT-843) — deprecation notice surfacing.
 *
 * Parent-story criterion exercised here:
 *
 *   AC1 — deprecating a pack surfaces notice at run configuration, run health, and
 *         on its findings, with date and replacement.
 *
 * The load-bearing negatives, tested throughout:
 *
 *   * a pack that is NOT deprecated renders nothing — no empty banner, no "not
 *     deprecated" pill (the component is given no path to invent one);
 *   * the date and the replacement are always stated, INCLUDING when they are
 *     absent ("no removal date has been announced", "no replacement pack has been
 *     named"), never left as a gap the reader has to interpret;
 *   * deprecation never suppresses or replaces the existing provenance, lifecycle,
 *     or certification facts beside it.
 *
 * The backend half is pinned in
 * ``backend/tests/unit/test_pack_deprecation_surfacing.py``.
 */
import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  PackDeprecationBadge,
  PackDeprecationDetail,
} from '../components/common/PackDeprecationNotice';
import { PackProvenanceRow } from '../components/analyst_review/OpportunityDetail';
import { packLifecycleLabel } from '../pages/RunHealthDashboardPage';
import { deprecationsByPackId } from '../api/packStateApi';
import type { PackStateItem } from '../api/packStateApi';
import type { PackDeprecationNotice } from '../types/packDeprecation';
import type { OpportunityCandidate } from '../types/analystReview';
import type { PackHealthItem } from '../types/runHealth';

const NOTICE =
  "Pack 'cloud_ops' v1.2.0 is deprecated as of 2026-07-01: Superseded by the " +
  'Enterprise Operations pack. It runs normally until 2026-09-29, after which it ' +
  'will be disabled. Replaced by Enterprise Operations (enterprise_ops v1.0.0+).';

const notice = (
  overrides: Partial<PackDeprecationNotice> = {},
): PackDeprecationNotice => ({
  packId: 'cloud_ops',
  version: '1.2.0',
  phase: 'grace',
  label: 'Deprecated',
  statusLabel: 'Deprecated — runs until 2026-09-29',
  reason: 'Superseded by the Enterprise Operations pack.',
  deprecatedOn: '2026-07-01',
  graceEndsOn: '2026-09-29',
  daysRemaining: 57,
  replacementPackId: 'enterprise_ops',
  replacementLabel: 'Enterprise Operations (enterprise_ops v1.0.0+)',
  summary: NOTICE,
  ...overrides,
});

const opp = (overrides: Partial<OpportunityCandidate> = {}): OpportunityCandidate =>
  ({
    id: 'opp_1',
    title: 'Recurring resolution loop',
    packId: 'cloud_ops',
    packVersion: '1.2.0',
    ...overrides,
  }) as OpportunityCandidate;

describe('PackDeprecationBadge', () => {
  it('renders the deprecation pill with the date the backend put in the label', () => {
    render(
      <PackDeprecationBadge phase="grace" label="Deprecated — runs until 2026-09-29" />,
    );
    const badge = screen.getByTestId('pack-deprecation-grace');
    expect(badge).toHaveTextContent('Deprecated — runs until 2026-09-29');
    expect(badge).toHaveAttribute('data-phase', 'grace');
  });

  it('distinguishes an expired grace period from an active one', () => {
    const { rerender } = render(<PackDeprecationBadge phase="grace" />);
    expect(screen.getByTestId('pack-deprecation-grace')).toHaveTextContent('Deprecated');

    rerender(<PackDeprecationBadge phase="grace_expired" />);
    expect(screen.getByTestId('pack-deprecation-grace_expired')).toHaveTextContent(
      'grace period ended',
    );
  });

  it('falls back to the canonical wording when the backend sends no label', () => {
    render(<PackDeprecationBadge phase="grace_expired" />);
    expect(screen.getByTestId('pack-deprecation-grace_expired')).toHaveTextContent(
      'Deprecated — grace period ended',
    );
  });

  it('carries the full notice as its tooltip so the pill can stay compact', () => {
    render(<PackDeprecationBadge phase="grace" notice={NOTICE} />);
    expect(screen.getByTestId('pack-deprecation-grace')).toHaveAttribute('title', NOTICE);
  });

  it('renders nothing for a pack that is not deprecated', () => {
    const { container, rerender } = render(<PackDeprecationBadge phase={undefined} />);
    expect(container).toBeEmptyDOMElement();

    // An unrecognised phase is not guessed at either.
    rerender(<PackDeprecationBadge phase="sunsetting" label="Sunsetting" />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText('Sunsetting')).not.toBeInTheDocument();
  });
});

describe('PackDeprecationDetail', () => {
  it('states the date support ends and what replaces it', () => {
    const value = notice();
    render(
      <PackDeprecationDetail
        phase={value.phase}
        notice={value.summary}
        graceEndsOn={value.graceEndsOn}
        replacementLabel={value.replacementLabel}
        daysRemaining={value.daysRemaining}
      />,
    );
    // AC1's two named facts, both present and both explicit.
    expect(screen.getByTestId('pack-deprecation-ends-on')).toHaveTextContent(
      'Supported until 2026-09-29 (57 days left)',
    );
    expect(screen.getByTestId('pack-deprecation-replacement')).toHaveTextContent(
      'Replaced by Enterprise Operations (enterprise_ops v1.0.0+)',
    );
  });

  it('says so explicitly when no removal date has been announced', () => {
    const value = notice({ graceEndsOn: '', daysRemaining: null });
    render(
      <PackDeprecationDetail
        phase={value.phase}
        notice={value.summary}
        graceEndsOn={value.graceEndsOn}
        replacementLabel={value.replacementLabel}
        daysRemaining={value.daysRemaining}
      />,
    );
    // Never "Supported until " with a gap after it.
    expect(screen.getByTestId('pack-deprecation-ends-on')).toHaveTextContent(
      'No removal date has been announced.',
    );
  });

  it('says so explicitly when no replacement has been named', () => {
    const value = notice({ replacementPackId: '', replacementLabel: '' });
    render(
      <PackDeprecationDetail
        phase={value.phase}
        notice={value.summary}
        graceEndsOn={value.graceEndsOn}
        replacementLabel={value.replacementLabel}
      />,
    );
    expect(screen.getByTestId('pack-deprecation-replacement')).toHaveTextContent(
      'No replacement pack has been named.',
    );
  });

  it('reports an expired grace in the past tense', () => {
    render(
      <PackDeprecationDetail
        phase="grace_expired"
        notice={NOTICE}
        graceEndsOn="2026-09-29"
        replacementLabel="Enterprise Operations (enterprise_ops)"
      />,
    );
    expect(screen.getByTestId('pack-deprecation-ends-on')).toHaveTextContent(
      'Support ended 2026-09-29.',
    );
  });

  it('renders nothing for a pack that is not deprecated', () => {
    const { container } = render(<PackDeprecationDetail phase={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('pluralises the countdown correctly on the last day', () => {
    render(
      <PackDeprecationDetail
        phase="grace"
        notice={NOTICE}
        graceEndsOn="2026-09-29"
        daysRemaining={1}
      />,
    );
    expect(screen.getByTestId('pack-deprecation-ends-on')).toHaveTextContent('(1 day left)');
  });
});

describe('findings surface', () => {
  it('shows the notice beside the pack id and version', () => {
    render(
      <PackProvenanceRow
        opp={opp({
          packDeprecated: true,
          packDeprecationPhase: 'grace',
          packDeprecationLabel: 'Deprecated — runs until 2026-09-29',
          packDeprecationNotice: NOTICE,
          packDeprecationEndsOn: '2026-09-29',
          packDeprecationReplacementPackId: 'enterprise_ops',
          packDeprecationReplacementLabel: 'Enterprise Operations (enterprise_ops v1.0.0+)',
        })}
      />,
    );
    // Provenance is never replaced by the notice — they answer different questions
    // a reader asks at the same moment.
    expect(screen.getByTestId('pack-provenance-id')).toHaveTextContent('cloud_ops');
    expect(screen.getByTestId('pack-provenance-version')).toHaveTextContent('v1.2.0');
    expect(screen.getByTestId('pack-provenance-deprecation')).toHaveTextContent(
      'runs until 2026-09-29',
    );
    expect(
      screen.getByTestId('pack-provenance-deprecation-replacement'),
    ).toHaveTextContent('Replaced by Enterprise Operations');
  });

  it('omits the notice entirely for a finding from a live pack', () => {
    render(<PackProvenanceRow opp={opp()} />);
    expect(screen.queryByTestId('pack-provenance-deprecation')).not.toBeInTheDocument();
    expect(
      screen.queryByTestId('pack-provenance-deprecation-replacement'),
    ).not.toBeInTheDocument();
    // …and the rest of the provenance row is unaffected (pre-2.0-C4 responses).
    expect(screen.getByTestId('pack-provenance-id')).toBeInTheDocument();
  });

  it('renders deprecation, certification, and the disabled label together', () => {
    render(
      <PackProvenanceRow
        opp={opp({
          packState: 'disabled',
          packStateLabel: 'Produced by a now-disabled pack',
          packCertificationLevel: 'certified',
          packCertificationLabel: 'CloudFulcrum Certified',
          packDeprecated: true,
          packDeprecationPhase: 'grace_expired',
          packDeprecationLabel: 'Deprecated — grace period ended',
          packDeprecationNotice: NOTICE,
        })}
      />,
    );
    // Four orthogonal facts about one finding: none of them suppresses another.
    expect(screen.getByTestId('pack-provenance-id')).toBeInTheDocument();
    expect(screen.getByTestId('pack-provenance-certification')).toBeInTheDocument();
    expect(screen.getByTestId('pack-provenance-disabled')).toBeInTheDocument();
    expect(screen.getByTestId('pack-provenance-deprecation')).toHaveAttribute(
      'data-phase',
      'grace_expired',
    );
  });

  it('shows the notice without a replacement when none was named', () => {
    render(
      <PackProvenanceRow
        opp={opp({
          packDeprecated: true,
          packDeprecationPhase: 'grace',
          packDeprecationLabel: 'Deprecated',
          packDeprecationNotice: NOTICE,
        })}
      />,
    );
    expect(screen.getByTestId('pack-provenance-deprecation')).toBeInTheDocument();
    expect(
      screen.queryByTestId('pack-provenance-deprecation-replacement'),
    ).not.toBeInTheDocument();
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

  it('leaves the lifecycle pills untouched by deprecation', () => {
    // A deprecated pack is not a disabled pack and not a rolled-back one. During
    // grace it runs exactly as before, so state and version must read normally.
    const lifecycle = packLifecycleLabel(
      packRow({
        deprecated: true,
        deprecation_phase: 'grace',
        deprecation_label: 'Deprecated — runs until 2026-09-29',
      }),
    );
    expect(lifecycle.stateLabel).toBe('Active');
    expect(lifecycle.stateTone).toBe('good');
    expect(lifecycle.versionLabel).toBe('Version 1.2.0');
    expect(lifecycle.rolledBack).toBe(false);
  });

  it('an expired grace still does not change the pack state pill', () => {
    // Until AT-845 actually safe-disables it, the state row reports what is true.
    const lifecycle = packLifecycleLabel(
      packRow({ deprecated: true, deprecation_phase: 'grace_expired' }),
    );
    expect(lifecycle.stateLabel).toBe('Active');
  });

  it('a disabled AND deprecated pack reports both independently', () => {
    const row = packRow({
      pack_state: 'disabled',
      deprecated: true,
      deprecation_phase: 'grace_expired',
    });
    const lifecycle = packLifecycleLabel(row);
    expect(lifecycle.stateLabel).toBe('Disabled');
    expect(row.deprecation_phase).toBe('grace_expired');
  });
});

describe('run-configuration surface', () => {
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
      certification: null,
      ...overrides,
    }) as PackStateItem;

  it('maps the pack-state response to notices by pack id', () => {
    const byId = deprecationsByPackId({
      orgId: 'default',
      packs: [
        row({ deprecation: notice() }),
        row({ packId: 'ncino', packName: 'nCino Lending', deprecation: null }),
      ],
    });

    expect(byId.cloud_ops.graceEndsOn).toBe('2026-09-29');
    expect(byId.cloud_ops.replacementPackId).toBe('enterprise_ops');
    // A live pack is ABSENT rather than mapped to null, so a lookup is falsy and a
    // caller cannot accidentally render an empty notice for it.
    expect(byId.ncino).toBeUndefined();
  });

  it('omits a pack whose response predates the field', () => {
    expect(deprecationsByPackId({ orgId: 'default', packs: [row()] })).toEqual({});
  });

  it('degrades to no notices when the pack-state read fails', () => {
    expect(deprecationsByPackId(null)).toEqual({});
    expect(deprecationsByPackId(undefined)).toEqual({});
  });
});
