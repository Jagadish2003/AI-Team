import React from 'react';
import TopNav from '../components/common/TopNav';
import OpportunityList from '../components/analyst_review/OpportunityList';
import OpportunityDetail from '../components/analyst_review/OpportunityDetail';
import ReasoningOverride from '../components/analyst_review/ReasoningOverride';
import PermissionsPanel from '../components/analyst_review/PermissionsPanel';
import AuditTrail from '../components/analyst_review/AuditTrail';
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
    setOverrideReason,
    saveOverride,
    toggleLock,
    audit,
  } = useAnalystReviewContext();

  const { push } = useToast();

  return (
    <div className="min-h-screen bg-bg text-text flex flex-col">
      <TopNav />

      <div className="px-8 pt-6 pb-3">
        <div className="text-2xl font-semibold text-text">Analyst Review</div>
        <div className="mt-1 text-sm text-muted">
          Deep-dive trust layer: validate and override AI rationale per opportunity before executive reporting.
        </div>
      </div>

      <div className="flex-1 px-8 pb-6 overflow-hidden">
        <div
          className="grid h-full gap-6"
          style={{
            gridTemplateColumns: '400px 1fr 400px 300px',
            height: 'calc(100vh - 148px)',
          }}
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
            onOverrideReason={setOverrideReason}
            onSave={() => {
              const r = saveOverride();
              if (!r.ok) push(r.error ?? 'Unable to save override');
              else push('Override saved.');
            }}
            onViewEvidence={() => push('Evidence panel will be linked in Screen 7.')}
            onLockToggle={() => {
              toggleLock();
              push('Lock toggled.');
            }}
            onDecision={(d) => {
              const result = setDecision(d);
              if (!result.ok) {
                push(
                  result.error === 'Decision finalized'
                    ? 'Decision finalized. It can\u2019t be changed now.'
                    : (result.error ?? 'Unable to update decision.')
                );
              } else {
                push(`Decision set to ${d}.`);
              }
            }}
          />

          <div className="flex flex-col gap-4 overflow-y-auto">
            <PermissionsPanel opp={selected} />
            <AuditTrail events={audit} />
          </div>
        </div>
      </div>
    </div>
  );
}