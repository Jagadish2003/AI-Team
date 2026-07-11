/**
 * StackBuilderPage — Sprint 7 Final Wiring + Sprint 8 Foundation
 *
 * Combines StackBuilderRouter and StackBuilderRouterPage into a single
 * page-level component. Owns useSetupState(), handles session persistence,
 * routes between the 4 child pages, and translates state to /launch + /compute.
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useRunContext } from '../context/RunContext';
import { useAuthOptional } from '../context/AuthContext';
import { isViewerRole } from '../utils/roles';

// Import components and types
import { CheckCircle2, Database, Layers3, Loader2, Target } from 'lucide-react';
import type { WorkspaceCatalogResponse } from '../types/workspace_catalog';
import type { SetupState, SystemWeighting } from '../types/stack_builder';
import PageShell from '../components/common/PageShell';
import {
  DiscoveryConfidenceBar,
  StackBuilderProgressBar,
  useSetupState,
} from '../components/stack_builder';

// Import the 4 inner screens
import DiscoveryFocusPage from './DiscoveryFocusPage';
import YourSystemsPage from './YourSystemsPage';
import SourceWeightingPage from './SourceWeightingPage';
import DiscoveryPlanPage from './DiscoveryPlanPage';

// ── Static Definitions ───────────────────────────────────────────────────────

const INDUSTRY_PACK_HINTS: Record<string, string[]> = {
  financial_services: ['ncino', 'service_cloud'],
  public_sector: ['strs_benefits', 'service_cloud'],
  logistics_supply_chain: ['service_cloud'],
  retail_commerce: ['service_cloud'],
  healthcare: ['service_cloud'],
  energy_utilities: ['service_cloud'],
  manufacturing: ['service_cloud'],
  technology: ['service_cloud'],
};

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

const INDUSTRY_LABELS: Record<string, string> = {
  financial_services: 'Financial services',
  public_sector: 'Public sector',
  logistics_supply_chain: 'Logistics & supply chain',
  retail_commerce: 'Retail & commerce',
  healthcare: 'Healthcare',
  energy_utilities: 'Energy & utilities',
  manufacturing: 'Manufacturing',
  technology: 'Technology',
};

const TEMPLATE_LABELS: Record<string, string> = {
  commercial_lending: 'Commercial lending',
  public_retirement: 'Public retirement',
  service_operations: 'Service operations',
  revenue_operations: 'Revenue operations',
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
): string {
  const declaredCloud = getCatalogSalesforceProducts(catalog)
    .find(productId => CLOUD_PACK_REGISTRY[productId]);

  if (declaredCloud) {
    return CLOUD_PACK_REGISTRY[declaredCloud];
  }

  // Fallback 1: industry hint, when an industry is selected.
  if (state.industryId) {
    const hints = INDUSTRY_PACK_HINTS[state.industryId];
    if (hints && hints.length > 0) return hints[0];
  }

  // Fallback 2: safe default — never a Stack Builder pre-selection.
  return 'service_cloud';
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
  selected_system_ids: string[];
  pack_id: string;
  weightings: Record<string, SystemWeighting>;
}

export function buildStackBuilderLaunchPayload(
  state: SetupState,
  packId: string,
  orgId: string,
): StackBuilderLaunchPayload {
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
    // Dummy template picker: the UI keeps a template highlight + suggested-focus
    // note for guidance only, but template selection must not influence the run,
    // so template_id is never sent to the backend (always null).
    template_id: null,
    selected_system_ids: state.selectedSystemIds,
    pack_id: packId,
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
}: {
  setupState: ReturnType<typeof useSetupState>;
}) {
  const { state, confidence } = setupState;
  const confirmedCount = state.selectedSystemIds.filter(id => state.weightings[id]?.confirmed).length;
  const activeStep = setupState.steps.find(step => step.number === state.currentStep);

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
        <SummaryRow label="Industry" value={state.industryId ? INDUSTRY_LABELS[state.industryId] : 'Optional'} />
        <SummaryRow label="Template" value={state.templateId ? TEMPLATE_LABELS[state.templateId] : 'Optional'} />
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
  const [catalog, setCatalog] = useState<WorkspaceCatalogResponse | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const { state, steps } = setupState;
  const { clearSession } = useStackBuilderPersistence(orgId, setupState, apiBase, token);
  const copy = STEP_COPY[state.currentStep] ?? STEP_COPY[1];

  const fetchCatalog = useCallback(() => {
    setCatalogLoading(true);
    const headers = buildAuthHeaders(token);

    fetch(`${apiBase}/api/integration-hub/workspace-catalog`, {
      credentials: 'omit',
      headers,
    })
      .then(r => {
        if (!r.ok) throw new Error(`Catalog fetch failed: ${r.status}`);
        return r.json();
      })
      .then((fetchedCatalog: WorkspaceCatalogResponse | null) => {
        setCatalog(fetchedCatalog);
        setCatalogError(null);
        setupState.setSalesforceClouds(getCatalogSalesforceProducts(fetchedCatalog));
      })
      .catch((err) => {
        console.error('[StackBuilderPage] Catalog fetch failed:', err);
        setCatalogError('Could not load your connected systems. Please retry.');
        setCatalog(null);
        setupState.setSalesforceClouds([]);
      })
      .finally(() => setCatalogLoading(false));
  }, [apiBase, token, setupState.setSalesforceClouds]);

  useEffect(() => {
    fetchCatalog();
  }, [fetchCatalog]);

  useEffect(() => {
    const handleVisibilityChange = () => {
      if (!document.hidden) {
        fetchCatalog();
      }
    };

    const handleFocus = () => {
      fetchCatalog();
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    window.addEventListener('focus', handleFocus);
    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      window.removeEventListener('focus', handleFocus);
    };
  }, [fetchCatalog]);

  useEffect(() => {
    if (catalog && setupState.initFromCatalog) {
      setupState.initFromCatalog(catalog);
    }
  }, [catalog, setupState.initFromCatalog]);

  const handleLaunch = useCallback(async () => {
    const packId = resolvePackId(state, catalog);
    console.log(`packId:`, packId);
    const systems = normaliseSystems(state.selectedSystemIds);
    const headers = buildAuthHeaders(token);

    let runId: string;
    try {
      const launchResp = await fetch(`${apiBase}/api/stack-builder/launch`, {
        method: 'POST',
        credentials: 'omit',
        headers,
        body: JSON.stringify(buildStackBuilderLaunchPayload(state, packId, orgId)),
      });
      if (!launchResp.ok) {
        throw new Error(`Launch failed: ${launchResp.status}`);
      }
      const launchData = await launchResp.json();
      runId = launchData.runId;
    } catch (err) {
      console.error('[StackBuilderPage] Launch failed:', err);
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
        }),
      }).catch((err) => {
        console.error('[StackBuilderPage] Compute trigger failed:', err);
      });

    clearSession();

    // Stale Run Fix Part 2: Update global context BEFORE navigating
    setRunId(runId);
    navigate(`/discovery-run?runId=${runId}`);

  }, [state, catalog, orgId, apiBase, clearSession, navigate, setRunId, token]);

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

          {state.currentStep === 1 && (
            <DiscoveryFocusPage setupState={setupState} />
          )}
          {state.currentStep === 2 && catalogLoading && (
            <div className="flex flex-col items-center justify-center gap-3 py-16 text-muted">
              <Loader2 size={24} className="animate-spin text-emerald-500" aria-hidden />
              <p className="text-sm">Loading your connected systems…</p>
            </div>
          )}
          {state.currentStep === 2 && catalogError && !catalogLoading && (
            <div className="rounded-xl border border-red-200 bg-red-50/10 px-5 py-4 text-sm text-red-400">
              {catalogError}
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="ml-2 underline hover:no-underline"
              >
                Retry
              </button>
            </div>
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
              onLaunch={handleLaunch}
            />
          )}
        </div>

        <aside className="min-w-0">
          <StackBuilderSidePanel setupState={setupState} />
        </aside>
      </div>
    </PageShell>
  );
}
