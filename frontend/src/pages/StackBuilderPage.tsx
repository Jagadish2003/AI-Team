import React, { useCallback, useEffect, useRef } from 'react';
import { CheckCircle2, Database, Layers3, Target } from 'lucide-react';
import PageShell from '../components/common/PageShell';
import {
  DiscoveryConfidenceBar,
  StackBuilderProgressBar,
  useSetupState,
} from '../components/stack_builder';
import DiscoveryFocusPage from '../pages/DiscoveryFocusPage';
import YourSystemsPage from '../pages/YourSystemsPage';
import SourceWeightingPage from '../pages/SourceWeightingPage';
import DiscoveryPlanPage from '../pages/DiscoveryPlanPage';

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

const STEP_COPY: Record<number, { title: string; description: string }> = {
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

function buildAuthHeaders(token: string) {
  return {
    'Content-Type': 'application/json',
    Authorization: `Bearer ${token}`,
  };
}

function resolvePackId(state: ReturnType<typeof useSetupState>['state']): string {
  if (!state.industryId) return 'service_cloud';
  const hints = INDUSTRY_PACK_HINTS[state.industryId];
  if (!hints || hints.length === 0) return 'service_cloud';
  return hints[0];
}

function normaliseSystems(selectedIds: string[]): string[] {
  const normalised = selectedIds.map(id =>
    SALESFORCE_CLOUD_IDS.has(id) ? 'salesforce' : id,
  );
  return [...new Set(normalised)];
}

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
          setupState.restoreState(data.state);
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

interface Props {
  orgId: string;
  onComplete: (runId: string) => void;
  apiBase?: string;
  token?: string;
}

export default function StackBuilderPage({
  orgId,
  onComplete,
  apiBase = '',
  token = 'dev-token-change-me',
}: Props) {
  const setupState = useSetupState();
  const { state, steps } = setupState;
  const { clearSession } = useStackBuilderPersistence(orgId, setupState, apiBase, token);
  const copy = STEP_COPY[state.currentStep] ?? STEP_COPY[1];

  const handleLaunch = useCallback(async () => {
    const packId = resolvePackId(state);
    const systems = normaliseSystems(state.selectedSystemIds);
    const headers = buildAuthHeaders(token);

    let runId: string;
    try {
      const launchResp = await fetch(`${apiBase}/api/stack-builder/launch`, {
        method: 'POST',
        credentials: 'omit',
        headers,
        body: JSON.stringify({
          org_id: orgId,
          focus_id: state.focusId,
          industry_id: state.industryId,
          template_id: state.templateId,
          selected_system_ids: state.selectedSystemIds,
          pack_id: packId,
          weightings: state.weightings,
        }),
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

    void fetch(`${apiBase}/api/runs/${runId}/compute`, {
        method: 'POST',
        credentials: 'omit',
        headers,
        body: JSON.stringify({
          mode: 'live',
          systems,
          pack: 'strs_benefits',
        }),
      }).catch((err) => {
        console.error('[StackBuilderPage] Compute trigger failed:', err);
      });

    clearSession();
    onComplete(runId);
  }, [state, orgId, apiBase, clearSession, onComplete, token]);

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
          {state.currentStep === 2 && (
            <YourSystemsPage setupState={setupState} />
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
