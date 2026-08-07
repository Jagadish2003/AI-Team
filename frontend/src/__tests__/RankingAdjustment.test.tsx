import React from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { RankingAdjustmentPanel } from '../components/learning/RankingAdjustment';
import type { OpportunityRanking } from '../types/analystReview';

function ranking(overrides: Partial<OpportunityRanking> = {}): OpportunityRanking {
  return {
    schemaVersion: '1.0.0',
    baseRank: 2,
    baseImpact: 5,
    adjustedRank: 2,
    moved: 0,
    adjusted: true,
    caps: {
      maxScoreFraction: 0.15,
      maxRankMove: 2,
    },
    wasCapped: true,
    cappedBy: 'rank_move',
    reason: {
      schemaVersion: '1.0.0',
      direction: 'up',
      ranksMoved: 0,
      baseRank: 2,
      adjustedRank: 2,
      decisionCount: 1,
      decisionsByAction: { accept: 1 },
      outcomeCount: 0,
      outcomesByVerdict: {},
      hasOutcomeEvidence: false,
      wasCapped: true,
      cappedBy: 'rank_move',
      evidenceStrength: 'minimal',
      totalSignals: 1,
      contributingDecisions: [],
      contributingOutcomes: [],
      summary: 'Adjustment capped: requested a higher rank but the cap kept this finding in place.',
    },
    ...overrides,
  };
}

describe('RankingAdjustmentPanel', () => {
  it('renders a capped adjustment even when moved is zero', () => {
    render(<RankingAdjustmentPanel ranking={ranking()} />);

    expect(screen.getByTestId('ranking-adjustment-panel')).toHaveTextContent(
      /kept this finding in place/i,
    );
  });

  it('does not crash when thin-evidence contributor arrays are null', () => {
    const thinEvidence = ranking({
      reason: {
        ...ranking().reason!,
        contributingDecisions: null,
        contributingOutcomes: null,
      },
    } as unknown as Partial<OpportunityRanking>);

    render(<RankingAdjustmentPanel ranking={thinEvidence} />);

    expect(screen.getByTestId('ranking-adjustment-panel')).toBeTruthy();
    expect(screen.queryByTestId('ranking-adjustment-links')).toBeNull();
  });
});
