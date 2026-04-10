import React from 'react';
import TopNav from '../components/common/TopNav';
import TabsHeader from '../components/partial_results/TabsHeader';
import EntitiesSidebar from '../components/partial_results/EntitiesSidebar';
import EvidenceList from '../components/partial_results/EvidenceList';
import EvidenceViewer from '../components/partial_results/EvidenceViewer';

import { usePartialResultsContext } from '../context/PartialResultsContext';
import { useDiscoveryRunContext } from '../context/DiscoveryRunContext';
import { useRunContext } from '../context/RunContext';
import { useToast } from '../components/common/Toast';
import { useNavigate } from 'react-router-dom';
import { RunRequiredEmptyState } from '../components/common/RunRequiredEmptyState';

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
    canPrev,
    canNext,
    positionLabel,
    loading,
    error,
    refetch,
  } = usePartialResultsContext();

  const { run } = useDiscoveryRunContext();
  const { runId } = useRunContext();
  const { push } = useToast();
  const nav = useNavigate();

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          <RunRequiredEmptyState onStart={() => nav('/discovery-run')} />
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="mx-auto max-w-3xl px-8 py-10">
          <div className="rounded-xl border border-border bg-panel p-6">
            <div className="text-lg font-semibold">Loading partial results…</div>
            <div className="mt-2 text-sm text-muted">Please wait…</div>
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="mx-auto max-w-3xl px-8 py-10">
          <div className="rounded-xl border border-border bg-panel p-6">
            <div className="text-lg font-semibold">Failed to load partial results</div>
            <div className="mt-2 text-sm text-red-300">{error}</div>
            <button
              className="mt-4 rounded-md bg-accent px-3 py-2 text-sm text-bg hover:opacity-90"
              onClick={() => refetch()}
            >
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen text-text">
      <TopNav />
      <div className="w-full px-8 py-6 pb-10">
        <div className="mb-6 flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div>
            <div className="text-2xl font-semibold">Partial Results</div>
            <div className="mt-1 text-sm text-muted">
              Run ID: <span className="font-semibold text-text">{runId}</span>
            </div>
          </div>
          <div className="flex justify-end">
            <div className="flex items-center gap-2 px-3 py-2 rounded-full bg-accent/20 text-white text-xs font-medium">
              <span className="w-2 h-2 rounded-full bg-accent animate-pulse"></span>
              <span>
                {run?.status === 'RUNNING' ? 'RUNNING...' : (run?.status ?? '—')}
              </span>
              <span className="opacity-80">
                {run?.progress?.percent ?? 0}%
              </span>
            </div>
          </div>
        </div>

        <TabsHeader tab={activeTab} onTab={setActiveTab} />
        <div className="mt-4 grid grid-cols-1 gap-6 lg:grid-cols-[0.9fr_1.4fr_0.9fr]">
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
            canPrev={canPrev}
            canNext={canNext}
            onPrev={() => goPrev()}
            onNext={() => goNext()}
          />

          <EvidenceViewer
            evidence={selectedEvidence}
            positionLabel={positionLabel}
            onPrev={() => goPrev()}
            onNext={() => goNext()}
            onApprove={() => {
              const ok = approveSelected();
              if (ok) push('Approved. Moved to next unreviewed item.');
              else push('Decision finalized. It can’t be changed now.');
            }}
            onReject={() => {
              const ok = rejectSelected();
              if (ok) push('Rejected. Moved to next unreviewed item.');
              else push('Decision finalized. It can’t be changed now.');
            }}
          />
        </div>
      </div>
    </div>
  );
}