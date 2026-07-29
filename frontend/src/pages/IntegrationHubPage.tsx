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
 *   - Start Discovery Run is managed from the Discovery Run page
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
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import PageShell from '../components/common/PageShell';
import { Skeleton } from '../components/common/Skeleton';
import ErrorPanel from '../components/common/ErrorPanel';
import ConnectorGroupSection, { GroupConfig } from '../components/integrations/ConnectorGroupSection';
import RightPanel from '../components/integrations/RightPanel';
import LicenseLimitBanner, { systemLimitMessage } from '../components/integrations/LicenseLimitBanner';
import ConfirmDialog from '../components/common/ConfirmDialog';
import { useToast } from '../components/common/Toast';
import { ApiError } from '../lib/apiClient';
import { useResource, useDataCache } from '../lib/dataCache';
import { cacheKeys } from '../lib/cacheKeys';
import { useConnectorContext } from '../context/ConnectorContext';
import { useAuthOptional } from '../context/AuthContext';
import { isViewerRole } from '../utils/roles';
import { isDiscoveryReadyConnector } from '../utils/sourceReadiness';
import { computeConfidence } from '../utils/confidence';
import { Connector, OutboundSetupRequest } from '../types/connector';
import { fetchLicenseLimits } from '../api/licenseApi';
import type { LicenseLimitsResponse } from '../types/license';

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

const START_BAR_SOURCE_IDS = ['salesforce', 'servicenow', 'jira'];

