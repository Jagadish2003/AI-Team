/**
 * StackBuilderPage — Sprint 7 Final Wiring + Sprint 8 Foundation
 *
 * Combines StackBuilderRouter and StackBuilderRouterPage into a single
 * page-level component. Owns useSetupState(), handles session persistence,
 * routes between the 4 child pages, and translates state to /launch + /compute.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useRunContext } from '../context/RunContext';
import { useAuthOptional } from '../context/AuthContext';
import { isViewerRole } from '../utils/roles';

// Import components and types
import { CheckCircle2, Database, Layers3, Target } from 'lucide-react';
import type { WorkspaceCatalogResponse } from '../types/workspace_catalog';
import { getCatalogSystemIds } from '../types/workspace_catalog';
import { fetchTokenStatus, type TokenStatus } from '../services/staticApi';
import { useToast } from '../components/common/Toast';
import { useResource } from '../lib/dataCache';
import { Skeleton } from '../components/common/Skeleton';
import { cacheKeys } from '../lib/cacheKeys';
import type {
  SetupState,
  SystemWeighting,
  IndustryListItem,
  TemplateListItem,
  SystemDefaultItem,
} from '../types/stack_builder';
import PageShell from '../components/common/PageShell';
import InlineError from '../components/common/InlineError';
import {
  DiscoveryConfidenceBar,
  LendingFirstRunGuide,
  StackBuilderProgressBar,
  useSetupState,
} from '../components/stack_builder';
import type { LendingGuideLaunchState } from '../components/stack_builder';
import {
  fetchIndustries,
  fetchTemplates,
  fetchIndustrySystemDefaults,
} from '../api/stackBuilderApi';

// Import the 4 inner screens
import DiscoveryFocusPage from './DiscoveryFocusPage';
import YourSystemsPage from './YourSystemsPage';
import SourceWeightingPage from './SourceWeightingPage';
import DiscoveryPlanPage from './DiscoveryPlanPage';

// ── Static Definitions ───────────────────────────────────────────────────────

// R16-C1 T5 — Truthfulness check.
// Step 3's "weight evidence correctly" promise is intentionally retained:
// after R16-C1 T1–T4 the discovery engine now reads the per-system role and
// priority from the persisted Stack Builder configuration and applies a
// deterministic, bounded weighting (ROLE_WEIGHT in
// backend/discovery/weighting_context.py). The copy is no longer a
// credibility trap — it describes real engine behavior. Do not soften or
// remove it without first confirming the backend wiring still holds.
export const STEP_COPY: Record<number, { title: string; description: string }> = {
  1: {
    title: 'Stack Builder',
    description: 'Choose the discovery focus and optional accelerators that shape the initial analysis.',
  },
  2: {
    title: 'Stack Builder',
    description: 'Map the systems that show how work moves, where signals live, and which sources are ready.',
  },
  3: {
    title: 'Stack Builder',
    description: 'Confirm source roles and priorities so discovery can weight evidence correctly.',
  },
  4: {
    title: 'Stack Builder',
    description: 'Review the launch plan, expected evidence quality, and final discovery inputs.',
  },
};

const FOCUS_LABELS: Record<string, string> = {
  member_customer_service: 'Member / customer service',
  core_operations: 'Core operations',
  approvals_compliance: 'Approvals / compliance',
  cross_system_handoffs: 'Cross-system handoffs',
  back_office_productivity: 'Back-office productivity',
  engineering_change: 'Engineering / change',
  enterprise_wide: 'Enterprise-wide discovery',
};

const SALESFORCE_CLOUD_IDS = new Set([
  'salesforce_pss',
  'salesforce_sc',
  'salesforce_ncino',
  'salesforce_fsc',
  'salesforce_rc',
  'salesforce_hc',
]);

const CLOUD_PACK_REGISTRY: Record<string, string> = {
  salesforce_pss: 'strs_benefits',
  salesforce_sc: 'service_cloud',
  salesforce_ncino: 'ncino',
  salesforce_fsc: 'service_cloud',
  salesforce_rc: 'service_cloud',
  salesforce_hc: 'service_cloud',
};

// ── Helpers ──────────────────────────────────────────────────────────────────

const ORG_ID_HEADER = (import.meta.env.VITE_ORG_ID as string | undefined)?.trim();

function buildAuthHeaders(token: string) {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
    ...(ORG_ID_HEADER ? { 'X-Org-Id': ORG_ID_HEADER } : {}),
  };
}

function getCatalogSalesforceProducts(catalog: WorkspaceCatalogResponse | null): string[] {
  const salesforce = catalog?.primary_platforms?.find(
    system => system.system_id === 'salesforce',
  );
  return Array.isArray(salesforce?.products) ? salesforce.products : [];
}

// The Salesforce pack is driven SOLELY by the workspace product declaration
// (Integration Hub → "Salesforce products in use"), persisted on the connector
// record and surfaced here via the workspace catalog. Stack Builder system
// pre-selection (selectedSystemIds / selectedSalesforceClouds) no longer
// influences the pack: a default preselect of `salesforce_ncino` was silently
// forcing the nCino pack on orgs that had declared nothing, so runs picked
// detectors the org's data could never fire. With no declaration we fall to the
// industry hint, then a safe service_cloud default — never a preselected cloud.
// Exported for direct unit testing.
export function resolvePackId(
  state: ReturnType<typeof useSetupState>['state'],
  catalog: WorkspaceCatalogResponse | null,
  industries: IndustryListItem[],
  templates: TemplateListItem[],
): string {
  // An explicit Step 4 choice is authoritative and is what makes template and
  // industry pack defaults editable before launch.
  if (state.packId) return state.packId;

  const selectedTemplate = templates.find(
    template => template.template_id === state.templateId,
  );
  if (selectedTemplate?.pack_id) {
    return selectedTemplate.pack_id;
  }

  // The Salesforce pack is then driven SOLELY by the workspace product
  // declaration (the declared cloud from the catalog) — never a Stack Builder
  // pre-selection, which was silently forcing the nCino pack (f2026ee). See the
  // header comment above.
  const declaredCloud = getCatalogSalesforceProducts(catalog)
    .find(productId => CLOUD_PACK_REGISTRY[productId]);

  if (declaredCloud) {
    return CLOUD_PACK_REGISTRY[declaredCloud];
  }

  // Fallback 1: industry hint, when an industry is selected. Industry pack hints
  // come from the registry response — there is deliberately no frontend mirror.
  if (state.industryId) {
    const hints = industries.find(
      industry => industry.industry_id === state.industryId,
    )?.pack_hints;
    if (hints && hints.length > 0) return hints[0];
  }

  // Fallback 2: safe default — never a Stack Builder pre-selection.
  return 'service_cloud';
}

// ── Pack resolution — a run's packs are the UNION of two sources ────────────────
//
// R191-P1: a run's packs are the UNION of:
//   • the SALESFORCE packs, fixed by the Integration Hub product declaration
//     (Service Cloud / nCino / … via CLOUD_PACK_REGISTRY), and
//   • the ANALYSIS packs, chosen per-run in the Discovery Plan multi-select
//     (state.packIds; the offerable set lives in src/data/analysisPacks.ts).
// The Salesforce products are NOT offered in the Discovery Plan — they are
// declared once in the Integration Hub.

// The Salesforce packs a workspace's declared products map to (fixed per run).
export function salesforcePacksFromCatalog(
  catalog: WorkspaceCatalogResponse | null,
): string[] {
  return Array.from(
    new Set(
      getCatalogSalesforceProducts(catalog)
        .map(productId => CLOUD_PACK_REGISTRY[productId])
        .filter((packId): packId is string => Boolean(packId)),
    ),
  );
}

// Resolve the full MULTI-pack selection for a run: the UNION of the fixed
// Salesforce packs (product declaration) and the chosen analysis packs
// (state.packIds — the Discovery Plan multi-select). Order-preserving and
// de-duplicated, Salesforce packs first. Falls back to the single resolved pack
// when neither is present.
export function resolvePackIds(
  state: ReturnType<typeof useSetupState>['state'],
  catalog: WorkspaceCatalogResponse | null,
  industries: IndustryListItem[],
  templates: TemplateListItem[],
): string[] {
  const salesforcePacks = salesforcePacksFromCatalog(catalog);
  const analysisPacks = (state.packIds ?? []).filter(Boolean);
  const all = Array.from(new Set([...salesforcePacks, ...analysisPacks]));
  if (all.length > 0) return all;
  // Nothing declared or selected — fall back to a single resolved pack.
  return [resolvePackId(state, catalog, industries, templates)];
}

// ── Launch payload builder ─────────────────────────────────────────────────────
//
// R16-C1 T5 — the configuration the customer selected is the configuration that
// runs. This pure builder is the single place the launch payload is assembled,
// so the "weight evidence correctly" promise on Screen 3 is enforceable: it
// always carries the per-system weightings (role + priority + confirmed +
// workflowFocus) captured and confirmed in the Stack Builder through to the
// backend launch endpoint, which persists them into the run's setup_context for
// the scorer and corroboration engine to read.

export interface StackBuilderLaunchPayload {
  org_id: string;
  focus_id: SetupState['focusId'];
  industry_id: SetupState['industryId'];
  template_id: SetupState['templateId'];
  template_ids: string[];
  selected_system_ids: string[];
  pack_id: string;
  // R191-P1 T5: the full multi-pack selection (order-preserving; first = primary).
  // The backend also accepts the singular pack_id for backward compatibility, and
  // reconciles the two — a single-pack launch sends a one-element pack_ids.
  pack_ids: string[];
  weightings: Record<string, SystemWeighting>;
}

export function buildStackBuilderLaunchPayload(
  state: SetupState,
  packId: string,
  orgId: string,
  packIds?: string[],
): StackBuilderLaunchPayload {
  // The full multi-pack selection sent to the backend. Prefer an explicit
  // `packIds` (handleLaunch passes resolvePackIds — the Salesforce-product packs
  // ∪ the chosen analysis packs); otherwise fall back to the state's own
  // packIds (e.g. multi-template selection), then the singular primary pack.
  const resolvedPackIds =
    packIds && packIds.length > 0
      ? packIds
      : state.packIds && state.packIds.length > 0
      ? state.packIds
      : [packId];
  // Surface the silent-mismatch case: every selected system should carry a
  // confirmed weighting. If one is missing (e.g. a system was selected but its
  // weighting was lost to a browser-state glitch before reaching launch), the
  // backend's load_for_run() falls back to NEUTRAL scoring for it with no error
  // — so the customer's "weight evidence correctly" promise would be partially
  // ignored without anyone knowing. Warn loudly so it is at least visible.
  const missingWeightings = state.selectedSystemIds.filter(
    id => !(id in state.weightings),
  );
  if (missingWeightings.length > 0) {
    console.warn(
      `[StackBuilder] Launching with ${missingWeightings.length} selected ` +
        `system(s) that have no weighting entry: ${missingWeightings.join(', ')}. ` +
        `These will be scored with neutral weighting by discovery.`,
    );
  }

  return {
    org_id: orgId,
    focus_id: state.focusId,
    industry_id: state.industryId,
    // R18-C1 T3 (AC5): the selected template is now registry-backed and
    // pre-populates the (editable) setup, so its id is sent to the backend. The
    // launch endpoint records template provenance on the run — which template
    // was chosen and which fields the user edited — and resolves the template's
    // pack/focus for an untouched launch. null when no template is selected.
    template_id: state.templateId,
    template_ids: state.templateIds?.length
      ? state.templateIds
      : (state.templateId ? [state.templateId] : []),
    selected_system_ids: state.selectedSystemIds,
    pack_id: packId,
    pack_ids: resolvedPackIds,
    weightings: state.weightings,
  };
}

function normaliseSystems(selectedIds: string[]): string[] {
  const normalised = selectedIds.map(id => {
    if (SALESFORCE_CLOUD_IDS.has(id)) return 'salesforce';
    if (id === 'jira_confluence') return 'jira';
    return id;
  });
  return [...new Set(normalised)];
}

// ── Pre-launch connector token-expiry guard ─────────────────────────────────────
//
// A discovery run against a connector whose OAuth token has expired silently
// produces no data from it. Before launching, we check the token status of every
// connector the run will use and refuse to start if any are expired — telling the
// user exactly which ones to reconnect (mirrors the "Token expired" / "Reconnect"
// state the Integration Hub tiles already show).

const CONNECTOR_DISPLAY_NAMES: Record<string, string> = {
  salesforce: 'Salesforce',
  servicenow: 'ServiceNow',
  jira: 'Jira',
  confluence: 'Confluence',
  sharepoint: 'SharePoint',
  github: 'GitHub',
  slack: 'Slack',
  teams: 'Microsoft Teams',
};

export function connectorDisplayName(id: string): string {
  return (
    CONNECTOR_DISPLAY_NAMES[id] ??
    id.replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase())
  );
}

// The run-used connectors worth an expiry check: only those the workspace has
// actually engaged (connected or needs_auth per the catalog). A never-configured
// or unknown system is ignored here — the run degrades gracefully for those, and
// checking them would false-positive (e.g. a not-connected system reads needs_auth).
export function connectorsToCheckForExpiry(
  systems: string[],
  catalog: WorkspaceCatalogResponse | null,
): string[] {
  const engaged = new Set(catalog ? getCatalogSystemIds(catalog) : []);
  return systems.filter(id => engaged.has(id));
}

// Given each checked connector's live token status, the ones that need a reconnect
// before a run can use them — the same condition the Integration Hub tile uses to
// show "Token expired": needs_auth (token gone/expired, no self-refresh) or
// refresh_failed (a live call was rejected 401 and the refresh could not recover).
export function expiredConnectors(
  statuses: Array<{ id: string; status: TokenStatus | null }>,
): string[] {
  return statuses
    .filter(s => s.status === 'needs_auth' || s.status === 'refresh_failed')
    .map(s => s.id);
}

// ── Session Persistence Hook ─────────────────────────────────────────────────

function useStackBuilderPersistence(
  orgId: string,
  setupState: ReturnType<typeof useSetupState>,
  apiBase: string,
  token: string,
) {
  const { state } = setupState;
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (!orgId) return;

    fetch(`${apiBase}/api/stack-builder/setup-state/${encodeURIComponent(orgId)}`, {
      credentials: 'omit',
      headers: buildAuthHeaders(token),
    })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data?.state && setupState.state.currentStep === 1) {
          if (setupState.restoreState) {
            setupState.restoreState(data.state);
          }
        }
      })
      .catch(() => {
        // Saved setup state is a convenience, not a blocker.
      });
  }, [orgId, apiBase, token]);

  useEffect(() => {
    if (!orgId) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);

    saveTimer.current = setTimeout(() => {
      fetch(
        `${apiBase}/api/stack-builder/setup-state/${encodeURIComponent(orgId)}`,
        {
          method: 'POST',
          credentials: 'omit',
          headers: buildAuthHeaders(token),
          body: JSON.stringify({
            state,
            saved_at: new Date().toISOString(),
          }),
        },
      ).catch(() => {
        // Session save failure should not block setup.
      });
    }, 1000);

    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    };
  }, [state, orgId, apiBase, token]);

  const clearSession = useCallback(() => {
    if (!orgId) return;
    fetch(
      `${apiBase}/api/stack-builder/setup-state/${encodeURIComponent(orgId)}`,
      {
        method: 'DELETE',
        credentials: 'omit',
        headers: buildAuthHeaders(token),
      },
    ).catch(() => {});
  }, [orgId, apiBase, token]);

  return { clearSession };
}

// ── Subcomponents ────────────────────────────────────────────────────────────

function SummaryRow({
  label,
  value,
}: {
  label: string;
  value: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border/70 py-2 last:border-0">
      <span className="text-xs text-muted">{label}</span>
      <span className="text-right text-xs font-medium text-text">{value}</span>
    </div>
  );
}

function StackBuilderSidePanel({
  setupState,
  industries,
  templates,
}: {
  setupState: ReturnType<typeof useSetupState>;
  industries: IndustryListItem[];
  templates: TemplateListItem[];
}) {
  const { state, confidence } = setupState;
  const confirmedCount = state.selectedSystemIds.filter(id => state.weightings[id]?.confirmed).length;
  const activeStep = setupState.steps.find(step => step.number === state.currentStep);

  // Registry-driven labels: a relabelled industry/template reads correctly on
  // every summary surface with no frontend code change.
  const industryLabel = state.industryId
    ? industries.find(i => i.industry_id === state.industryId)?.label
        ?? state.industryId
    : 'Optional';
  const selectedTemplateIds = state.templateIds?.length
    ? state.templateIds
    : (state.templateId ? [state.templateId] : []);
  const templateLabel = selectedTemplateIds.length
    ? selectedTemplateIds
        .map(id => templates.find(template => template.template_id === id)?.label ?? id)
        .join(' + ')
    : 'Optional';

  return (
    <div className="sticky top-[76px] flex flex-col gap-3">
      <section className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="mb-3 flex items-center gap-2">
          <Target size={16} className="text-accent" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-text">Discovery confidence</h2>
        </div>
        <DiscoveryConfidenceBar state={confidence} showSummary={state.currentStep === 4} />
      </section>

      <section className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="mb-3 flex items-center gap-2">
          <Layers3 size={16} className="text-accent" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-text">Setup summary</h2>
        </div>
        <SummaryRow label="Current step" value={activeStep?.label ?? `Step ${state.currentStep}`} />
        <SummaryRow label="Focus" value={state.focusId ? FOCUS_LABELS[state.focusId] : 'Not selected'} />
        <SummaryRow label="Industry" value={industryLabel} />
        <SummaryRow label="Template" value={templateLabel} />
      </section>

      <section className="rounded-xl border border-border bg-panel p-4 shadow-sm">
        <div className="mb-3 flex items-center gap-2">
          <Database size={16} className="text-accent" aria-hidden="true" />
          <h2 className="text-sm font-semibold text-text">Sources</h2>
        </div>
        <SummaryRow label="Selected systems" value={state.selectedSystemIds.length} />
        <SummaryRow label="Salesforce products" value={state.selectedSalesforceClouds.length} />
        <SummaryRow label="Confirmed weights" value={`${confirmedCount}/${state.selectedSystemIds.length || 0}`} />
      </section>
    </div>
  );
}

// ── Main Page Component ──────────────────────────────────────────────────────

interface Props {
  apiBase?: string;
  token?: string;
}


export default function StackBuilderPage({
  // Use ?? (not ||) so an explicitly-empty VITE_API_BASE_URL — the same-origin
  // deployment value, where Nginx proxies /api/* to the backend — is respected.
  // With ||, an empty string is falsy and wrongly falls back to localhost:8000.
  apiBase = import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.DEV ? 'http://localhost:8000' : ''),
  token: tokenProp,
}: Props) {

  // Stale Run Fix Part 1: Bring in the context and navigate hook
  const { setRunId } = useRunContext();
  const navigate = useNavigate();
  const auth = useAuthOptional();

  // Multi-tenancy: sign catalog/launch/persistence calls with the in-session
  // JWT and key Stack Builder state by THIS user's org, so a run is created in
  // and scoped to the right workspace. An explicit `token` prop (tests) wins;
  // otherwise the session token, then the dev-token fallback.
  const token =
    tokenProp ?? auth?.token ?? (import.meta.env.VITE_DEV_JWT || 'dev-token-change-me');
  const orgId = auth?.user?.org_id ?? ORG_ID_HEADER ?? 'default';

  const setupState = useSetupState();
  const { push } = useToast();

  const [launchState, setLaunchState] = useState<LendingGuideLaunchState>('setup');

  const { state, steps } = setupState;
  const { clearSession } = useStackBuilderPersistence(orgId, setupState, apiBase, token);
  const copy = STEP_COPY[state.currentStep] ?? STEP_COPY[1];

  // Workspace catalog via the shared data cache: the SalesforceProductPicker
  // invalidates cacheKeys.workspaceCatalog on save, so the pack Stack Builder
  // resolves (from the declared Salesforce product) is never stale — no manual
  // refresh needed. The fetcher is pure (no side effects); catalog-derived setup
  // state is applied in the effect below.
  const catalogFetcher = useCallback(async (): Promise<WorkspaceCatalogResponse | null> => {
    const headers = buildAuthHeaders(token);
    const r = await fetch(`${apiBase}/api/integration-hub/workspace-catalog`, {
      credentials: 'omit',
      headers,
    });
    if (!r.ok) throw new Error(`Catalog fetch failed: ${r.status}`);
    return r.json();
  }, [apiBase, token]);

  const {
    data: catalogData,
    loading: catalogLoading,
    error: catalogErrObj,
    refetch: refetchCatalog,
  } = useResource<WorkspaceCatalogResponse | null>(cacheKeys.workspaceCatalog, catalogFetcher);
  const catalog = catalogData ?? null;
  const catalogError = catalogErrObj
    ? 'Could not load your connected systems. Please retry.'
    : null;

  // Apply catalog-derived setup state whenever the catalog changes (or errors).
  useEffect(() => {
    if (catalogErrObj) {
      setupState.setSalesforceClouds([]);
      return;
    }
    if (catalog) {
      setupState.setSalesforceClouds(getCatalogSalesforceProducts(catalog));
      setupState.initFromCatalog?.(catalog);
    }
    // setupState methods are the effect's other inputs; catalog identity changes
    // per fetch so this mirrors the previous per-fetch application.
  }, [catalog, catalogErrObj, setupState.setSalesforceClouds, setupState.initFromCatalog]);

  // R18-C1 T3: industries + templates come from the backend registry, not
  // hardcoded frontend arrays. Both come from the same source of truth, so ONE
  // cache key drives both pickers and one Retry reloads both. On the SHARED
  // cache (not page-local state) so navigating away and back re-renders them
  // from cache with no refetch and no skeleton — the registry only changes when
  // the backend's does, which the background revalidation picks up silently.
  // On error the lists stay EMPTY and a retry is surfaced (see
  // DiscoveryFocusPage) — never a stale local fallback (AC7/AC8/AC10).
  const registryFetcher = useCallback(async () => {
    const [fetchedIndustries, fetchedTemplates] = await Promise.all([
      fetchIndustries(apiBase, token),
      fetchTemplates(apiBase, token),
    ]);
    return { industries: fetchedIndustries, templates: fetchedTemplates };
  }, [apiBase, token]);

  const {
    data: registryData,
    loading: registryLoading,
    error: registryErrObj,
    refetch: fetchRegistry,
  } = useResource<{ industries: IndustryListItem[]; templates: TemplateListItem[] }>(
    cacheKeys.stackBuilderRegistry,
    registryFetcher,
  );
  // Memoised so the empty fallbacks keep a stable identity across renders.
  const industries = useMemo(() => registryData?.industries ?? [], [registryData]);
  const templates = useMemo(() => registryData?.templates ?? [], [registryData]);
  const registryError = registryErrObj
    ? 'Could not load industries and templates from the registry. Please retry.'
    : null;

  const loadIndustrySystemDefaults = useCallback(
    (industryId: string): Promise<SystemDefaultItem[]> =>
      fetchIndustrySystemDefaults(apiBase, token, industryId),
    [apiBase, token],
  );

  // NOTE: this page used to re-fetch the catalog itself on tab focus/visibility.
  // That is now both redundant and harmful: DataCacheProvider already
  // background-revalidates every observed key on focus (and on the org change
  // stream), whereas useResource's refetch() is a FOREGROUND run — it flips
  // `loading`, so returning to the tab replaced the already-loaded step with its
  // skeleton. The shared cache keeps the catalog fresh silently instead.

  const handleLaunch = useCallback(async () => {
    if (launchState === 'launching') return;
    setLaunchState('launching');
    // R191-P1 T5: resolve the full multi-pack selection; the primary (first) pack
    // stays the singular value for backward-compatible callers.
    const packIds = resolvePackIds(state, catalog, industries, templates);
    const packId = packIds[0] ?? resolvePackId(state, catalog, industries, templates);
    const systems = normaliseSystems(state.selectedSystemIds);
    const headers = buildAuthHeaders(token);

    // Guard: refuse to start a run that uses a connector whose token has expired —
    // it would silently return no data. Name the offenders and send the user to
    // reconnect them in the Integration Hub. The check itself is non-fatal: if
    // token-status can't be read, we let the launch proceed rather than block it.
    const toCheck = connectorsToCheckForExpiry(systems, catalog);
    if (toCheck.length > 0) {
      try {
        const statuses = await Promise.all(
          toCheck.map(async id => {
            try {
              return { id, status: (await fetchTokenStatus(id)).status };
            } catch {
              return { id, status: null as TokenStatus | null };
            }
          }),
        );
        const expired = expiredConnectors(statuses);
        if (expired.length > 0) {
          const names = expired.map(connectorDisplayName).join(', ');
          const many = expired.length > 1;
          push(
            `Can't start discovery — ${many ? 'these connectors have' : 'this connector has'} ` +
              `an expired token: ${names}. Reconnect ${many ? 'them' : 'it'} in the ` +
              `Integration Hub, then try again.`,
          );
          setLaunchState('setup');
          return;
        }
      } catch {
        // Whole check failed (e.g. network) — do not block the launch on it.
      }
    }

    let runId: string;
    try {
      const launchResp = await fetch(`${apiBase}/api/stack-builder/launch`, {
        method: 'POST',
        credentials: 'omit',
        headers,
        body: JSON.stringify(buildStackBuilderLaunchPayload(state, packId, orgId, packIds)),
      });
      if (!launchResp.ok) {
        throw new Error(`Launch failed: ${launchResp.status}`);
      }
      const launchData = await launchResp.json();
      runId = launchData.runId;
    } catch (err) {
      console.error('[StackBuilderPage] Launch failed:', err);
      setLaunchState('launch_error');
      return;
    }

    // Offline to Live Fix: Set mode to "live"
    void fetch(`${apiBase}/api/runs/${runId}/compute`, {
        method: 'POST',
        credentials: 'omit',
        headers,
        body: JSON.stringify({
          mode: 'live',
          systems,
          pack: packId,
          // R191-P1 T5: run every selected pack; backend reconciles with `pack`.
          pack_ids: packIds,
        }),
      }).catch((err) => {
        console.error('[StackBuilderPage] Compute trigger failed:', err);
      });

    clearSession();

    // Stale Run Fix Part 2: Update global context BEFORE navigating
    setRunId(runId);
    navigate(`/discovery-run?runId=${runId}`);

  }, [state, catalog, industries, templates, orgId, apiBase, clearSession, navigate, setRunId, token, launchState, push]);

  // Viewers cannot configure or launch discovery (analyst+ only). The nav hides
  // this destination for them, but a viewer can still reach it via a direct URL
  // or a stale link — so render a read-only notice instead of the interactive
  // builder. No selectable control is mounted, so nothing can be changed, and the
  // backend (setup-state/launch are analyst+) enforces the same boundary anyway.
  if (isViewerRole(auth?.user?.role)) {
    return (
      <PageShell
        title="Stack Builder"
        description="Discovery configuration is managed by owners and analysts."
      >
        <div className="rounded-xl border border-border bg-panel p-6 text-sm text-muted">
          <p className="font-medium text-text">Read-only access</p>
          <p className="mt-1">
            You have viewer access to this workspace. Configuring systems and
            launching a discovery run requires an analyst or owner role. Ask a
            workspace owner if you need to run discovery.
          </p>
        </div>
      </PageShell>
    );
  }

  return (
    <PageShell
      title={copy.title}
      description={copy.description}
      actions={
        <span className="inline-flex items-center gap-2 rounded-full border border-border bg-panel px-3 py-1.5 text-sm font-medium text-text">
          <CheckCircle2 size={15} className="text-accent" aria-hidden="true" />
          Step {state.currentStep} of 4
        </span>
      }
    >
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-5">
          <section className="rounded-xl border border-border bg-panel p-4 shadow-sm">
            <StackBuilderProgressBar steps={steps} />
          </section>

          {state.templateId === 'commercial_lending' && (() => {
            const selectedTemplate = templates.find(
              template => template.template_id === state.templateId,
            );
            if (!selectedTemplate) return null;
            return (
              <LendingFirstRunGuide
                template={selectedTemplate}
                state={state}
                packId={resolvePackId(state, catalog, industries, templates)}
                launchState={
                  launchState === 'setup' && state.currentStep === 4
                    ? 'ready'
                    : launchState
                }
              />
            );
          })()}

          {state.currentStep === 1 && (
            <DiscoveryFocusPage
              setupState={setupState}
              industries={industries}
              templates={templates}
              registryLoading={registryLoading}
              registryError={registryError}
              onRetryRegistry={fetchRegistry}
              fetchSystemDefaults={loadIndustrySystemDefaults}
            />
          )}
          {state.currentStep === 2 && catalogLoading && (
            // Skeleton mirrors the connected-systems list so the rows fill the
            // same space instead of snapping in after a centered spinner.
            <div
              aria-busy="true"
              aria-label="Loading your connected systems"
              className="rounded-xl border border-border bg-panel p-5 shadow-sm"
            >
              <Skeleton className="h-5 w-56" />
              <Skeleton className="mt-2 h-3 w-80" />
              <div className="mt-5 space-y-3">
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-14 w-full rounded-lg" />
                ))}
              </div>
            </div>
          )}
          {state.currentStep === 2 && catalogError && !catalogLoading && (
            <InlineError
              title="Could not load your connected systems"
              message={catalogError}
              onRetry={refetchCatalog}
            />
          )}
          {state.currentStep === 2 && !catalogLoading && !catalogError && (
            <YourSystemsPage setupState={setupState} catalog={catalog} />
          )}
          {state.currentStep === 3 && (
            <SourceWeightingPage setupState={setupState} />
          )}
          {state.currentStep === 4 && (
            <DiscoveryPlanPage
              setupState={setupState}
              industries={industries}
              templates={templates}
              activePackId={resolvePackId(state, catalog, industries, templates)}
              activePackIds={resolvePackIds(state, catalog, industries, templates)}
              salesforcePacks={salesforcePacksFromCatalog(catalog)}
              onLaunch={handleLaunch}
              launchState={launchState}
            />
          )}
        </div>

        <aside className="min-w-0">
          <StackBuilderSidePanel
            setupState={setupState}
            industries={industries}
            templates={templates}
          />
        </aside>
      </div>
    </PageShell>
  );
}
