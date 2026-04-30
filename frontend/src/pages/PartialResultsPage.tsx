import React from 'react';
import { ChevronLeft, ChevronRight, Search } from 'lucide-react';
import TopNav from '../components/common/TopNav';
import EntitiesSidebar from '../components/partial_results/EntitiesSidebar';
import EvidenceCard from '../components/partial_results/EvidenceCard';
import EvidenceViewer from '../components/partial_results/EvidenceViewer';

import { usePartialResultsContext } from '../context/PartialResultsContext';
import { useRunContext } from '../context/RunContext';
import { useToast } from '../components/common/Toast';
import { useNavigate } from 'react-router-dom';
import { RunRequiredEmptyState } from '../components/common/RunRequiredEmptyState';
import { useAnalystReviewContext } from '../context/AnalystReviewContext';

export default function PartialResultsPage() {
  const {
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

  const { opportunities } = useAnalystReviewContext();
  const { runId } = useRunContext();
  const { push } = useToast();
  const nav = useNavigate();

  if (!runId) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="px-8 py-6">
          <RunRequiredEmptyState
            pageTitle="Evidence Collection"
            pageDescription="Evidence collection acts as the trust layer for discovery, enabling enterprise transparency by clearly showing what evidence was inferred, from which source, and with what level of confidence."
            onStart={() => nav('/discovery-run')}
          />
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
            <div className="text-lg font-semibold">Loading partial results...</div>
            <div className="mt-2 text-sm text-muted">Please wait...</div>
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
            <div className="text-2xl font-semibold" data-testid="page-title">Evidence Collection</div>
            <div className="mt-1 mb-2 text-sm text-muted">
              Evidence collection acts as the trust layer for discovery, enabling enterprise transparency by clearly showing what evidence was inferred, from which source, and with what level of confidence.
            </div>
            <div className="mt-1 text-sm text-muted">
              Run ID: <span className="font-semibold text-text">{runId}</span>
            </div>
          </div>
        </div>

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

          <div className="rounded-xl border border-border bg-panel p-4">
            <div className="flex items-center justify-between">
              <div className="text-xl font-semibold text-text pb-3">
                {filteredEvidence.length} Evidence Snippets
              </div>
              <label className="cursor-pointer text-xs text-muted">
                <span className="flex items-center gap-2">
                  <input
                    type="checkbox"
                    className="accent-[#0D55D7]"
                    checked={saveDraftEnabled}
                    onChange={(e) => {
                      setSaveDraftEnabled(e.target.checked);
                      push(e.target.checked ? 'Auto-save enabled (mock).' : 'Auto-save paused (mock).');
                    }}
                  />
                  Save Draft
                </span>
              </label>
            </div>

            <div className="mt-3 flex gap-2">
              <div className="relative flex-1">
                <input
                  value={queryEvidence}
                  onChange={(e) => setQueryEvidence(e.target.value)}
                  placeholder="Search evidence..."
                  className="w-full appearance-none rounded-md border border-border bg-bg/30 px-3 py-2 pr-10 text-sm text-text placeholder:text-muted transition-colors hover:border-[#0D55D7]/50 hover:bg-bg/50 focus:border-[#0D55D7] focus:outline-none focus:ring-2 focus:ring-[#0D55D7]/50"
                  data-testid="evidence-search"
                />
                <Search className="pointer-events-none absolute right-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              </div>

              <select
                value={sourceFilter}
                onChange={(e) => setSourceFilter(e.target.value)}
                className="cursor-pointer whitespace-nowrap rounded-lg border border-border bg-panel px-3 py-2 text-sm text-text focus:outline-none focus:ring-1 focus:ring-accent"
                data-testid="source-filter"
              >
                {sources.map((source) => (
                  <option key={source} value={source}>
                    {source}
                  </option>
                ))}
              </select>
            </div>

            <div className="mt-3 h-[520px] overflow-y-auto rounded-lg border border-border bg-bg/20" data-testid="evidence-card-list">
              {filteredEvidence.length === 0 ? (
                <div className="py-4 text-center text-sm text-muted">
                  No evidence matches the current filters.
                </div>
              ) : (
                filteredEvidence.map((ev) => (
                  <EvidenceCard
                    key={ev.id}
                    evidence={ev}
                    selected={ev.id === selectedEvidenceId}
                    onSelect={selectEvidence}
                    opportunities={opportunities}
                  />
                ))
              )}
            </div>

            <div className="mt-3 flex items-center justify-between text-sm text-text">
              <button
                type="button"
                disabled={!canPrev}
                onClick={() => goPrev()}
                className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
              >
                <ChevronLeft className="h-4 w-4" /> Prev
              </button>
              <span>{positionLabel}</span>
              <button
                type="button"
                disabled={!canNext}
                onClick={() => goNext()}
                className="flex items-center gap-1 rounded border border-border bg-bg/40 px-4 py-2 text-sm font-medium text-text transition-colors hover:bg-bg/60 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Next <ChevronRight className="h-4 w-4" />
              </button>
            </div>
          </div>

          <EvidenceViewer
            evidence={selectedEvidence}
            positionLabel={positionLabel}
            onPrev={() => goPrev()}
            onNext={() => goNext()}
            onApprove={() => {
              const ok = approveSelected();
              if (ok) push('Approved. Moved to next unreviewed item.');
              else push("Decision finalized. It can't be changed now.");
            }}
            onReject={() => {
              const ok = rejectSelected();
              if (ok) push('Rejected. Moved to next unreviewed item.');
              else push("Decision finalized. It can't be changed now.");
            }}
          />
        </div>
      </div>
    </div>
  );
}