// MSP-B13 (AT-748): the Cloud Operations group is NOT a hardcoded id list.
// Membership is derived from the catalog itself — any connector the catalog marks
// `multiScope` (AWS/Azure Events today, future cloud connectors automatically)
// registers here — so no connector tile is hardcoded (T6-AC1/AC4).
const CLOUD_OPS_CATEGORY = 'cloud_operations';

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
  {
    categoryId: CLOUD_OPS_CATEGORY,
    label:      'Cloud Operations',
    subLabel:   'Multi-account/subscription cloud event connectors (AWS, Azure)',
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
    disconnectConnector,
    loading,
    error,
    refetch,
  } = useConnectorContext();

  const { push }         = useToast();
  const navigate         = useNavigate();
  const location         = useLocation();
  const [searchParams]   = useSearchParams();
  const auth             = useAuthOptional();
  const isViewer         = isViewerRole(auth?.user?.role);

  // Connect → View data (no manual "Configure & Sync" step). Configuring a
  // connector is now just a flag-flip that makes it discovery-ready, so it is
  // done automatically: any connector that is connected but not yet configured is
  // configured here in the background. This covers every connect path (OAuth
  // callback, client-credentials, static) AND any connector that was left
  // connected-but-unconfigured before this change — so its tile flips straight to
  // "View data". analyst+ only (configuring is a write; viewers can't); guarded by
  // a ref so each id is attempted once and the post-configure refetch can't loop.
  const autoConfiguredRef = useRef<Set<string>>(new Set());

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

  // Regularly re-check connector token expiry so a token that lapses WHILE the
  // user is on this page surfaces as "Token expired" / Reconnect without a manual
  // reload. Invalidating each connected connector's token-status cache key makes
  // that tile's useResource refetch and re-render its action. Only connected
  // connectors hold a token worth checking.
  const cache = useDataCache();
  useEffect(() => {
    const REVALIDATE_MS = 120_000; // 2 minutes
    const timer = setInterval(() => {
      for (const c of allConnectors) {
        if (c.status === 'connected') {
          cache.invalidate(cacheKeys.connectorTokenStatus(c.id));
        }
      }
    }, REVALIDATE_MS);
    return () => clearInterval(timer);
  }, [allConnectors, cache]);

  // Auto-configure connected-but-unconfigured connectors (Connect → View data).
  // See autoConfiguredRef above for the rationale. Runs whenever the connector
  // set changes; each id is attempted at most once (the ref), and after configure
  // the connector reports configured=true so it no longer matches.
  useEffect(() => {
    if (isViewer) return; // configuring is analyst+; viewers can't
    for (const c of allConnectors) {
      if (
        c.status === 'connected' &&
        !c.configured &&
        !autoConfiguredRef.current.has(c.id)
      ) {
        autoConfiguredRef.current.add(c.id);
        configureSync(c.id);
      }
    }
  }, [allConnectors, isViewer, configureSync]);


  // R17-D4 Addendum A / T11 (AT-506): license-limit state (systems used /
  // licensed) from T10's GET /api/license/limits. Re-fetched whenever the
  // connector set changes — i.e. after a connect/disconnect/configure round-trip
  // — so the count and the connect gate reflect a newly installed key or a fresh
  // connection with no restart (AC11). Fail-open: a fetch error leaves it null so
  // the UI never wrongly blocks — the backend gate (T9) remains the source of
  // truth for enforcement regardless of what the hub shows.
  // On the SHARED cache, not page-local state: the cache lives at the app root,
  // so navigating away and back re-renders the strip from the cached value with
  // NO refetch and no skeleton. It refreshes only when it should — a
  // connect/disconnect invalidates this key (ConnectorContext), and any other
  // user's change arrives via the org event stream — and those refreshes are
  // background (the current value stays on screen). A fetch error resolves to
  // null, i.e. the strip hides: fail-open, since the backend gate (T9) remains
  // the source of truth for enforcement (AC11).
  const {
    data: licenseData,
    loading: licenseLoading,
  } = useResource<LicenseLimitsResponse | null>(cacheKeys.license, fetchLicenseLimits);
  const licenseLimits = licenseData ?? null;

  // At/over the licensed cap → block NEW connections (forward-only; reconnecting
  // an already-connected system is never blocked, handled per-tile via isConnected
  // and enforced server-side). Unlimited / unknown (null) never blocks.
  const atSystemLimit = licenseLimits ? !licenseLimits.canConnectMore : false;
  const systemLimitMsg =
    licenseLimits && licenseLimits.systemsLicensed != null
      ? systemLimitMessage(licenseLimits.systemsLicensed)
      : '';

  // OAuth result feedback (CS-2 / AT-327 T5).
  // OAuthCallbackPage navigates here after the provider round-trip with
  // location.state.justConnected (success) or location.state.oauthError
  // (failure). Show a toast once, then clear the history-entry state so a
  // re-render or back-navigation does not re-fire it. ?category= lives in the
  // search string, not state, so preserving location.search keeps the deep-link
  // behaviour intact.
  const oauthToastShown = useRef(false);
  useEffect(() => {
    const oauthState = location.state as
      | { justConnected?: string; oauthError?: string }
      | null;
    if (!oauthState || oauthToastShown.current) return;

    if (oauthState.justConnected) {
      // Wait until the connector list has loaded so the toast can name the
      // connector (T5-AC3) rather than falling back to its id. The effect
      // re-runs when `loading` flips; the ref guard keeps it single-fire.
      if (loading) return;
      oauthToastShown.current = true;
      const name =
        allConnectors.find(c => c.id === oauthState.justConnected)?.name ??
        oauthState.justConnected;
      push(`${name} connected successfully`, 'success');
      navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
    } else if (oauthState.oauthError) {
      oauthToastShown.current = true;
      push(`Connection failed: ${oauthState.oauthError}`, 'error');
      navigate(`${location.pathname}${location.search}`, { replace: true, state: null });
    }
  }, [location.state, location.pathname, location.search, allConnectors, loading, push, navigate]);

  // Build groups. The Cloud Operations group is catalog-driven — its membership
  // is every connector the catalog flags `multiScope`, not a hardcoded id list
  // (MSP-B13 / AT-748, T6-AC1/AC4). The other groups keep their category map.
  const groups: GroupConfig[] = useMemo(
    () =>
      GROUP_META.map(meta => {
        const connectors =
          meta.categoryId === CLOUD_OPS_CATEGORY
            ? allConnectors.filter(c => c.multiScope)
            : allConnectors.filter(c => connectorBelongsToCategory(c, meta.categoryId));
        return {
          ...meta,
          allSystemIds:
            meta.categoryId === CLOUD_OPS_CATEGORY
              ? connectors.map(c => c.id)
              : CATEGORY_SYSTEMS[meta.categoryId] ?? [],
          connectors,
        };
      }),
    [allConnectors],
  );

  // Selected connector
  const selected = useMemo(
    () => allConnectors.find(c => c.id === selectedConnectorId) ?? null,
    [allConnectors, selectedConnectorId],
  );

  const startBarStatusConnectors = useMemo(() => {
    return START_BAR_SOURCE_IDS
      .map(id => allConnectors.find(c => c.id === id))
      .filter((connector): connector is Connector => Boolean(connector));
  }, [allConnectors]);

  const startBarReadyCount = useMemo(
    () => startBarStatusConnectors.filter(isDiscoveryReadyConnector).length,
    [startBarStatusConnectors],
  );

  const startBarConfidence = useMemo(
    () => computeConfidence(startBarReadyCount),
    [startBarReadyCount],
  );

  const startBarNext = useMemo(
    () => startBarStatusConnectors.find(c => !isDiscoveryReadyConnector(c)) ?? null,
    [startBarStatusConnectors],
  );

  // One-shot Connect: starting a connect flow mints a one-time OAuth state nonce
  // and then hands the browser to the provider, so the button must only ever be
  // clickable once. This holds the id of the connector whose flow is in flight —
  // its action button disables and reads "Connecting…", the same posture as the
  // Stack Builder "Start discovery" button while a run is launching.
  const [connectingId, setConnectingId] = useState<string | null>(null);

  async function startConnect(id: string) {
    if (connectingId) return;
    setConnectingId(id);
    const started = await connectConnector(id);
    // On success the browser is already navigating to the provider — keep the
    // button busy until it leaves the page. Only a failed auth-url call releases
    // it, so the user can retry.
    if (!started) setConnectingId(null);
  }

  // Connect / configure handler (same logic as pre-Sprint-9)
  function handlePrimary(id: string) {
    const c = allConnectors.find(x => x.id === id);
    if (!c) return;
    // R191-R1 T5 (AT-726): a roadmap connector (SAP/D365 and other unshipped
    // tiles) is not connectable. The tile's action is already disabled; this
    // guards the handler and surfaces the honest reason if it is ever reached.
    if (c.roadmap) {
      const target = c.roadmapTarget && /\d/.test(c.roadmapTarget) ? c.roadmapTarget : null;
      push(
        target
          ? `${c.name} is on the roadmap — coming in ${target}.`
          : `${c.name} is on the roadmap and is not yet connectable.`,
      );
      return;
    }
    // Forward-only license gate (AC10): block only a NEW connection at the limit.
    // Connecting an already-connected system (Configure/View/Reconnect) is never
    // blocked. The tile's Connect button is already disabled in this state; this
    // guard defends the handler and surfaces the clear message if it is reached.
    if (atSystemLimit && c.status !== 'connected') {
      push(systemLimitMsg || 'Your license limit has been reached. Contact CloudFulcrum to add more.', 'error');
      return;
    }
    if (c.status === 'connected' && !c.configured) {
      configureSync(id);
      push('Configuration complete. Data is now synced.');
    } else if (c.status === 'connected') {
      push('Coming Soon');
    } else if (c.status === 'coming_soon') {
      push('Coming Soon');
    } else {
      void startConnect(id);
      // Connect now leads straight to "View data" — configuring is automatic
      // (see the auto-configure effect). For OAuth connectors this toast is not
      // seen (the browser redirects to the provider), but keep it accurate.
      push('Connecting…');
    }
  }

  // Reconnect handler (CS-2 AC7). Fired from a tile whose token is expired /
  // refresh-failed. Re-runs the OAuth flow (auth-url → provider redirect) via
  // the same context method Connect uses — connectConnector navigates the
  // browser away, so no follow-up toast is needed here.
  function handleReconnect(id: string) {
    void startConnect(id);
  }

  // R18-C0 P4 / AT-566: disconnect flow. The tile's Disconnect action opens a
  // confirmation dialog (prevents accidental credential removal); confirming
  // clears the org vault credential for that connector and returns the tile to
  // its unconnected state.
  const [disconnectTarget, setDisconnectTarget] = useState<Connector | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);

  // R18-A3 follow-up: a one-shot request that pops a connector's outbound /
  // credential setup modal when the tile's "Set up outbound access" button is
  // clicked. Selecting mounts the detail panel (which hosts the modal); the
  // nonce bump makes the relevant setup manager open its modal immediately, so
  // the button no longer looks inert when the panel is already showing.
  const [outboundSetupRequest, setOutboundSetupRequest] =
    useState<OutboundSetupRequest | null>(null);

  function handleSetupOutbound(id: string) {
    selectConnector(id);
    setOutboundSetupRequest(prev => ({ connectorId: id, nonce: (prev?.nonce ?? 0) + 1 }));
  }

  function handleDisconnect(id: string) {
    const c = allConnectors.find(x => x.id === id);
    if (!c) return;
    setDisconnectTarget(c);
  }

  async function confirmDisconnect() {
    if (!disconnectTarget) return;
    const { id, name } = disconnectTarget;
    setDisconnecting(true);
    try {
      await disconnectConnector(id);
      push(`${name} disconnected.`, 'success');
      setDisconnectTarget(null);
    } catch (err) {
      const detail =
        err instanceof ApiError && typeof (err.body as any)?.detail === 'string'
          ? (err.body as any).detail
          : `Could not disconnect ${name}.`;
      push(detail, 'error');
    } finally {
      setDisconnecting(false);
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
    <PageShell
      title="Integration Hub"
      description="Connect enterprise systems to provide data for discovery. Manage credentials and connection status for your workspace."
    >
        {loading && (
          // Skeleton mirrors the groups-column tile grid + side panel so the real
          // tiles fill the same space instead of snapping in after a spinner.
          <div
            aria-busy="true"
            aria-label="Loading Integration Hub"
            className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]"
          >
            <div className="flex min-w-0 flex-col gap-6">
              {[0, 1].map((g) => (
                <div key={g}>
                  <Skeleton className="mb-3 h-5 w-48" />
                  <div className="grid gap-4 sm:grid-cols-2">
                    {[0, 1, 2, 3].map((t) => (
                      <Skeleton key={t} className="h-32 w-full rounded-xl" />
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <Skeleton className="h-96 w-full rounded-xl" />
          </div>
        )}
        {error && !loading && <ErrorPanel message={error} onRetry={refetch} />}

        {!loading && !error && (
          <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">

            {/* Groups column */}
            <div className="flex min-w-0 flex-col gap-4">

              {/* R17-D4 Addendum A / T11: systems used vs licensed (AC14) */}
              <LicenseLimitBanner limits={licenseLimits} loading={licenseLoading} />

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
                    onReconnect={handleReconnect}
                    onDisconnect={handleDisconnect}
                    onSetupOutbound={handleSetupOutbound}
                    onAddSource={handleAddSource}
                    connectBlocked={atSystemLimit}
                    connectBlockMessage={systemLimitMsg}
                    connectingId={connectingId}
                  />
                </div>
              ))}
            </div>

            {/* Right panel — unchanged */}
            <div className="min-w-0">
              <RightPanel
                selected={selected}
                outboundSetupRequest={outboundSetupRequest}
                onConfigure={() => {
                  if (!selected) return;
                  configureSync(selected.id);
                  push('Configuration complete. Data is now synced.');
                }}
                confidence={startBarConfidence}
                recommendedConnectedCount={startBarReadyCount}
                recommendedTotal={3}
                next={startBarNext}
                connectingNext={Boolean(startBarNext) && connectingId === startBarNext?.id}
                onConnectNext={() => {
                  if (!startBarNext) return;
                  if (startBarNext.status === 'connected') {
                    configureSync(startBarNext.id);
                    push('Configuration complete. Data is now synced.');
                  } else if (atSystemLimit) {
                    // Forward-only: the next-best source is not yet connected, so
                    // it is a NEW system — blocked at the limit with the clear
                    // message, consistent with the disabled tile Connect buttons.
                    push(systemLimitMsg || 'Your license limit has been reached. Contact CloudFulcrum to add more.', 'error');
                  } else {
                    void startConnect(startBarNext.id);
                    push('Connecting…');
                  }
                }}
              />
            </div>
          </div>
        )}

        {/* R18-C0 P4 / AT-566: disconnect confirmation. */}
        <ConfirmDialog
          open={disconnectTarget !== null}
          title="Disconnect connector"
          message={
            <>
              Disconnect <span className="font-semibold text-text">{disconnectTarget?.name}</span>?
              This clears the stored credential for this workspace and returns the
              connector to its unconnected state. You can reconnect it at any time.
            </>
          }
          confirmLabel="Disconnect"
          busyLabel="Disconnecting…"
          busy={disconnecting}
          onConfirm={confirmDisconnect}
          onCancel={() => { if (!disconnecting) setDisconnectTarget(null); }}
        />
    </PageShell>
  );
}
