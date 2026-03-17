import React, { useEffect, useMemo, useState } from 'react';
import TopNav from '../components/common/TopNav';
import OpportunityToolbar, { TierFilter, ConfidenceFilter, DecisionFilter } from '../components/opportunity_map/OpportunityToolbar';
import OpportunityMatrix from '../components/opportunity_map/OpportunityMatrix';
import TopQuickWins from '../components/opportunity_map/TopQuickWins';
import OpportunityDetails from '../components/opportunity_map/OpportunityDetails';
import OpportunityRankedList from '../components/opportunity_map/OpportunityRankedList';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useNavigate } from 'react-router-dom';
import { useToast } from '../components/common/Toast';

export default function OpportunityMapPage() {
  const {
    opportunities,
    selectedId: contextSelectedId,
    select,
  } = useAnalystReviewContext();

  const nav = useNavigate();
  const { push } = useToast();

  const [q, setQ] = useState('');
  const [tier, setTier] = useState<TierFilter>('All');
  const [conf, setConf] = useState<ConfidenceFilter>('All');
  const [decision, setDecision] = useState<DecisionFilter>('All');
  const [selectedId, setSelectedId] = useState<string | null>(
    contextSelectedId ?? opportunities[0]?.id ?? null
  );

  const filtered = useMemo(() => {
    const query = q.trim().toLowerCase();
    return opportunities
      .filter(o => tier === 'All' || o.tier === tier)
      .filter(o => conf === 'All' || o.confidence === conf)
      .filter(o => decision === 'All' || o.decision === decision)
      .filter(
        o =>
          !query ||
          o.title.toLowerCase().includes(query) ||
          o.category.toLowerCase().includes(query)
      );
  }, [opportunities, q, tier, conf, decision]);

  // Sync local page selection from shared context.
  // This is what makes Executive Report -> Opportunity Map preserve selection.
  useEffect(() => {
    if (contextSelectedId && contextSelectedId !== selectedId) {
      setSelectedId(contextSelectedId);
    }
  }, [contextSelectedId, selectedId]);

  // Keep shared context in sync whenever selection changes on this page
  // so the Opportunity List, matrix, and downstream pages stay aligned.
  useEffect(() => {
    if (selectedId && selectedId !== contextSelectedId) {
      select(selectedId);
    }
  }, [selectedId, contextSelectedId, select]);

  useEffect(() => {
    if (filtered.length === 0) return;

    if (!selectedId || !filtered.some(o => o.id === selectedId)) {
      setSelectedId(filtered[0].id);
    }
  }, [filtered, selectedId]);

  const selected = useMemo(
    () => filtered.find(o => o.id === selectedId) ?? filtered[0] ?? null,
    [filtered, selectedId]
  );

  const ranked = useMemo(
    () =>
      filtered
        .slice()
        .sort(
          (a, b) =>
            (b.impact - b.effort) - (a.impact - a.effort) || b.impact - a.impact
        ),
    [filtered]
  );

  const quickWins = useMemo(
    () => ranked.filter(o => o.tier === 'Quick Win').slice(0, 5),
    [ranked]
  );

  const openReview = (id: string) => {
    select(id);
    nav('/analyst-review');
  };

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="w-full px-8 py-6 pb-10">
        <div className="mb-3">
          <div className="text-2xl font-semibold">Opportunity Map</div>
          <div className="mt-1 text-sm text-muted">
            Portfolio view: browse, compare, shortlist. Open Analyst Review only when
            you're ready to govern decisions.
          </div>
        </div>

        <OpportunityToolbar
          q={q}
          onQ={setQ}
          tier={tier}
          onTier={setTier}
          conf={conf}
          onConf={setConf}
          decision={decision}
          onDecision={setDecision}
          totalShown={filtered.length}
        />

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1fr_490px] lg:items-start">
          <div className="space-y-4">
            <OpportunityMatrix
              filtered={filtered}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
            <TopQuickWins quickWins={quickWins} onSelect={setSelectedId} />
          </div>

          <div className="flex flex-col gap-4">
            <OpportunityDetails
              selected={selected}
              onViewAnalysis={() =>
                push('View Analysis will be linked in Screen 8/10 (future).')
              }
              onGoToReview={() => selected && openReview(selected.id)}
            />
            <OpportunityRankedList
              ranked={ranked}
              selectedId={selectedId}
              onSelect={setSelectedId}
            />
          </div>
        </div>
      </div>
    </div>
  );
}