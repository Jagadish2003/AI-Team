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
    activeTab, setActiveTab,
    filteredEntities, countsByType, entityTypes, setEntityTypeEnabled,
    queryEntities, setQueryEntities,
    selectedEntityIds, toggleEntity, clearSelection,
    filteredEvidence, selectedEvidenceId, selectEvidence,
    sources, sourceFilter, setSourceFilter,
    queryEvidence, setQueryEvidence,
    selectedEvidence, approveSelected, rejectSelected,
    saveDraftEnabled, setSaveDraftEnabled,
    goPrev, goNext,
    positionLabel
  } = usePartialResultsContext();

  const { run } = useDiscoveryRunContext();
  const { push } = useToast();

  return (
    <div className="min-h-screen text-text">
      <TopNav />
      <div className="mx-auto max-w-6xl px-4 py-6 pb-10">
        <div className="mb-4 flex items-center justify-between">
          <div>
            <div className="text-2xl font-semibold">Partial Results</div>
            <div className="mt-1 text-sm text-muted">Run ID: <span className="text-text font-semibold">{run.runId}</span></div>
          </div>
          <div className="rounded-lg border border-border bg-panel px-3 py-2 text-sm text-text">
            {run.status === 'RUNNING' ? 'RUNNING…' : run.status}
          </div>
        </div>

        <TabsHeader tab={activeTab} onTab={setActiveTab} />

        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[360px_1fr_360px]">
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
            onSaveDraftEnabled={(v) => { setSaveDraftEnabled(v); push(v ? 'Auto-save enabled (mock).' : 'Auto-save paused (mock).'); }}
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
            onApprove={() => { approveSelected(); push('Approved. Moved to next unreviewed item.'); }}
            onReject={() => { rejectSelected(); push('Rejected. Moved to next unreviewed item.'); }}
          />
        </div>
      </div>
    </div>
  );
}
