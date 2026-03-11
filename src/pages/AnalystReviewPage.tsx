import React from 'react';
import TopNav from '../components/common/TopNav';
import OpportunityList from '../components/analyst_review/OpportunityList';
import OpportunityDetail from '../components/analyst_review/OpportunityDetail';
import ReasoningOverride from '../components/analyst_review/ReasoningOverride';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';
import { useToast } from '../components/common/Toast';

export default function AnalystReviewPage() {
  const {
    opportunities,
    selectedId,
    select,
    selected,
    setDecision,
    setOverrideText,
    toggleLock,
    audit,
  } = useAnalystReviewContext();

  const { push } = useToast();

  return (
    <div className="min-h-screen bg-bg text-text flex flex-col">
      <TopNav />

      {/* Page title — matches PartialResultsPage spacing */}
      <div className="w-full px-8 py-6 pb-0">
        <div className="text-2xl font-semibold text-text">Analyst Review</div>
        <div className="mt-1 text-sm text-muted">
          Deep-dive trust layer: validate and override AI rationale per opportunity before executive reporting.
        </div>
      </div>

      {/* 3-column grid — matches px-8 gutter of PartialResultsPage */}
      <div className="flex-1 px-8 py-6 pb-10 overflow-hidden">
        <div
          className="grid h-full gap-6"
          style={{ gridTemplateColumns: '300px 1fr 320px', height: 'calc(100vh - 148px)' }}
        >
          <OpportunityList
            items={opportunities}
            selectedId={selectedId}
            onSelect={select}
            onCreate={() => push('Create Opportunity will be available in Sprint 2.')}
          />

          <OpportunityDetail
            opp={selected}
            audit={audit}
            onNavigate={() => push('Full detail view coming in Sprint 2.')}
          />

          <ReasoningOverride
            opp={selected}
            audit={audit}
            onOverrideText={setOverrideText}
            onLockToggle={() => { toggleLock(); push('Lock toggled.'); }}
            onDecision={(d) => {
              setDecision(d);
              push(`Decision set to ${d}.`);
            }}
          />
        </div>
      </div>
    </div>
  );
}