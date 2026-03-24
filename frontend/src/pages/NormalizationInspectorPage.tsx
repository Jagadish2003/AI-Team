import React, { useState, useEffect } from 'react';
import TopNav from '../components/common/TopNav';
import LoadingPanel from '../components/common/LoadingPanel';
import ErrorPanel from '../components/common/ErrorPanel';
import { useNormalizationContext } from '../context/NormalizationContext';
import ConfidenceBanner from '../components/normalization/ConfidenceBanner';
import SourcesEntitiesPanel from '../components/normalization/SourcesEntitiesPanel';
import MappingTable from '../components/normalization/MappingTable';
import FieldDetailsPanel from '../components/normalization/FieldDetailsPanel';
import { useNavigate } from 'react-router-dom';

const persistedSources = { current: new Set<string>() };
const persistedEntities = { current: new Set<string>() };

export default function NormalizationInspectorPage() {
  const {
    confidence,
    counts,
    setSourceFilter,
    setEntityFilter,
    permissionsLoading,
    permissionsError,
    refetchPermissions
  } = useNormalizationContext();
  const nav = useNavigate();

  const [selectedSources, setSelectedSources] = useState<Set<string>>(new Set(persistedSources.current));
  const [selectedEntities, setSelectedEntities] = useState<Set<string>>(new Set(persistedEntities.current));

  const total = counts.MAPPED + counts.AMBIGUOUS + counts.UNMAPPED;

  useEffect(() => { persistedSources.current = new Set(selectedSources); }, [selectedSources]);
  useEffect(() => { persistedEntities.current = new Set(selectedEntities); }, [selectedEntities]);

  useEffect(() => {
    setSourceFilter(selectedSources.size === 0 ? 'All Sources' : Array.from(selectedSources).join(','));
    setEntityFilter(selectedEntities.size === 0 ? 'All Entities' : Array.from(selectedEntities).join(','));
  }, [selectedSources, selectedEntities, setSourceFilter, setEntityFilter]);

  // --- Task 5: Handle Loading and Error States ---
  if (permissionsLoading) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="p-6">
          <LoadingPanel title="Loading permissions" subtitle="Fetching permission catalog…" />
        </div>
      </div>
    );
  }

  if (permissionsError) {
    return (
      <div className="min-h-screen text-text">
        <TopNav />
        <div className="p-6">
          <ErrorPanel message={permissionsError} onRetry={refetchPermissions} title="Could not load permissions" />
        </div>
      </div>
    );
  }
  // --- End Task 5 ---

  return (
    <div className="min-h-screen text-text">
      <TopNav />
      <div className="w-full px-8 py-6 pb-10">
        <div className="mb-4">
          <div className="text-2xl font-semibold">Normalization &amp; Mapping Inspector</div>
          <div className="mt-1 text-sm text-muted">
            Transparency view: inspect how fields from sources map into AgentIQ&apos;s common schema.
          </div>
        </div>

        <ConfidenceBanner conf={confidence} onAction={() => nav('/integration-hub')} />

        <div className="mt-4 flex gap-6">
          <div className="flex flex-col gap-4" style={{ flex: 3 }}>
            <div className="flex gap-6" style={{ height: '700px' }}>
              <div className="flex flex-col" style={{ flex: 1 }}>
                <SourcesEntitiesPanel
                  selectedSources={selectedSources}
                  setSelectedSources={setSelectedSources}
                  selectedEntities={selectedEntities}
                  setSelectedEntities={setSelectedEntities}
                />
              </div>
              <div className="flex flex-col" style={{ flex: 2 }}>
                <MappingTable />
              </div>
            </div>

            <div className="flex items-center gap-8 rounded-xl border border-border bg-panel px-6 py-3 text-sm">
              <div className="flex items-center gap-2">
                <span className="h-2.5 w-2.5 rounded-sm bg-[#00B4B4]" />
                <span className="font-semibold text-text">Mapped</span>
                <span className="font-bold text-text">{counts.MAPPED}</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="font-semibold text-muted">{counts.UNMAPPED}</span>
                <span className="text-muted">Unmapped</span>
              </div>
              <div className="flex items-center gap-2">
                <span className="font-semibold text-muted">{counts.AMBIGUOUS}</span>
                <span className="text-muted">Ambiguous</span>
              </div>
              <div className="ml-auto flex items-center gap-2">
                <span className="text-muted">Total</span>
                <span className="font-bold text-text">{total}</span>
              </div>
            </div>
          </div>

          <div className="flex flex-col" style={{ flex: 1 }}>
            <FieldDetailsPanel />
          </div>
        </div>
      </div>
    </div>
  );
}