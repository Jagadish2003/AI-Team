import React from 'react';
import TopNav from '../components/common/TopNav';
import TabsHeader from '../components/partial_results/TabsHeader';
import EntitiesSidebar from '../components/partial_results/EntitiesSidebar';
import EvidenceList from '../components/partial_results/EvidenceList';
import EvidenceViewer from '../components/partial_results/EvidenceViewer';
import { usePartialResultsContext } from '../context/PartialResultsContext';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useToast } from '../components/common/Toast';

export default function PartialResultsPage() {
  const {
    activeTab,
    setActiveTab,
    filteredEntities,
    countsByType,
    entityTypes,
    setEntityTypeEnabled,
    queryEntities,
    setQueryEntities,
    selectedEntityIds,
    toggleEntity,
    clearSelection,
    filteredEvidence,
    selectedEvidenceId,
    selectEvidence,
    sources,
    sourceFilter,
    setSourceFilter,
    queryEvidence,
    setQueryEvidence,
    selectedEvidence,
    approveSelected,
    rejectSelected,
    saveDraftEnabled,
    setSaveDraftEnabled,
    goPrev,
    goNext,
    positionLabel
  } = usePartialResultsContext();

  const { run } = useDiscoveryRunContext();
  const { push } = useToast();

  return (
    <div className="min-h-screen text-text">
      <TopNav />

      <div className="w-full px-8 py-6 pb-10">
        <div className="mb-6 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="text-2xl font-semibold">Partial Results</div>
            <div className="mt-1 text-sm text-muted">
              Run ID: <span className="font-semibold text-text">{run.runId}</span>
            </div>
          </div>

          <div
            className={`inline-flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
              run.status === 'COMPLETED'
                ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200'
                : run.status === 'FAILED'
                  ? 'border-danger/40 bg-danger/10 text-danger'
                  : 'border-accent/40 bg-accent/10 text-text'
            }`}
          >
            <span className="h-2 w-2 rounded-full bg-current opacity-80" />
            {run.status === 'RUNNING' ? 'RUNNING…' : run.status}
            <span className="text-muted">·</span>
            <span>{run.progress.percent}%</span>
          </div>
        </div>

        <TabsHeader tab={activeTab} onTab={setActiveTab} />

        <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-3">
          <EntitiesSidebar
            entities={filteredEntities}
            countsByType={countsByType}
            enabledTypes={entityTypes}
            onTypeToggle={setEntityTypeEnabled}
            query={queryEntities}
            onQuery={setQueryEntities}
            selectedEntityIds={selectedEntityIds}
            onToggleEntity={toggleEntity}
            onClear={clearSelection}
          />

          <EvidenceList
            evidence={filteredEvidence}
            selectedId={selectedEvidenceId}
            onSelect={selectEvidence}
            sources={sources}
            sourceFilter={sourceFilter}
            onSourceFilter={setSourceFilter}
            query={queryEvidence}
            onQuery={setQueryEvidence}
            saveDraftEnabled={saveDraftEnabled}
            onSaveDraftEnabled={(v) => {
              setSaveDraftEnabled(v);
              push(v ? 'Auto-save enabled (mock).' : 'Auto-save paused (mock).');
            }}
            positionLabel={positionLabel}
            onPrev={() => goPrev()}
            onNext={() => goNext()}
            onPaginationToast={() => push('List paging will be added in Sprint 2.')}
          />

          <EvidenceViewer
            evidence={selectedEvidence}
            positionLabel={positionLabel}
            onPrev={() => goPrev()}
            onNext={() => goNext()}
            onApprove={() => {
              const ok = approveSelected();
              if (ok) push('Approved. Moved to next unreviewed item.');
              else push('Decision finalized. It can\u2019t be changed now.');
            }}
            onReject={() => {
              const ok = rejectSelected();
              if (ok) push('Rejected. Moved to next unreviewed item.');
              else push('Decision finalized. It can\u2019t be changed now.');
            }}
          />
        </div>
      </div>
    </div>
  );
}