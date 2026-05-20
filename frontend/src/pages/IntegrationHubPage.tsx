/**
 * IntegrationHubPage — ENG-IH-2 Sprint 9
 *
 * Redesigned Integration Hub — Group A/B/C taxonomy layout.
 *
 * WHAT CHANGED FROM PRE-SPRINT-9:
 *   Old: Two flat sections — recommended (hero cards) and standard (grid)
 *   New: Four capability groups matching Stack Builder Screen 2 taxonomy:
 *     Group A — Primary Business Platforms
 *     Group B — Operational Systems
 *     Group C — Communications & Knowledge
 *     Group D — Data & Engineering Sources
 *
 * The Group A/B/C/D taxonomy is identical to the Stack Builder Screen 2
 * grouping. This creates a consistent mental model — the user sees the
 * same organisation in both places.
 *
 * ?category= deep-link (ENG-IH-2 AC6):
 *   Stack Builder Screen 2 "Connect in Integration Hub" CTAs navigate to:
 *     /integration-hub?category=comms_knowledge
 *   This page reads the ?category= param on mount and opens the connector
 *   picker pre-filtered to that category by scrolling to the relevant group
 *   and highlighting it.
 *   Supported values: primary_platforms, operational_systems,
 *     comms_knowledge, data_engineering
 *   If param is absent or unknown — normal page load, no filter applied.
 *
 * WHAT DID NOT CHANGE:
 *   - Connect / Configure flow unchanged — same ConnectorTile, same handlers
 *   - DiscoveryStartBar unchanged
 *   - RightPanel unchanged
 *   - ConnectorContext unchanged
 *   - Dark theme preserved — bg-panel, border-border, text-muted tokens only
 *
 * Dark theme note (ENG-IH-2 AC5):
 *   Integration Hub uses existing dark surface tokens throughout.
 *   bg-panel for card surfaces, border-border for card borders.
 *   No bg-white, bg-slate-50, or hardcoded light background anywhere.
 *
 * Accessibility:
 *   Each group section has a region role with aria-label.
 *   "Add a source" CTA has focus ring and keyboard support.
 *   ?category= highlighted group has aria-live="polite" announcement.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import PageShell from '../components/common/PageShell';
import LoadingPanel from '../components/common/LoadingPanel';
import ErrorPanel from '../components/common/ErrorPanel';
import ConnectorGroupSection, { GroupConfig } from '../components/integrations/ConnectorGroupSection';
import RightPanel from '../components/integrations/RightPanel';
import DiscoveryStartBar from '../components/integrations/DiscoveryStartBar';
import { useToast } from '../components/common/Toast';
import { useConnectorContext } from '../context/ConnectorContext';
import { useRunContext } from '../context/RunContext';
import { useSourceIntakeContext } from '../context/SourceIntakeContext';
import { isDiscoveryReadyConnector } from '../utils/sourceReadiness';
import { Connector } from '../types/connector';

// ── Category → system ID membership ─────────────────────────────────────────
// Mirrors SYSTEM_CATEGORY in routes_workspace_catalog.py exactly.
// Neospin and Vitech removed per ENG-IH-4.

const CATEGORY_SYSTEMS: Record<string, string[]> = {
  primary_platforms: [
    'salesforce', 'sap', 'oracle_ebs', 'workday', 'dynamics365',
  ],
  operational_systems: [
    'jira', 'jira_confluence', 'servicenow', 'azure_devops', 'linear', 'zendesk',
  ],
  comms_knowledge: [
    'slack', 'teams', 'm365', 'confluence', 'sharepoint', 'notion',
  ],
  data_engineering: [
    'github', 'gitlab', 'bitbucket', 'azure_repos',
    'postgresql', 'sql_server', 'oracle_db', 'databricks', 'snowflake', 'dbt',
  ],
};

// ── Group metadata ────────────────────────────────────────────────────────────

interface GroupMeta {
  categoryId: string;
  label:      string;
  subLabel:   string;
}

const GROUP_META: GroupMeta[] = [
  {
    categoryId: 'primary_platforms',
    label:      'Primary Business Platforms',
    subLabel:   'Where your operation\'s core workflows live',
  },
  {
    categoryId: 'operational_systems',
    label:      'Operational Systems',
    subLabel:   'Work tracking, ITSM, and operational signal sources',
  },
  {
    categoryId: 'comms_knowledge',
    label:      'Communications & Knowledge',
    subLabel:   'Communication signals and documentation sources',
  },
  {
    categoryId: 'data_engineering',
    label:      'Data & Engineering Sources',
    subLabel:   'Source control, databases, and data platform connectors',
  },
];

// ── Helpers ───────────────────────────────────────────────────────────────────

function connectorBelongsToCategory(connector: Connector, categoryId: string): boolean {
  const systemIds = CATEGORY_SYSTEMS[categoryId] ?? [];
  return systemIds.includes(connector.id);
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function IntegrationHubPage() {
  const {
    recommended,
    standard,
    selectedConnectorId,
    selectConnector,
    connectConnector,
    configureSync,
    confidence,
    recommendedConnectedCount,
    nextBestRecommendedId,
    loading,
    error,
    refetch,
  } = useConnectorContext();

  const { push }         = useToast();
  const navigate         = useNavigate();
  const [searchParams]   = useSearchParams();
  const { runId }        = useRunContext();
  const { uploadedFiles } = useSourceIntakeContext();

  // ?category= deep-link param — ENG-IH-2 AC6
  const deepLinkCategory = searchParams.get('category') ?? null;
  const groupRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const [highlightedCategory, setHighlightedCategory] = useState<string | null>(null);

  // Scroll to and highlight the deep-linked category on mount
  useEffect(() => {
    if (!deepLinkCategory || loading) return;
    const validCategories = Object.keys(CATEGORY_SYSTEMS);
    if (!validCategories.includes(deepLinkCategory)) return;

    setHighlightedCategory(deepLinkCategory);
    const el = groupRefs.current[deepLinkCategory];
    if (el) {
      setTimeout(() => {
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 300);
    }

    // Clear highlight after 3s
    const timer = setTimeout(() => setHighlightedCategory(null), 3000);
    return () => clearTimeout(timer);
  }, [deepLinkCategory, loading]);

  // All connectors combined
  const allConnectors = useMemo(
    () => [...recommended, ...standard],
    [recommended, standard],
  );

  // Build groups
  const groups: GroupConfig[] = useMemo(
    () =>
      GROUP_META.map(meta => ({
        ...meta,
        allSystemIds: CATEGORY_SYSTEMS[meta.categoryId] ?? [],
        connectors: allConnectors.filter(c =>
          connectorBelongsToCategory(c, meta.categoryId)
        ),
      })),
    [allConnectors],
  );

  // Selected connector
  const selected = useMemo(
    () => allConnectors.find(c => c.id === selectedConnectorId) ?? null,
    [allConnectors, selectedConnectorId],
  );

  const next = useMemo(
    () => recommended.find(c => c.id === nextBestRecommendedId) ?? null,
    [recommended, nextBestRecommendedId],
  );

  const readyConnectorCount = useMemo(
    () => allConnectors.filter(isDiscoveryReadyConnector).length,
    [allConnectors],
  );

  const canStart = readyConnectorCount > 0 || uploadedFiles.length > 0;

  // Connect / configure handler (same logic as pre-Sprint-9)
  function handlePrimary(id: string) {
    const c = allConnectors.find(x => x.id === id);
    if (!c) return;
    if (c.status === 'connected' && !c.configured) {
      configureSync(id);
      push('Configuration complete. Data is now synced.');
    } else if (c.status === 'connected') {
      push('Data preview available in later Sprint.');
    } else if (c.status === 'coming_soon') {
      push('Connector coming soon.');
    } else {
      connectConnector(id);
      push('Connector connected. Click Configure & Sync to load data.');
    }
  }

  // "Add a source" CTA — navigate to /integration-hub?category={id}
  // When this is the current page (user clicked another group's CTA),
  // scroll to that group instead of navigating away.
  function handleAddSource(categoryId: string) {
    setHighlightedCategory(categoryId);
    const el = groupRefs.current[categoryId];
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      setTimeout(() => setHighlightedCategory(null), 3000);
    } else {
      navigate(`/integration-hub?category=${categoryId}`);
    }
  }

  return (
    <>
      <PageShell
        title="Integration Hub"
        description="Connect enterprise systems to provide data for discovery. Manage credentials and connection status for your workspace."
        contentClassName="pb-[190px] xl:pb-28"
      >
        {loading && <LoadingPanel />}
        {error && !loading && <ErrorPanel message={error} onRetry={refetch} />}

        {!loading && !error && (
          <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">

            {/* Groups column */}
            <div className="flex min-w-0 flex-col gap-4">

              {/* Deep-link announcement for screen readers */}
              {highlightedCategory && (
                <div aria-live="polite" className="sr-only">
                  {GROUP_META.find(g => g.categoryId === highlightedCategory)?.label} section highlighted.
                </div>
              )}

              {groups.map(group => (
                <div
                  key={group.categoryId}
                  ref={el => { groupRefs.current[group.categoryId] = el; }}
                  role="region"
                  aria-label={group.label}
                  className={[
                    'transition-all duration-300',
                    highlightedCategory === group.categoryId
                      ? 'rounded-xl ring-2 ring-accent/45 ring-offset-2 ring-offset-bg'
                      : '',
                  ].join(' ')}
                >
                  <ConnectorGroupSection
                    group={group}
                    selectedId={selectedConnectorId}
                    onSelect={id => {
                      selectConnector(id);
                    }}
                    onPrimary={handlePrimary}
                    onAddSource={handleAddSource}
                  />
                </div>
              ))}
            </div>

            {/* Right panel — unchanged */}
            <div className="min-w-0">
              <RightPanel
                selected={selected}
                onConfigure={() => {
                  if (!selected) return;
                  configureSync(selected.id);
                  push('Configuration complete. Data is now synced.');
                }}
                confidence={confidence}
                recommendedConnectedCount={recommendedConnectedCount}
                recommendedTotal={3}
                next={next}
                onConnectNext={() => {
                  if (!next) return;
                  if (next.status === 'connected') {
                    configureSync(next.id);
                    push('Configuration complete. Data is now synced.');
                  } else {
                    connectConnector(next.id);
                    push('Connected next best source.');
                  }
                }}
              />
            </div>
          </div>
        )}
      </PageShell>

      {/* Discovery start bar — unchanged */}
      {!loading && !error && (
        <DiscoveryStartBar
          confidence={confidence}
          recommendedReadyCount={recommendedConnectedCount}
          recommendedTotal={3}
          recommended={recommended}
          canStart={canStart}
          onStart={() => {
            if (runId) {
              navigate(`/discovery-run?runId=${runId}`);
            } else {
              navigate('/discovery-run', { state: { autoStart: true } });
            }
          }}
        />
      )}
    </>
  );
}
